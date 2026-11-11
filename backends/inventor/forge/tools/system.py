"""Escape hatch and maintenance: run Python against the live Inventor session, reload the tools."""
from __future__ import annotations

import contextlib
import importlib
import io
import sys
import traceback

from ..com import MM, K, items, typed_call
from ..registry import TOOLS, tool
from ..session import Session

_NS: dict = {}


@tool("system")
def execute_python(s: Session, code: str) -> dict:
    """Run Python in a persistent namespace with the live Inventor objects: app, doc, cdef (component
    definition or None), tg (TransientGeometry), to (TransientObjects), K (all Inventor enum constants),
    items(collection), typed_call(obj, method, ...) for [out]-parameter methods, MM (=0.1, mm->cm).
    Inventor API units are cm and radians. The value of a final expression is returned; print() output too.
    Use it for anything the dedicated tools don't cover.
    """
    app = s.app
    doc = app.ActiveDocument
    cdef = None
    if doc is not None:
        try:
            cdef = doc.ComponentDefinition
        except Exception:
            cdef = None
    _NS.update({"app": app, "doc": doc, "cdef": cdef, "tg": app.TransientGeometry, "to": app.TransientObjects,
                "K": K, "items": items, "typed_call": typed_call, "MM": MM, "session": s})
    buf = io.StringIO()
    result = None
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
        return {"ok_python": False, "stdout": buf.getvalue()[-4000:], "error": traceback.format_exc(limit=6)[-3000:]}
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
