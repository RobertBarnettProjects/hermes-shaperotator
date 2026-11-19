"""What a CAD MCP server says about its own tools (the ``cad/*`` keys of each tool's ``_meta``; see
inventor-forge's profile.py for the full list). The plugin asks these questions of whatever server is active, so
any server that publishes a profile gets the same treatment as inventor-forge: previews after geometry changes,
the mouse-and-keyboard notice, code approvals, declared file arguments and waits sized to the call.

A server without a profile still works; each question then has a cautious answer.
"""
from __future__ import annotations

import re
from typing import Any

from .state import STATE

FLOOR_S = 900.0
# when a server hasn't said (not started, or no profile): tools known to drive the mouse and keyboard. Erring this
# way only shows the user a notice they didn't strictly need.
TAKES_INPUT_FALLBACK = {"analyze_stress": True, "analyze_modes": True, "result_at_point": True,
                        "study_report": {"if_args": {"only_this_study": True}}}


def client(start: bool = False):
    """The active backend's MCP client, or None (FreeCAD, or a server not started yet when start is False)."""
    try:
        b = STATE.backend()
    except Exception:
        return None
    c = getattr(b, "client", None)
    if c is None:
        get = getattr(b, "mcp_client", None)
        try:
            c = get(start) if callable(get) else None
        except Exception:                      # not installed, or misconfigured: nothing to ask
            return None
    if c is None:
        return None
    if not start and not getattr(c, "initialized", False):
        return None
    return c


def _tools(start: bool) -> dict[str, dict] | None:
    c = client(start)
    if c is None:
        return None
    try:
        return c.tools
    except Exception:
        return None


def declares(start: bool = False) -> bool:
    """Does the active server publish a cad profile (so its tools' metadata is authoritative)?"""
    c = client(start)
    if c is None:
        return False
    try:
        c.tools                                   # starts it when asked to
    except Exception:
        return False
    return bool(getattr(c, "profile", None))


def meta(tool: str, start: bool = False) -> dict | None:
    """The tool's _meta, or None when the server isn't running, doesn't declare a profile, or doesn't list the tool."""
    tools = _tools(start)
    if tools is None or tool not in tools or not declares():
        return None
    m = tools[tool].get("_meta")
    return m if isinstance(m, dict) else {}


# --------------------------------------------------------------------------- questions
def geometry(tool: str, fallback: set[str] | None = None) -> bool:
    m = meta(tool)
    if m is not None:
        return bool(m.get("cad/geometry"))
    tools = _tools(False) or {}
    if tool in tools:                             # no profile: anything not read-only may have changed the shape
        return not (tools[tool].get("annotations") or {}).get("readOnlyHint", False)
    return tool in (fallback or set())


def takes_input(tool: str, args: dict | None = None, start: bool = True) -> bool:
    m = meta(tool, start)
    v = m.get("cad/takes_input") if m is not None else TAKES_INPUT_FALLBACK.get(tool)
    if isinstance(v, dict):
        want = v.get("if_args") or {}
        a = args or {}
        return all(a.get(k) == x for k, x in want.items())
    return bool(v)


def runs_code(tool: str, description: str = "") -> bool:
    m = meta(tool, start=True)
    if m is not None:
        return bool(m.get("cad/runs_code"))
    return bool(re.search(r"python|exec|eval|script|shell|run_code|execute", f"{tool} {description}", re.I))


def files(tool: str) -> dict[str, list[str]] | None:
    """{"writes", "reads", "names"} declared for the tool, {} when it touches no files, None when undeclared."""
    m = meta(tool, start=True)
    if m is None:
        return None
    f = m.get("cad/files") or m.get("inventor-forge/files") or {}
    return {k: [str(x) for x in f.get(k) or []] for k in ("writes", "reads", "names")}


def budget(tool: str, args: dict, default: float) -> float:
    """How long to wait for the call: the server's cad/budget rule for these arguments, never below default."""
    m = (meta(tool) or {}).get("cad/budget")
    if not isinstance(m, dict):
        return default
    floor = max(default, float(m.get("floor_s") or FLOOR_S))
    arg = m.get("timeout_arg")
    if not arg:
        return floor
    try:
        own = float(args.get(arg, m.get("timeout_default")))
    except (TypeError, ValueError):
        return floor
    n = 1
    for name, fallback in (m.get("repeat_args") or {}).items():
        v = args.get(name)
        n = max(n, len(v) if isinstance(v, (list, tuple)) else int(fallback or 1))
    return max(floor, n * (own + float(m.get("per_item_s", 600))) + float(m.get("extra_s", 300)))


def describe(tool: str) -> str:
    tools = _tools(True) or {}
    return str((tools.get(tool) or {}).get("description", ""))


def summary(args: Any = None) -> dict:
    """For cad_status: whether the active server publishes a profile, and what it says about itself."""
    c = client(False)
    if c is None:
        return {}
    return {"server": c.server_info, "profile": getattr(c, "profile", None) or None,
            "last_progress": getattr(c, "last_progress", "") or None}
