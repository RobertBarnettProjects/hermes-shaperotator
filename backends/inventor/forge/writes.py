"""The one place file paths a tool writes to are decided: relative paths go under the session's workdir, the folder
is created, protected folders and read-only files are refused, and nothing is written in read-only mode."""
from __future__ import annotations

import glob
import os

from .com import InventorError


def read_only() -> bool:
    return os.environ.get("INVENTOR_FORGE_READ_ONLY", "").strip().lower() in ("1", "true", "yes")


def protected_roots() -> list[str]:
    """Folders a model must never be written into: Autodesk's library, template and install folders, and any the
    user lists in INVENTOR_FORGE_PROTECTED (separated by ';')."""
    roots = []
    public = os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Documents", "Autodesk")
    for inv in glob.glob(os.path.join(public, "Inventor *")):      # library folders only, not the project workspace
        roots += [os.path.join(inv, sub) for sub in ("Templates", "Design Data", "Content Center Files")]
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        v = os.environ.get(env)
        if v:
            roots.append(os.path.join(v, "Autodesk"))
    roots += [x.strip() for x in os.environ.get("INVENTOR_FORGE_PROTECTED", "").split(";") if x.strip()]
    return [os.path.normcase(os.path.realpath(r_)) for r_ in roots]


def guard(path: str, what: str = "write") -> None:
    """Refuse to write into a protected folder, over a read-only file, or at all in read-only mode."""
    if read_only():
        raise InventorError(f"won't {what} {path}: this server runs read-only (INVENTOR_FORGE_READ_ONLY)")
    rp = os.path.normcase(os.path.realpath(path))
    for root in protected_roots():
        if rp == root or rp.startswith(root.rstrip("\\/") + os.sep):
            raise InventorError(f"won't {what} {path}: it is inside {root} (Autodesk's library, templates or install, "
                                "or a folder listed in INVENTOR_FORGE_PROTECTED). Save a copy elsewhere with a path")
    if os.path.exists(path) and not os.access(path, os.W_OK):
        raise InventorError(f"won't {what} {path}: the file is read-only (checked in, or a library part)")


def output_path(workdir: str, path: str, sub: str, ext: str = "", what: str = "write") -> str:
    """The real path a tool writes to: relative paths under workdir/sub, the extension added when missing, the
    folder created, and the gate applied."""
    if not os.path.isabs(path):
        path = os.path.join(workdir, sub, path) if sub else os.path.join(workdir, path)
    if ext and not path.lower().endswith(ext.lower()):
        path += ext
    guard(path, what)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return path
