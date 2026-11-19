"""Where a CAD server may write during one tool call, sent with every call as _meta "cad/write_scope".

The job folder is always allowed. A file outside it is allowed only for the call whose approval prompt named it. A
server that enforces the scope (inventor-forge does, at every write, saves of changed referenced documents
included) refuses anything else before writing and names the paths after "cad/needs_approval:"; the next identical
call then asks the user about those paths too.
"""
from __future__ import annotations

import contextvars
import json
import threading
from typing import Any

NEEDS_MARK = "cad/needs_approval:"
_CURRENT: contextvars.ContextVar = contextvars.ContextVar("shaperotator_write_scope", default=None)
_LOCK = threading.Lock()
_STAGED: dict[str, list[str]] = {}        # call key -> outside files its approval prompt named
_NEEDED: dict[str, list[str]] = {}        # call key -> files the server refused last time
# the same, by tool name: the hook and the handler may see the arguments normalised differently, and the approval
# prompt for a tool immediately precedes its handler, so the tool's latest entry is the fallback
_STAGED_TOOL: dict[str, list[str]] = {}
_NEEDED_TOOL: dict[str, list[str]] = {}


def _tool(key: str) -> str:
    return key.split("\0", 1)[0]


def call_key(tool_name: str, args: Any) -> str:
    return str(tool_name) + "\0" + json.dumps(args, sort_keys=True, default=str)


def stage(key: str, files: list[str]) -> None:
    """Called while the approval prompt is prepared: the files it names, for that call alone."""
    with _LOCK:
        _STAGED[key] = _STAGED_TOOL[_tool(key)] = list(dict.fromkeys(files))


def needed(key: str) -> list[str]:
    with _LOCK:
        return list(_NEEDED.get(key) or _NEEDED_TOOL.get(_tool(key)) or [])


def default_scope() -> dict:
    from .paths import job_roots
    return {"dirs": [str(p) for p in job_roots()], "files": []}


def current() -> dict:
    """The scope for a request being sent now: the running tool call's, else the job folder alone."""
    sc = _CURRENT.get()
    return sc if sc is not None else default_scope()


def _strings(v: Any, depth: int = 0):
    if depth > 8:
        return
    if isinstance(v, str):
        yield v
        if NEEDS_MARK in v and v.lstrip()[:1] in "{[":
            try:
                yield from _strings(json.loads(v), depth + 1)
            except ValueError:
                pass
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x, depth + 1)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x, depth + 1)


def parse_needed(out: Any) -> list[str]:
    """The paths a server's refusal named, wherever in a tool result (a JSON string or object) the refusal sits."""
    found: list[str] = []
    for s in _strings(out):
        if NEEDS_MARK not in s:
            continue
        tail = s.split(NEEDS_MARK, 1)[1].strip()
        try:
            got, _ = json.JSONDecoder().raw_decode(tail)
        except ValueError:
            continue
        if isinstance(got, list):
            found += [str(x) for x in got if isinstance(x, str)]
    return list(dict.fromkeys(found))


def scoped(tool_name: str, handler):
    """Wrap a tool handler: the call runs with its own scope (job folder + files its approval named), and a refusal
    from the server is remembered so the same call asks about those paths next time."""
    def run(args: Any = None, **kw: Any):
        key = call_key(tool_name, args)
        with _LOCK:
            files = _STAGED.pop(key, None)
            fallback = _STAGED_TOOL.pop(tool_name, [])
            files = fallback if files is None else files
        sc = default_scope()
        sc["files"] = files
        mark = _CURRENT.set(sc)
        try:
            out = handler(args, **kw)
        finally:
            _CURRENT.reset(mark)
        got = parse_needed(out)
        with _LOCK:
            if got:
                _NEEDED[key] = _NEEDED_TOOL[tool_name] = got
            else:
                _NEEDED.pop(key, None)
                _NEEDED_TOOL.pop(tool_name, None)
        return out
    run.__name__ = getattr(handler, "__name__", tool_name)
    run.__doc__ = getattr(handler, "__doc__", None)
    return run
