"""Escape hatch and maintenance: run Python against the live Inventor session, reload the tools."""
from __future__ import annotations

import contextlib
import importlib
import io
import sys
import traceback

from ..com import MM, InventorError, K, items, late, typed_call
from ..registry import TOOLS, tool
from ..session import Session

_NS: dict = {}


def _log_snippet(s: Session, code: str, outcome: str) -> None:
    """Append what ran to execute_python.log (time, SHA-256 of the code, outcome, the code), for the audit trail."""
    import hashlib
    import os
    import time as _t
    try:
        os.makedirs(s.workdir, exist_ok=True)
        with open(os.path.join(s.workdir, "execute_python.log"), "a", encoding="utf-8") as f:
            f.write(f"--- {_t.strftime('%Y-%m-%d %H:%M:%S')} sha256={hashlib.sha256(code.encode()).hexdigest()} "
                    f"{outcome}\n{code}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- Inventor API reference
_API_INDEX: list | None = None
_VT = {2: "short", 3: "long", 4: "float", 5: "double", 8: "string", 9: "object", 11: "bool", 12: "variant",
       13: "unknown", 16: "byte", 17: "byte", 18: "ushort", 19: "ulong", 20: "int64", 22: "int", 24: "void", 25: "HRESULT",
       26: "ptr", 27: "array", 29: "object"}


def _type_name(ti, td) -> str:
    """A readable name for a TYPEDESC from the type library."""
    try:
        import pythoncom
        vt = td[0] if isinstance(td, tuple) else td
        if isinstance(td, tuple) and vt == pythoncom.VT_PTR:
            return _type_name(ti, td[1])
        if isinstance(td, tuple) and vt == pythoncom.VT_USERDEFINED:
            return ti.GetRefTypeInfo(td[1]).GetDocumentation(-1)[0]
        if isinstance(td, tuple) and vt == pythoncom.VT_SAFEARRAY:
            return _type_name(ti, td[1]) + "[]"
        return _VT.get(vt, str(vt))
    except Exception:
        return "?"


def _api_index() -> list:
    """(kind, owner, name, signature, doc) for every interface member and enum in Inventor's type library."""
    global _API_INDEX
    if _API_INDEX is not None:
        return _API_INDEX
    import pythoncom

    from ..com import INVENTOR_TYPELIB
    tlb = pythoncom.LoadRegTypeLib(INVENTOR_TYPELIB, 1, 0, 0)
    out = []
    for i in range(tlb.GetTypeInfoCount()):
        ti = tlb.GetTypeInfo(i)
        attr = ti.GetTypeAttr()
        owner = tlb.GetDocumentation(i)[0]
        if attr.typekind == pythoncom.TKIND_ENUM:
            names = [ti.GetNames(ti.GetVarDesc(j).memid)[0] for j in range(attr.cVars)]
            out.append(("enum", owner, owner, ", ".join(names[:40]) + (" ..." if len(names) > 40 else ""),
                        tlb.GetDocumentation(i)[1] or ""))
            continue
        if attr.typekind not in (pythoncom.TKIND_DISPATCH, pythoncom.TKIND_INTERFACE):
            continue
        seen = set()
        for j in range(attr.cFuncs):
            fd = ti.GetFuncDesc(j)
            names = ti.GetNames(fd.memid)
            name = names[0]
            if name.startswith("_") or name in ("QueryInterface", "AddRef", "Release", "GetTypeInfoCount",
                                                "GetTypeInfo", "GetIDsOfNames", "Invoke"):
                continue
            kind = {pythoncom.INVOKE_PROPERTYGET: "property", pythoncom.INVOKE_PROPERTYPUT: "property",
                    pythoncom.INVOKE_PROPERTYPUTREF: "property"}.get(fd.invkind, "method")
            if (name, kind) in seen:
                continue
            seen.add((name, kind))
            args = []
            for k, a in enumerate(fd.args):
                pname = names[k + 1] if k + 1 < len(names) else f"arg{k}"
                flags = a[1]
                tag = "out " if flags & pythoncom.PARAMFLAG_FOUT and not flags & pythoncom.PARAMFLAG_FIN else ""
                opt = "?" if flags & pythoncom.PARAMFLAG_FOPT else ""
                args.append(f"{tag}{pname}{opt}: {_type_name(ti, a[0])}")
            rt = fd.rettype
            ret = _type_name(ti, rt[0] if isinstance(rt, tuple) and len(rt) == 3 else rt)
            sig = f"{name}({', '.join(args)}) -> {ret}" if kind == "method" else f"{name}: {ret}"
            doc = ti.GetDocumentation(fd.memid)[1] or ""
            out.append((kind, owner, name, sig, doc))
    _API_INDEX = out
    return out


@tool("system", mutates=False, com=False)
def api_lookup(s: Session, query: str, object: str = "", limit: int = 25) -> dict:
    """Look up Inventor's API in its own type library (offline, version-exact): objects, their methods and
    properties with argument names and types ([out] marks arguments to pass to typed_call), and enums. For writing
    execute_python code. Distances in the API are cm, angles radians.

    query: word to find in member names (e.g. "AddByDistanceExtent", "Fillet", "Mass")
    object: only members of this object (e.g. "ExtrudeFeatures", "PartComponentDefinition")
    limit: how many matches to return
    """
    q, obj = query.lower(), object.lower()
    rows = []
    for kind, owner, name, sig, doc in _api_index():
        if obj and owner.lower() != obj:
            continue
        if q and q not in name.lower() and q not in owner.lower():
            continue
        rows.append({"object": owner, "kind": kind, "signature": sig, **({"doc": doc[:200]} if doc else {})})
    exact = [r_ for r_ in rows if r_["signature"].lower().startswith(q)]
    rows = exact + [r_ for r_ in rows if r_ not in exact]
    return {"matches": rows[:int(limit)], "total": len(rows)}


# --------------------------------------------------------------------------- calls held up by a dialog
@tool("system", mutates=False, com=False)
def pending_call(s: Session, wait_seconds: float = 0.0) -> dict:
    """The call that came back early because Inventor showed a dialog (or took too long): whether it has finished
    now, its result or error, and any dialog Inventor still shows. Reads windows only, so it works while Inventor
    is blocked.

    wait_seconds: wait up to this long for it to finish first
    """
    import concurrent.futures as cf
    import time as _t
    forge = getattr(s, "forge", None)
    p = getattr(forge, "pending", None)
    if not p:
        return {"pending": False}
    fut = p["future"]
    try:
        res = fut.result(timeout=max(0.0, float(wait_seconds)))
    except cf.TimeoutError:
        from .. import uidrive as U
        held = {d_["hwnd"] for d_ in p.get("dialogs") or []}
        dialogs = [d_ for d_ in (U.dialog_rows(s.pid) if s.pid else []) if not held or d_["hwnd"] in held]
        return {"pending": True, "tool": p["tool"], "running_s": round(_t.time() - p["started"], 1),
                "dialogs": [{k: d[k] for k in ("title", "text", "buttons")} for d in dialogs]}
    except Exception as e:  # noqa: BLE001
        forge.pending = None
        return {"pending": False, "tool": p["tool"], "finished": True, "error": str(e)}
    forge.pending = None
    return {"pending": False, "tool": p["tool"], "finished": True, "result": res}


@tool("system", com=False)
def answer_dialog(s: Session, button: str, title: str = "", any_dialog: bool = False) -> dict:
    """Press a button on the Inventor dialog that is holding up a call (pending_call shows it), with a window
    message, not the mouse. Only use a button the user would agree to: ask them when it matters (e.g. "Save").
    Dialogs the user opened themselves are left alone unless any_dialog is set (only on the user's request).

    button: the button's label as listed (OK, Cancel, Yes, No...)
    title: which dialog, when more than one is held (default: the first)
    any_dialog: allow a dialog no tool call is waiting on
    """
    from .. import uidrive as U
    if not s.pid:
        raise InventorError("not connected to an Inventor")
    forge = getattr(s, "forge", None)
    p = getattr(forge, "pending", None)
    held = {d_["hwnd"] for d_ in (p or {}).get("dialogs") or []}
    if not held and not any_dialog:
        raise InventorError("no tool call is waiting on a dialog (pending_call); dialogs the user opened are theirs "
                            "to answer")
    dialogs = [d for d in U.dialog_rows(s.pid) if (any_dialog or d["hwnd"] in held)
               and (not title or title.lower() in d["title"].lower())]
    if not dialogs:
        raise InventorError("Inventor shows no dialog" + (f" titled like {title!r}" if title else ""))
    d = dialogs[0]
    if not U.press_dialog_button(d["hwnd"], button):
        raise InventorError(f"dialog '{d['title']}' has no button {button!r}; its buttons are {d['buttons']}")
    return {"pressed": button, "dialog": d["title"], "text": d["text"]}


# --------------------------------------------------------------------------- standards
@tool("system")
def list_standards(s: Session) -> dict:
    """The drafting/engineering standards this server can work to, with what each one sets (projection, drawing
    standard, general tolerance, threads, fasteners, materials, sheet sizes). Show these to the user to choose from."""
    from ..standards import STANDARDS, summary
    return {"chosen": s.standard, "standards": [summary(k) for k in STANDARDS]}


@tool("system")
def set_standard(s: Session, standard: str, drawing_units: str = "mm") -> dict:
    """Set the standard for the rest of this session (ask the user first; list_standards has the options). Drawings
    use its template, projection angle and dimension style; standard parts and materials follow its series.

    standard: AS1100, ISO, ASME, DIN, BS8888, JIS or GB (names like "AS 1100" or "ANSI" are accepted)
    drawing_units: mm, or in (ASME only: inch drawing template; models stay in mm)
    """
    from ..com import InventorError
    from ..standards import key, summary
    k = key(standard)
    u = (drawing_units or "mm").lower()
    if u not in ("mm", "in"):
        raise InventorError("drawing_units must be mm or in")
    if u == "in" and k != "ASME":
        raise InventorError("inch drawings are only offered with ASME")
    s.standard, s.drawing_units = k, u
    return {"set": True, **summary(k, u)}


@tool("system")
def get_standard(s: Session) -> dict:
    """The standard chosen for this session, or chosen=false if the user hasn't been asked yet."""
    from ..standards import DEFAULT, summary
    if not s.standard:
        return {"chosen": False, "default_if_not_asked": DEFAULT,
                "ask": "Ask the user which standard to work to (list_standards), then call set_standard."}
    return {"chosen": True, **summary(s.standard, s.drawing_units)}


@tool("system")
def execute_python(s: Session, code: str, undo_on_error: bool = True) -> dict:
    """Run Python in a persistent namespace with the live Inventor objects: app, doc, cdef (component
    definition or None), tg (TransientGeometry), to (TransientObjects), K (all Inventor enum constants),
    items(collection), typed_call(obj, method, ...) for [out]-parameter methods, MM (=0.1, mm->cm).
    Inventor API units are cm and radians. The value of a final expression is returned; print() output too.
    Use it for anything the dedicated tools don't cover. Every snippet is logged (time, SHA-256, outcome) to
    execute_python.log in the workdir.

    undo_on_error: run inside one undo step and roll the document back if the code raises (default true)
    Don't run Inventor commands synchronously here (ControlDefinition.Execute2(True)): one that opens a dialog
    holds the whole server until someone answers it. Use Execute2(False), or the dedicated tools.
    """
    app = s.app
    # the session's working document (brought to the front, so code using app.ActiveDocument agrees), else the active one
    doc = s.front() if s._working() is not None else late(app.ActiveDocument)
    cdef = None
    if doc is not None:
        try:
            cdef = doc.ComponentDefinition
        except Exception:
            cdef = None
    _NS.update({"app": app, "doc": doc, "cdef": cdef, "tg": app.TransientGeometry, "to": app.TransientObjects,
                "K": K, "items": items, "typed_call": typed_call, "MM": MM, "session": s})
    _log_snippet(s, code, "start")
    buf = io.StringIO()
    result = None
    txn = None
    if undo_on_error and doc is not None:
        try:
            txn = app.TransactionManager.StartTransaction(doc, "execute_python")
        except Exception:
            txn = None
    try:
        import ast
        tree = ast.parse(code, mode="exec")
        last = tree.body[-1] if tree.body else None
        with contextlib.redirect_stdout(buf):
            if isinstance(last, ast.Expr):
                exec(compile(ast.Module(tree.body[:-1], type_ignores=[]), "<forge>", "exec"), _NS)
                result = eval(compile(ast.Expression(last.value), "<forge>", "eval"), _NS)
            else:
                exec(compile(tree, "<forge>", "exec"), _NS)
    except Exception:
        rolled = False
        if txn is not None:
            try:
                txn.Abort()
                rolled = True
            except Exception:
                pass
        # the traceback from the snippet's own lines, not the server's
        tb = traceback.format_exc(limit=8)
        lines = [ln for ln in tb.splitlines() if "inventor_forge" not in ln]
        _log_snippet(s, code, "error")
        return {"ok_python": False, "stdout": buf.getvalue()[-4000:], "error": "\n".join(lines)[-3000:],
                "rolled_back": rolled}
    if txn is not None:
        try:
            txn.End()
        except Exception:
            pass
    _log_snippet(s, code, "ok")
    out = {"ok_python": True, "stdout": buf.getvalue()[-8000:]}
    if result is not None:
        try:
            import json
            json.dumps(result)
            out["result"] = result
        except Exception:
            out["result"] = repr(result)[:4000]
    return out


@tool("system", mutates=False)
def reload_api(s: Session) -> dict:
    """Reload the tool modules from disk (after editing them) without restarting the server."""
    pkg = __name__.rsplit(".", 2)[0]
    names = [n for n in list(sys.modules) if n.startswith(pkg + ".tools.") or n == pkg + ".uidrive"]
    before = set(TOOLS)
    TOOLS.clear()
    reloaded = []
    for n in sorted(names, key=lambda x: (x.endswith("uidrive") is False, x)):
        importlib.reload(sys.modules[n])
        reloaded.append(n.rsplit(".", 1)[-1])
    return {"reloaded": reloaded, "tools": len(TOOLS), "added": sorted(set(TOOLS) - before),
            "removed": sorted(before - set(TOOLS))}


@tool("system", mutates=False)
def list_tools(s: Session, group: str = "") -> dict:
    """Tool names by group (session, documents, sketch, features, inspect, parameters, materials,
    properties, sheet_metal, assembly, drawing, analysis, view, system)."""
    out: dict[str, list[str]] = {}
    for t in TOOLS.values():
        if group and t.group != group:
            continue
        out.setdefault(t.group, []).append(t.name)
    return {"groups": out, "count": sum(len(v) for v in out.values())}


def _pdf_strings(content: bytes) -> list[str]:
    """Lines of text shown by Tj / TJ / ' operators in a PDF content stream (simple fonts). A move with a vertical
    offset (Td, TD, T*, ') starts a new line; a horizontal move inside a line adds a space when it is a large one."""
    import re as _re
    tok_rx = _re.compile(rb"\((?:\\.|[^\\)])*\)|(-?\d*\.?\d+)\s+(-?\d*\.?\d+)\s+T[dD]|T\*|'|\bTJ\b|\bTj\b", _re.S)
    unesc = _re.compile(rb"\\([0-7]{1,3}|.)", _re.S)
    names = {b"n": b"\n", b"r": b"", b"t": b"\t", b"b": b"", b"f": b""}
    tm_rx = _re.compile(rb"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+Tm")
    size_rx = _re.compile(rb"/\S+\s+(-?[\d.]+)\s+Tf")
    if not tm_rx.search(content):          # no absolute placement: one line per text object, in drawing order
        out = []
        for block in _re.findall(rb"BT(.*?)ET", content, _re.S):
            line = "".join(unesc.sub(lambda q: bytes([int(q.group(1), 8) & 255]) if q.group(1)[:1].isdigit()
                                     else names.get(q.group(1), q.group(1)), m.group(0)[1:-1]).decode("latin-1")
                           for m in tok_rx.finditer(block) if m.group(0).startswith(b"("))
            if line.strip():
                out.append(line)
        return out
    pieces = []            # (y, x, text, font size): programs that place every letter on its own are joined up again
    order = 0
    for block in _re.findall(rb"BT(.*?)ET", content, _re.S):
        tm = tm_rx.search(block)
        x, y = (float(tm.group(5)), float(tm.group(6))) if tm else (0.0, 0.0)
        fs = size_rx.search(block)
        size = float(fs.group(1)) if fs else 10.0
        order += 1
        for m in tok_rx.finditer(block):
            tok = m.group(0)
            if tok.startswith(b"("):
                raw = unesc.sub(lambda q: bytes([int(q.group(1), 8) & 255]) if q.group(1)[:1].isdigit()
                                else names.get(q.group(1), q.group(1)), tok[1:-1])
                pieces.append((y, x, raw.decode("latin-1"), size))
                x += len(raw) * size * 0.5
            elif m.group(1) is not None:
                x += float(m.group(1))
                y += float(m.group(2))
            elif tok in (b"T*", b"'"):
                y -= size * 1.2
    lines: dict = {}
    for y, x, text, size in pieces:
        key = next((k for k in lines if abs(k - y) < 0.3 * max(size, 1)), y)
        lines.setdefault(key, []).append((x, text, size))
    out = []
    for key in sorted(lines, reverse=True):
        row = sorted(lines[key])
        text, end = "", None
        for x, t_, size in row:
            if end is not None and x - end > size * 0.9:      # a real gap: a separate word or column
                text += "  "
            text += t_
            end = x + len(t_) * size * 0.55
        out.append(text)
    return out


@tool("system", mutates=False, com=False)
def read_pdf_text(s: Session, path: str, max_chars: int = 20000) -> dict:
    """Text of a PDF (datasheets, standards, drawings from others) using only Python's standard library. Works for most
    PDFs written with simple fonts; scanned pages and some embedded fonts give little or no text.

    max_chars: cut the text off after this many characters
    """
    import os
    import re as _re
    import zlib
    if not os.path.exists(path):
        from ..com import InventorError
        raise InventorError(f"no such file: {path}")
    data = open(path, "rb").read()
    texts = []
    for m in _re.finditer(rb"<<(.*?)>>\s*stream\r?\n", data, _re.S):
        start = m.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        body = data[start:end]
        if b"FlateDecode" in m.group(1):
            try:
                body = zlib.decompress(body)
            except zlib.error:
                try:
                    body = zlib.decompressobj().decompress(body)
                except zlib.error:
                    continue
        elif b"/Filter" in m.group(1):
            continue
        texts += _pdf_strings(body)
    text = "\n".join(t for t in texts if t.strip())
    pages = len(_re.findall(rb"/Type\s*/Page[^s]", data))
    return {"pages": pages, "characters": len(text), "text": text[:max_chars], "truncated": len(text) > max_chars}
