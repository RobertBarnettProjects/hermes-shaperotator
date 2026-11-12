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
    "add_base_view": {"model_path", "path", "model"}, "make_part_sheet": {"model_path", "model"},
    "make_assembly_sheet": {"model_path", "model"},
    # these name an open document or a file they only read
    "activate_document": {"name", "path"}, "close_document": {"name"}, "copy_parameters": {"source"},
    "link_parameters": {"source"}, "derive_part": {"source", "path"}, "insert_derived": {"source", "path"},
}
# words that make an argument a file location when they end its name: csv_path, filePath, outputDir, dest, saveAs
PATH_WORDS = {"path", "paths", "file", "files", "filename", "filenames", "folder", "dir", "directory", "output", "outputs",
              "outfile", "dest", "destination", "target", "location", "uri", "url", "saveas"}
# arguments that hold free text, never a location
TEXT_KEYS = {"notes", "note", "text", "texts", "title", "description", "label", "comment", "expression", "value", "fields",
             "message", "prompt", "content", "code", "query"}
# forge arguments that become part of a file name (sanitised to a plain name before the call)
NATIVE_NAMES = {"analyze_stress": ("name", "study_name"), "analyze_modes": ("name", "study_name"),
                "study_report": ("name",), "create_part": ("name",), "create_assembly": ("name",),
                "create_drawing": ("name",)}
# forge tools that save the open document where it is (no path argument)
SAVES_IN_PLACE = {"run_study_cases"}


def _parts(key: str) -> list[str]:
    return re.findall(r"[^.\[\]]+", key)


def path_named(key: str) -> bool:
    """Is the argument (last name part of a nested key) named like a file location? Word-based: "profile" is not."""
    names = [n for n in _parts(key) if not n.isdigit()]
    if not names:
        return False
    name = names[-1]
    if name.replace("_", "").lower() in PATH_WORDS:
        return True
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)
    return bool(words) and words[-1].lower() in PATH_WORDS


def text_named(key: str) -> bool:
    return any(n.lower() in TEXT_KEYS for n in _parts(key) if not n.isdigit())


def looks_like_path(v: str) -> bool:
    """A value that names a file location by itself: C:\\... or C:/..., \\\\server\\..., /dir..., ~/..., $VAR/...,
    %VAR%\\..., file:// or a relative path that climbs (../). Text such as "C: bracket", "/ 2 places", "~3 kg",
    "Cost $5" or "d1/2" is not a path."""
    v = v.strip()
    return bool(
        re.match(r"^[A-Za-z]:[\\/]", v) or re.match(r"^(\\\\|//)[^\\/\s]", v) or re.match(r"^/[\w.-]", v)
        or re.match(r"^~[\\/]", v) or re.match(r"^\$\{?[A-Za-z_]\w*\}?[\\/]", v) or re.match(r"^%\w+%[\\/]", v)
        or re.match(r"^file:", v, re.I) or re.search(r"(^|[\\/])\.\.([\\/]|$)", v))


def expands(v: str) -> bool:
    """True when a server could turn this into another path (~ or an environment variable)."""
    return bool(re.match(r"^~[\\/]?", v.strip()) or re.search(r"\$\{?[A-Za-z_]\w*|%\w+%", v))


# cad_* tools: (action or None) -> argument names that are written
PLUGIN_WRITES = {
    "cad_document": {"save": ("path",), "export": ("path",)},
    "cad_drawing": {"render": ("path",), "export_pdf": ("path",)},
}


def _flatten(d: Any, prefix: str = "", depth: int = 0):
    """(key, string) pairs of an argument tree: nested objects become "outer.inner", list items "key[0]"."""
    if depth > 20:
        yield prefix or "args", "/(arguments nested too deeply to check)"    # looks like a path: prompts
        return
    items_ = d.items() if isinstance(d, dict) else enumerate(d) if isinstance(d, (list, tuple)) else ()
    for k, v in items_:
        key = f"{prefix}[{k}]" if isinstance(d, (list, tuple)) else (f"{prefix}.{k}" if prefix else str(k))
        if isinstance(v, str):
            yield key, v
        else:
            yield from _flatten(v, key, depth + 1)


