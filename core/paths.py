"""Where the model may write files, and the minimal environment child processes get.

Paths the model chooses are taken literally: no `~` or `$VAR` expansion, and no `..` parts. A relative path lands
in the session's job folder. A write that resolves (symlinks followed) outside the job folders is not refused
outright, since saving a model next to the user's own files is a normal request, but the pre_tool_call hook turns
it into a Hermes approval prompt naming the resolved path, so it never happens without the user saying yes.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .state import STATE


class PathError(ValueError):
    pass


def job_roots() -> list[Path]:
    """The folder writes may go to without asking: this session's own job folder (not other sessions' jobs)."""
    return [STATE.jobs().resolve()]


def resolve(p: str | os.PathLike) -> Path:
    """The real path a model-supplied path refers to. Relative paths are inside the job folder."""
    raw = str(p)
    if "\x00" in raw:
        raise PathError("path contains a NUL character")
    pp = Path(raw)
    if ".." in pp.parts:
        raise PathError(f"'..' is not allowed in paths ({raw}); give a path inside the job folder or a full path")
    if not pp.is_absolute():
        if pp.drive or raw.startswith(("\\", "/")):        # C:foo or \foo on Windows: drive- or root-relative
            raise PathError(f"give a full path or one relative to the job folder, not {raw!r}")
        pp = STATE.jobs() / pp
    return pp.resolve(strict=False)                          # follows symlinks, so a link can't point out


def inside_jobs(path: Path) -> bool:
    for root in job_roots():
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def safe_name(name: str, default: str = "file", limit: int = 80) -> str:
    """A model-supplied name reduced to a plain file name: no folders, drive, '..' or odd characters."""
    base = re.split(r"[\\/:]", str(name or ""))[-1]
    base = re.sub(r"[^\w .()+-]", "_", base).strip(" .")
    return (base or default)[:limit]


# --------------------------------------------------------------------------- which arguments are written
# cad_native tools whose path arguments are only read
NATIVE_READS = {
    "open_document": {"path"}, "place_component": {"path"}, "read_stress_report": {"path"},
    "read_pdf_text": {"path"}, "set_active_project": {"project"}, "create_drawing": {"model_path"},
    "add_base_view": {"model_path", "path"}, "make_part_sheet": {"model_path"}, "make_assembly_sheet": {"model_path"},
}
PATH_KEY = re.compile(r"(?:path|file|filename|folder|dir|directory|output|outfile|dest|destination|target|save_?as|"
                      r"paths|files|outputs)(?:\[\d+\])?$", re.I)
# forge arguments that become part of a file name (sanitised to a plain name before the call)
NATIVE_NAMES = {"analyze_stress": ("name", "study_name"), "analyze_modes": ("name", "study_name"),
                "study_report": ("name",), "create_part": ("name",), "create_assembly": ("name",),
                "create_drawing": ("name",)}


def looks_like_path(v: str) -> bool:
    """An argument value that names a file location by itself: absolute (C:\\..., \\\\server, /...), or starting with
    ~ or holding a $VAR a server might expand. Relative strings like "d1/2" are expressions, not paths."""
    v = v.strip()
    return bool(v) and (os.path.isabs(v) or bool(re.match(r"^[A-Za-z]:", v)) or v.startswith(("/", "\\", "~"))
                        or bool(re.search(r"\$\{?\w", v)) or bool(re.search(r"%\w+%", v)))


def expands(v: str) -> bool:
    """True when a server could turn this into another path (~ or an environment variable)."""
    return v.strip().startswith("~") or bool(re.search(r"\$\{?\w|%\w+%", v))

# cad_* tools: (action or None) -> argument names that are written
PLUGIN_WRITES = {
    "cad_document": {"save": ("path",), "export": ("path",)},
    "cad_drawing": {"render": ("path",), "export_pdf": ("path",)},
}


def _flatten(d: Any, prefix: str = "", depth: int = 0):
    """(key, string) pairs of an argument tree: nested objects become "outer.inner", list items "key[0]"."""
    if depth > 6:
        return
    items_ = d.items() if isinstance(d, dict) else enumerate(d) if isinstance(d, (list, tuple)) else ()
    for k, v in items_:
        key = f"{prefix}[{k}]" if isinstance(d, (list, tuple)) else (f"{prefix}.{k}" if prefix else str(k))
        if isinstance(v, str):
            yield (key if not isinstance(d, (list, tuple)) else key, v)
        else:
            yield from _flatten(v, key, depth + 1)


def written_args(tool_name: str, args: Any) -> list[tuple[str, str]]:
    """(argument, value) pairs of a call that name a file or folder it will write."""
    if not isinstance(args, dict):
        return []
    if tool_name == "cad_native":
        tool = str(args.get("tool", ""))
        targs = args.get("args") if isinstance(args.get("args"), dict) else {}
        reads = NATIVE_READS.get(tool, set())
        names = NATIVE_NAMES.get(tool, ())
        # any argument named like a path, and any other string that looks like one (servers name them freely:
        # filePath, outputPath, dest...); declared reads and the name arguments sanitised in cad_native are left out
        return [(k, v) for k, v in _flatten(targs)
                if v.strip() and k not in reads and k not in names and (PATH_KEY.search(k) or looks_like_path(v))]
    spec = PLUGIN_WRITES.get(tool_name)
    if not spec:
        return []
    keys = spec.get(args.get("action"), ()) or spec.get(None, ())
    return [(k, args[k]) for k in keys if isinstance(args.get(k), str) and args[k].strip()]


def implicit_save(tool_name: str, args: Any) -> bool:
    """A call that saves the open document in place (no path given), so the destination is the document's own file."""
    if not isinstance(args, dict):
        return False
    a = args.get("action")
    if tool_name == "cad_document":
        return (a == "save" and not str(args.get("path") or "").strip()) or (a == "close" and bool(args.get("save")))
    if tool_name == "cad_native":
        t, ta = args.get("tool"), args.get("args") if isinstance(args.get("args"), dict) else {}
        return (t == "save_document" and not str(ta.get("path") or "").strip()) or (t == "close_document" and bool(ta.get("save")))
    return False


def open_document_path() -> str:
    """Where the open document is saved ("" if unsaved or unknown)."""
    try:
        st = STATE.backend().call("status")
    except Exception:
        return ""
    act = st.get("active") if isinstance(st, dict) else None
    act = act if isinstance(act, dict) else (st.get("document") if isinstance(st, dict) else None) or {}
    p = str(act.get("path") or "") if isinstance(act, dict) else ""
    return "" if not p or p.startswith("(") else p


def outside_writes(tool_name: str, args: Any) -> tuple[list[str], list[str]]:
    """(paths outside the job folders this call would write, problems that make it invalid)."""
    outside, bad = [], []
    for key, value in written_args(tool_name, args):
        if tool_name == "cad_native" and not PATH_KEY.search(key) and expands(value):
            outside.append(f"{value} (as given; the CAD server may expand it)")
            continue
        try:
            rp = resolve(value)
        except PathError as e:
            bad.append(f"{key}: {e}")
            continue
        if not inside_jobs(rp):
            outside.append(str(rp))
    if implicit_save(tool_name, args):
        target = open_document_path()
        if target:
            try:
                if not inside_jobs(Path(target).resolve(strict=False)):
                    outside.append(f"{target} (saving the open document over its own file)")
            except OSError:
                outside.append(target)
    return outside, bad


from .childenv import child_env  # noqa: E402,F401  (re-exported)
