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
    """The folders writes may go to without asking: this session's job folder and the job tree it sits in."""
    job = STATE.jobs()
    return [job.resolve(), job.parent.resolve()]


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


# --------------------------------------------------------------------------- which arguments are written
# cad_native tools whose path arguments are only read
NATIVE_READS = {
    "open_document": {"path"}, "place_component": {"path"}, "read_stress_report": {"path"},
    "read_pdf_text": {"path"}, "set_active_project": {"project"}, "create_drawing": {"model_path"},
    "add_base_view": {"model_path", "path"}, "make_part_sheet": {"model_path"}, "make_assembly_sheet": {"model_path"},
}
PATH_KEY = re.compile(r"(?:^|_)(?:path|file|filename|folder|dir|directory|output)$", re.I)

# cad_* tools: (action or None) -> argument names that are written
PLUGIN_WRITES = {
    "cad_document": {"save": ("path",), "export": ("path",)},
    "cad_drawing": {"render": ("path",), "export_pdf": ("path",)},
}


def written_args(tool_name: str, args: Any) -> list[tuple[str, str]]:
    """(argument, value) pairs of a call that name a file or folder it will write."""
    if not isinstance(args, dict):
        return []
    if tool_name == "cad_native":
        tool = str(args.get("tool", ""))
        targs = args.get("args") if isinstance(args.get("args"), dict) else {}
        reads = NATIVE_READS.get(tool, set())
        return [(k, v) for k, v in targs.items()
                if isinstance(v, str) and v.strip() and PATH_KEY.search(k) and k not in reads]
    spec = PLUGIN_WRITES.get(tool_name)
    if not spec:
        return []
    keys = spec.get(args.get("action"), ()) or spec.get(None, ())
    return [(k, args[k]) for k in keys if isinstance(args.get(k), str) and args[k].strip()]


def outside_writes(tool_name: str, args: Any) -> tuple[list[str], list[str]]:
    """(paths outside the job folders this call would write, problems that make it invalid)."""
    outside, bad = [], []
    for key, value in written_args(tool_name, args):
        try:
            rp = resolve(value)
        except PathError as e:
            bad.append(f"{key}: {e}")
            continue
        if not inside_jobs(rp):
            outside.append(str(rp))
    return outside, bad


from .childenv import child_env  # noqa: E402,F401  (re-exported)