def set_nested(d: Any, key: str, value: str) -> None:
    """Set the string at a flattened key ("export.path", "files[0]") in an argument tree."""
    toks = re.findall(r"[^.\[\]]+|\[\d+\]", key)
    cur = d
    for t in toks[:-1]:
        cur = cur[int(t[1:-1])] if t.startswith("[") else cur[t]
    last = toks[-1]
    if last.startswith("["):
        cur[int(last[1:-1])] = value
    else:
        cur[last] = value


def written_args(tool_name: str, args: Any) -> list[tuple[str, str]]:
    """(argument, value) pairs of a call that name a file location (written, or read by a tool not known to read)."""
    if not isinstance(args, dict):
        return []
    if tool_name == "cad_native":
        tool = str(args.get("tool", ""))
        targs = args.get("args") if isinstance(args.get("args"), dict) else {}
        reads = NATIVE_READS.get(tool, set())
        names = NATIVE_NAMES.get(tool, ())
        # arguments named like a location, and any other value that looks like one (servers name them freely:
        # filePath, outputPath, dest...); declared reads, free-text arguments and sanitised names are left out
        return [(k, v) for k, v in _flatten(targs)
                if v.strip() and k not in reads and k not in names and not text_named(k)
                and (path_named(k) or looks_like_path(v))]
    spec = PLUGIN_WRITES.get(tool_name)
    if not spec:
        return []
    keys = spec.get(args.get("action"), ()) or spec.get(None, ())
    return [(k, args[k]) for k in keys if isinstance(args.get(k), str) and args[k].strip()]


def implicit_save(tool_name: str, args: Any) -> bool:
    """A call that saves an open document where it is (no path given), so the destination is that document's file."""
    if not isinstance(args, dict):
        return False
    a = args.get("action")
    if tool_name == "cad_document":
        return (a == "save" and not str(args.get("path") or "").strip()) or (a == "close" and bool(args.get("save")))
    if tool_name == "cad_native":
        t, ta = args.get("tool"), args.get("args") if isinstance(args.get("args"), dict) else {}
        return ((t == "save_document" and not str(ta.get("path") or "").strip())
                or (t == "close_document" and bool(ta.get("save"))) or t in SAVES_IN_PLACE)
    return False


UNKNOWN = "?"


def open_document_path() -> str:
    """Where the open document is saved: "" if unsaved, UNKNOWN if the CAD program couldn't say."""
    if STATE.backend_name not in STATE.backends:          # not started: don't launch a CAD program from the hook
        return UNKNOWN
    import concurrent.futures as cf
    pool = cf.ThreadPoolExecutor(max_workers=1)
    try:
        st = pool.submit(STATE.backend().call, "status").result(timeout=10)   # a busy CAD program: don't stall
    except Exception:
        return UNKNOWN
    finally:
        pool.shutdown(wait=False)
    act = st.get("active") if isinstance(st, dict) else None
    act = act if isinstance(act, dict) else (st.get("document") if isinstance(st, dict) else None) or {}
    p = str(act.get("path") or "") if isinstance(act, dict) else ""
    return "" if not p or p.startswith("(") else p


def outside_writes(tool_name: str, args: Any) -> tuple[list[str], list[str]]:
    """(locations outside the job folder this call would use, problems that make it invalid)."""
    outside, bad = [], []
    for key, value in written_args(tool_name, args):
        named = tool_name != "cad_native" or path_named(key)
        if tool_name == "cad_native" and (re.match(r"^file:", value.strip(), re.I) or expands(value)):
            outside.append(f"{value} (as given; the CAD server resolves it)")
            continue
        try:
            rp = resolve(value)
        except PathError as e:
            if named:
                bad.append(f"{key}: {e}")                 # a path argument with '..': refuse
            else:
                outside.append(f"{value} (as given, argument {key})")
            continue
        if not inside_jobs(rp):
            outside.append(str(rp))
    if implicit_save(tool_name, args):
        ta = args.get("args") if isinstance(args.get("args"), dict) else {}
        if tool_name == "cad_native" and ta.get("name"):
            outside.append(f"the saved file of the open document {ta['name']!r} (saved where it is)")
        else:
            target = open_document_path()
            if target == UNKNOWN:
                outside.append("the open document's own file (the CAD program didn't say where that is)")
            elif target:
                try:
                    if not inside_jobs(Path(target).resolve(strict=False)):
                        outside.append(f"{target} (saving the open document over its own file)")
                except OSError:
                    outside.append(target)
    return outside, bad


from .childenv import child_env  # noqa: E402,F401  (re-exported)
