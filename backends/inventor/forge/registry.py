"""Tool registry: plain Python functions -> JSON-schema'd tools.

    @tool("part")
    def extrude(s: Session, distance: float = 10.0, operation: str = "join") -> dict:
        '''Extrude the active sketch's profile.

        distance: mm
        operation: join | cut | intersect | new_body
        '''

The first docstring paragraph is the tool description; ``name: text`` lines document parameters.
"""
from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass
from typing import Any, Callable

TOOLS: dict[str, "Tool"] = {}


@dataclass
class Tool:
    name: str
    group: str
    fn: Callable[..., Any]
    description: str
    schema: dict
    mutates: bool
    com: bool = True          # False: never talks to Inventor, safe while it is solving


def _json_type(ann: Any) -> dict:
    origin = typing.get_origin(ann)
    if ann in (inspect.Parameter.empty, Any):
        return {}
    if origin is typing.Union or str(origin) == "types.UnionType":
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        return _json_type(args[0]) if len(args) == 1 else {}
    if ann is bool:
        return {"type": "boolean"}
    if ann is int:
        return {"type": "integer"}
    if ann is float:
        return {"type": "number"}
    if ann is str:
        return {"type": "string"}
    if ann is dict or origin is dict:
        return {"type": "object"}
    if ann is list or origin in (list, tuple):
        args = typing.get_args(ann)
        return {"type": "array", "items": _json_type(args[0]) if args else {}}
    return {}


def _parse_doc(doc: str) -> tuple[str, dict[str, str]]:
    doc = inspect.cleandoc(doc or "")
    paras = doc.split("\n\n")
    desc = paras[0].replace("\n", " ").strip()
    params: dict[str, str] = {}
    current = None
    for line in "\n\n".join(paras[1:]).splitlines():
        stripped = line.strip()
        head, sep, rest = stripped.partition(":")
        if sep and head.isidentifier() and not line.startswith((" ", "\t")):
            current = head
            params[head] = rest.strip()
        elif current and stripped:
            params[current] += " " + stripped
        elif not stripped:
            current = None
        else:
            desc += " " + stripped
    return desc, params


def tool(group: str, name: str | None = None, mutates: bool = True, com: bool = True):
    def deco(fn):
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
        desc, pdocs = _parse_doc(fn.__doc__ or "")
        props, required = {}, []
        for i, (pname, p) in enumerate(sig.parameters.items()):
            if i == 0:          # the Session
                continue
            spec = _json_type(hints.get(pname, p.annotation))
            if pname in pdocs:
                spec["description"] = pdocs[pname]
            if p.default is inspect.Parameter.empty:
                required.append(pname)
            elif p.default is not None and not callable(p.default):
                spec["default"] = list(p.default) if isinstance(p.default, tuple) else p.default
            props[pname] = spec
        schema = {"type": "object", "properties": props}
        if required:
            schema["required"] = required
        tname = name or fn.__name__
        TOOLS[tname] = Tool(tname, group, fn, desc, schema, mutates, com)
        return fn
    return deco


def load_all() -> dict[str, Tool]:
    from .tools import assembly, docs, drawing, features, fea, inspect as _inspect, parametric, sheetmetal, sketch, standard, studies, system  # noqa: F401
    return TOOLS
