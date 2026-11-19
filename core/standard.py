"""The drafting/engineering standard each chat works to. The agent asks the user once at the start of a CAD chat;
the choice is kept per Hermes session (on disk, so it survives a restart) and every drawing follows it.

The table (standards_table.py) is the same one inventor-forge uses, kept here so FreeCAD-only installs need nothing
else.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import standards_table as T
from .state import STATE, _data_dir

StandardError = T.StandardError
_LOCK = threading.Lock()


def _file() -> Path:
    return _data_dir() / "standards.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def configured() -> str:
    """A standard fixed in the plugin settings (plugins.hermes-shaperotator.standard), or ""."""
    v = str(STATE.settings.get("standard") or "").strip()
    try:
        return T.key(v) if v else ""
    except StandardError:
        return ""


def chosen(session_id: str | None = None) -> dict | None:
    """{"standard", "drawing_units"} chosen in this chat, else the configured one, else None (not asked yet)."""
    sid = session_id if session_id is not None else STATE.session_id
    got = _load().get(sid or "_")
    if got:
        return got
    c = configured()
    return {"standard": c, "drawing_units": "mm", "from": "plugin settings"} if c else None


def choose(standard: str, drawing_units: str = "mm", session_id: str | None = None) -> dict:
    k = T.key(standard)
    u = (drawing_units or "mm").lower()
    if u not in ("mm", "in"):
        raise StandardError("drawing_units must be mm or in")
    if u == "in" and k != "ASME":
        raise StandardError("inch drawings are only offered with ASME")
    sid = (session_id if session_id is not None else STATE.session_id) or "_"
    with _LOCK:
        data = _load()
        data[sid] = {"standard": k, "drawing_units": u}
        if len(data) > 500:                      # keep the file small: oldest chats drop off
            data = dict(list(data.items())[-500:])
        tmp = _file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(_file())                     # atomic: a crash mid-write can't lose every chat's choice
    return data[sid]


def for_call(explicit: str = "") -> str:
    """The standard a drawing call should use: one named in the call, else this chat's choice, else ""."""
    if explicit:
        return T.key(explicit)
    c = chosen()
    return c["standard"] if c else ""


def options() -> list[dict]:
    return [T.summary(k) for k in T.STANDARDS]


ASK = ("No standard has been chosen for this chat. Ask the user once which standard to work to (use the clarify "
       "tool if you have it, with these options: " + ", ".join(f"{k} ({v['name']})" for k, v in T.STANDARDS.items())
       + "), then call cad_standard(action='set'). It is kept for the rest of the chat.")
