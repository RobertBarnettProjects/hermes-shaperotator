"""Autodesk Inventor backend (Windows only, COM) over inventor-forge, this project's Inventor MCP server.

inventor-forge runs as its own process, from its own Python environment: installed on first use when the user
approves (``cad_backend(action="install", backend="inventor")``, see core/installer.py), or any command configured
under ``mcp_servers`` with ``dialect: inventor-forge``. This adapter maps hermes-shaperotator's backend-neutral ops
onto forge tools, and exposes every other forge tool through the ``native`` op (``cad_native`` in Hermes): sheet
metal, assemblies, materials, mass properties, drawings, Inventor-side stress/modal analysis...

Nothing of forge is imported here: what the plugin needs to know about each tool (geometry changes, mouse use, file
arguments, how long to wait) comes from the server's own cad profile (core/server_profile.py).
"""
from __future__ import annotations

import sys
from typing import Any

from ...core.backend import BackendError, CadBackend


class _McpForge:
    """inventor-forge over MCP: the client plus forge's connect / re-sync rules."""

    def __init__(self, client):
        self.client = client

    @property
    def tools(self) -> dict:
        return self.client.tools

    def call(self, name: str, args: dict) -> dict:
        from ...core import server_profile
        self.client.start()                       # before the budget: it comes from the server's tool list
        if name == "connect":
            self.client.wait_for_pending_calls()
        wait = server_profile.budget(name, args, self.client.timeout, c=self.client)
        meta = self.client.tools.get(name) or {}
        mutating = False if name == "connect" else not (meta.get("annotations") or {}).get("readOnlyHint", False)
        result = self.client.call(name, args, mutating=mutating, timeout=wait)
        if name == "connect":
            self.client.call("status", {}, mutating=False)
            self.client.uncertain = False
        return result

    def close(self) -> None:
        self.client.close()


class InventorBackend(CadBackend):
    name = "inventor"
    family = "inventor"
    supported = frozenset({
        "connect", "status", "new_part", "new_assembly", "open", "save", "close", "export",
        "sketch", "rect", "circle", "line", "polygon", "profile",
        "extrude", "revolve", "hole", "fillet", "chamfer", "pattern_circular", "delete_feature",
        "get_parameters", "set_parameter", "add_parameter",
        "inspect", "list_edges", "list_faces", "find_edge", "find_face", "render",
        "transaction",
        "drawing_new", "drawing_part_sheet", "drawing_assembly_sheet", "drawing_check", "drawing_export_pdf",
        "drawing_render", "drawing_set_properties",
        "python", "native",
    })

    def __init__(self, settings: dict[str, Any], server: dict | None = None):
        super().__init__(settings)
        self._forge: _McpForge | None = None
        self.server = server          # an mcp_servers entry; None = the add-on installed by core/installer.py

    def available(self) -> tuple[bool, str]:
        if sys.platform != "win32":
            return False, "Autodesk Inventor runs on Windows only"
        from ...core import installer
        found = installer.detect("inventor")
        if not found.get("found"):
            return False, found.get("detail") or "Autodesk Inventor is not installed"
        if self.server:
            return True, f"{found.get('version')}, inventor-forge from mcp_servers ({' '.join(self.server['command'])})"
        st = installer.server_status("inventor")
        if not st["installed"]:
            return False, f"{found.get('version')} found; {st['detail']}"
        return True, f"{found.get('version')}, inventor-forge {st.get('version')}" + (
            "" if st.get("current") else f" ({st['detail']})")

    def _config(self) -> dict:
        if self.server:
            return self.server
        from ...core import installer
        from ...core.state import STATE
        try:
            return installer.server_config("inventor", {"job_dir": str(STATE.jobs())})
        except installer.InstallError as e:
            raise BackendError(str(e)) from e

    def mcp_client(self, start: bool = False):
        """The forge client (created, and started when asked); None when forge isn't installed."""
        if self._forge is None:
            if not start:
                return None
            from ...core.mcp_client import McpClient
            s = self._config()
            env = dict(s.get("env") or {})
            env.setdefault("INVENTOR_FORGE_VISIBLE", "1" if self.settings.get("inventor_visible", True) else "0")
            client = McpClient(s["command"], env=env, cwd=s.get("cwd"), timeout=float(s.get("timeout", 900)))
            with self._lock:                  # a live poll and a tool call must not each make a client
                if self._forge is None:
                    self._forge = _McpForge(client)
        return self._forge.client

    @property
    def forge(self) -> _McpForge:
        self.mcp_client(start=True)
        assert self._forge is not None
        return self._forge

    @property
    def client(self):
        """The started client, if any (hooks ask without starting a server)."""
        return self._forge.client if self._forge is not None else None

    def tool_names(self) -> list[str]:
        return sorted(self.forge.tools)

    def _f(self, tool: str, **args: Any) -> dict:
        from ...core.mcp_client import McpError
        try:
            return self.forge.call(tool, {k: v for k, v in args.items() if v is not None})
        except McpError as e:
            raise BackendError(str(e)) from e
        except BackendError:
            raise
        except Exception as e:  # noqa: BLE001
            raise BackendError(f"Inventor {tool} failed: {type(e).__name__}: {e}") from e

    def shutdown(self) -> None:
        if self._forge is not None:
            self._forge.close()
            self._forge = None

    # ------------------------------------------------------------------ op table
    def _call(self, op: str, **k: Any) -> dict:
        f = self._f
        idx = int(k.get("sketch_index") or 0)
        if op == "connect":
            return f("connect")
        if op == "status":
            st = f("status")
            return {"connected": True, "document": st.get("active")}
        if op == "new_part":
            return f("create_part", name=k.get("name") or "", sheet_metal=bool(k.get("sheet_metal", False)))
        if op == "new_assembly":
            return f("create_assembly")
        if op == "open":
            return f("open_document", path=k["path"])
        if op == "save":
            return f("save_document", path=k.get("path") or "")
        if op == "close":
            return f("close_document", save=bool(k.get("save", False)))
        if op == "export":
            return f("export_document", path=k["path"], format=k.get("format") or "")
        if op == "sketch":
            return f("create_sketch", plane=k.get("plane", "XY"), offset=float(k.get("offset") or 0),
                     face_index=int(k.get("face_index") or 0))
        if idx and op in ("rect", "circle", "line", "polygon", "profile"):
            f("select_sketch", index=idx)
        if op == "rect":
            r = f("draw_rectangle", width=k.get("width", 100), height=k.get("height", 50), x=k.get("x", 0),
                  y=k.get("y", 0))
            return {**r, "message": r.get("drew", "rectangle")}
        if op == "circle":
            r = f("draw_circle", diameter=k.get("diameter", 50), x=k.get("x", 0), y=k.get("y", 0),
                  construction=bool(k.get("construction", False)))
            return {**r, "message": r.get("drew", "circle")}
        if op == "line":
            r = f("draw_line", x1=k.get("x1", 0), y1=k.get("y1", 0), x2=k.get("x2", 100), y2=k.get("y2", 0),
                  construction=bool(k.get("construction", False)))
            return {**r, "message": r.get("drew", "line")}
        if op == "polygon":
            r = f("draw_polygon", sides=int(k.get("sides", 6)), radius=k.get("radius", 25), x=k.get("x", 0),
                  y=k.get("y", 0))
            return {**r, "message": r.get("drew", "polygon")}
        if op == "profile":
            r = f("draw_closed_profile", points=k["points"])
            return {**r, "message": r.get("drew", "profile")}
        if op == "extrude":
            through = str(k.get("extent_type", "distance")).lower() in ("through_all", "all", "through")
            direction = k.get("direction", "positive")
            if k.get("operation") == "cut" and direction == "positive":
                direction = "auto"      # forge picks the side that actually removes material
            return f("extrude", distance=k.get("distance", 10), operation=k.get("operation", "join"),
                     direction=direction, through_all=through, sketch_index=idx)
        if op == "revolve":
            return f("revolve", axis=k.get("axis", "Y"), angle=k.get("angle", 360), operation=k.get("operation", "join"),
                     sketch_index=idx)
        if op == "hole":
            tap = ""
            if k.get("tapped"):
                pitch = k.get("pitch") or {3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75, 16: 2.0,
                                           20: 2.5, 24: 3.0}.get(int(round(k.get("diameter", 10))), 1.5)
                tap = f"M{k.get('diameter', 10):g}x{pitch:g}"
            return f("hole", x=k.get("x", 0), y=k.get("y", 0), diameter=k.get("diameter", 10),
                     depth=k.get("depth", 0), tap=tap)
        if op == "fillet":
            return f("fillet", radius=k.get("radius", 2), edges=[int(i) for i in k.get("edge_indices", [1])])
        if op == "chamfer":
            return f("chamfer", distance=k.get("distance", 2), edges=[int(i) for i in k.get("edge_indices", [1])])
        if op == "pattern_circular":
            return f("circular_pattern", features=[str(k["feature_index"])], count=int(k.get("count", 8)),
                     angle=k.get("angle", 360), axis=k.get("axis", "Z"))
        if op == "delete_feature":
            return f("delete_feature", features=[str(i) for i in k["indices"]])
        if op == "get_parameters":
            p = f("get_parameters")
            return {"parameters": p.get("model", []) + p.get("user", [])}
        if op == "set_parameter":
            return f("set_parameter", name=k["name"], expression=str(k["expression"]))
        if op == "add_parameter":
            return f("add_parameter", name=k["name"], expression=str(k["expression"]), units=k.get("units", "mm"))
        if op == "inspect":
            return f("inspect")
        if op == "list_edges":
            return f("list_edges", min_length=k.get("min_length", 0), max_count=k.get("max_count", 100))
        if op == "list_faces":
            return f("list_faces", min_area=k.get("min_area", 0), max_count=k.get("max_count", 80))
        if op == "find_edge":
            return f("find_edge", x=k.get("x"), y=k.get("y"), z=k.get("z"), tolerance=k.get("tolerance", 0.5))
        if op == "find_face":
            return f("find_face", x=k.get("x"), y=k.get("y"), z=k.get("z"), min_area=k.get("min_area", 0))
        if op in ("render", "drawing_render"):
            path = k["path"]
            try:
                kind = f("status").get("active", {}).get("type")
            except BackendError:
                kind = None
            if kind == "drawing":
                return f("render_sheet", path=path)
            return f("screenshot", path=path, view=k.get("view", "iso"))
        if op == "transaction":
            return f("transaction", action=k.get("action", "begin"), name=k.get("name", "hermes-shaperotator"))
        if op == "drawing_new":
            if k.get("standard"):
                f("set_standard", standard=k["standard"], drawing_units=k.get("drawing_units") or "mm")
            r = f("create_drawing", standard=k.get("standard", ""), sheet_size=k.get("sheet_size", "A3"),
                  orientation=k.get("orientation", "landscape"))
            if k.get("properties"):
                r["properties"] = f("set_title_block_field", fields=k["properties"])
            return r
        if op == "drawing_set_properties":
            r = f("set_title_block_field", fields=k.get("properties") or {})
            return {"properties": r.get("set", {}), **r}
        if op in ("drawing_part_sheet", "drawing_assembly_sheet") and k.get("standard"):
            f("set_standard", standard=k["standard"], drawing_units=k.get("drawing_units") or "mm")
        if op == "drawing_part_sheet":
            return f("make_part_sheet", model=k.get("model_path", ""), views=k.get("views", "front,top,right,iso"),
                     scale=float(k.get("scale") or 0), sheet_size=k.get("sheet_size", "A3"),
                     standard=k.get("standard") or self.settings.get("standard") or "")
        if op == "drawing_assembly_sheet":
            return f("make_assembly_sheet", model=k.get("model_path", ""), scale=float(k.get("scale") or 0),
                     sheet_size=k.get("sheet_size", "A3"), standard=k.get("standard") or self.settings.get("standard") or "")
        if op == "drawing_check":
            r = f("check_drawing")
            return {"report": r["report"], "issues": r["total"], "issue_list": r["issues"]}
        if op == "drawing_export_pdf":
            return f("export_pdf", path=k["path"])
        if op == "python":
            r = f("execute_python", code=k["code"])
            if not r.get("ok_python", True):
                return {"output": r.get("stdout", ""), "error": r.get("error", "")}
            out = r.get("stdout") or ""
            if "result" in r:
                out += ("\n" if out else "") + repr(r["result"])
            return {"output": out or "(ok, no output)"}
        if op == "native":
            tool = k.get("tool", "")
            if tool not in self.forge.tools:
                raise BackendError(f"unknown Inventor tool {tool!r}; available: {', '.join(self.tool_names())}")
            return f(tool, **(k.get("args") or {}))
        raise BackendError(f"{op} not implemented for Inventor")
