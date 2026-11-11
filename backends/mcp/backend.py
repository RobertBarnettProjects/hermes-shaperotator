"""Any CAD program behind an MCP server.

Configure servers in the plugin settings (``plugins.hermes-shaperotator.mcp_servers`` in config.yaml, or the
``HERMES_SHAPEROTATOR_MCP_SERVERS`` environment variable as JSON)::

    mcp_servers:
      inventor-mcp:                      # inventor-forge over MCP (same features as the built-in backend)
        command: [python, -m, inventor_forge.server]
        dialect: inventor-forge
      my-cad:                            # any other CAD MCP server
        command: [node, my-cad-server.js]
        ops:                             # hermes-shaperotator op -> that server's tool (+ optional argument renames)
          status: get_status
          new_part: {tool: create_document, args: {name: title}, fixed: {kind: part}}
          extrude: {tool: pad, args: {distance: length}}
          inspect: get_model_info

Then ``cad_backend backend=<name>``. Ops without a mapping fall back to a server tool with the same name;
every server tool is reachable through ``cad_native``. The workbench (plans, FEA verification, history,
artifacts, viewport) sits above this contract, so swapping the server keeps all of it.
"""
from __future__ import annotations

import shutil
import math
from typing import Any

from ...core.backend import OPS, BackendError, CadBackend
from ...core.mcp_client import McpClient, McpError


def make_mcp_backend(name: str, cfg: dict, settings: dict) -> CadBackend:
    if not isinstance(cfg, dict) or not cfg.get("command"):
        raise ValueError(f"mcp_servers.{name} needs a 'command' list")
    if not isinstance(cfg["command"], list) or any(not isinstance(v, str) or not v for v in cfg["command"]):
        raise ValueError(f"mcp_servers.{name}.command must be an argv array of strings")
    timeout = float(cfg.get("timeout", 900))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"mcp_servers.{name}.timeout must be finite and positive")
    if cfg.get("env") is not None and (not isinstance(cfg["env"], dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in cfg["env"].items())):
        raise ValueError(f"mcp_servers.{name}.env must be a string mapping")
    ops = cfg.get("ops", {})
    if not isinstance(ops, dict):
        raise ValueError(f"mcp_servers.{name}.ops must be a mapping")
    for op, mapping in ops.items():
        if isinstance(mapping, str) and mapping:
            continue
        if not isinstance(mapping, dict) or not isinstance(mapping.get("tool"), str) or not mapping["tool"]:
            raise ValueError(f"invalid operation mapping for {op}")
        if not isinstance(mapping.get("args", {}), dict) or not isinstance(mapping.get("fixed", {}), dict):
            raise ValueError(f"invalid argument mapping for {op}")
        if any(not isinstance(v, (str, type(None))) for v in mapping.get("args", {}).values()):
            raise ValueError(f"invalid argument rename for {op}")
    if str(cfg.get("dialect") or cfg.get("family") or "").lower() in ("inventor-forge", "inventor"):
        from ..inventor.backend import InventorBackend
        b = InventorBackend(settings, server=cfg)
        b.name = name
        b.available = lambda: _command_available(cfg)      # no COM probe: the server may be remote
        return b
    return McpBackend(name, cfg, settings)


def _command_available(cfg: dict) -> tuple[bool, str]:
    exe = cfg["command"][0]
    if shutil.which(exe) is None:
        return False, f"MCP server command not found: {exe}"
    return True, f"MCP server: {' '.join(map(str, cfg['command']))}"


class McpBackend(CadBackend):
    family = "generic"

    def __init__(self, name: str, cfg: dict, settings: dict[str, Any]):
        super().__init__(settings)
        self.name = name
        self.cfg = cfg
        self.family = str(cfg.get("family") or "generic")
        self.ops: dict[str, Any] = dict(cfg.get("ops") or {})
        self.client = McpClient(cfg["command"], env=cfg.get("env"), cwd=cfg.get("cwd"),
                                timeout=float(cfg.get("timeout", 900)))

    @property
    def supported(self) -> frozenset[str]:          # type: ignore[override]
        try:
            names = set(self.client.tools)
        except McpError:
            names = set()
        mapped = {op for op, m in self.ops.items() if op in OPS}
        same = {op for op in OPS if op in names}
        return frozenset(mapped | same | {"connect", "status", "native"})

    def available(self) -> tuple[bool, str]:
        return _command_available(self.cfg)

    def shutdown(self) -> None:
        self.client.close()

    def tool_names(self) -> list[str]:
        return sorted(self.client.tools)

    def _route(self, op: str, kwargs: dict) -> tuple[str, dict]:
        m = self.ops.get(op, op)
        if isinstance(m, str):
            return m, kwargs
        renames = m.get("args") or {}
        out = {renames.get(k, k): v for k, v in kwargs.items() if renames.get(k, k) is not None}
        out.update(m.get("fixed") or {})
        return m["tool"], out

    def _call(self, op: str, **k: Any) -> dict:
        try:
            if op == "native":
                tool = k.get("tool", "")
                if tool not in self.client.tools:
                    raise BackendError(f"unknown {self.name} tool {tool!r}; available: {', '.join(self.tool_names())}")
                read_ops = {"status", "inspect", "get_parameters", "list_edges", "list_faces", "find_edge", "find_face"}
                read_tools = {self._route(op, {})[0] for op in read_ops}
                return self.client.call(tool, k.get("args") or {}, mutating=False if tool in read_tools else None)
            if op == "connect":
                self.client.start()
                self.client.wait_for_pending_calls()
                status_tool, status_args = self._route("status", {})
                if status_tool not in self.client.tools:
                    if self.client.uncertain:
                        raise BackendError("Cannot re-sync without a status tool mapping. Configure ops.status and connect again.")
                else:
                    self.client.call(status_tool, status_args, mutating=False)
                    self.client.uncertain = False
                info = self.client.server_info
                return {"server": info.get("name"), "version": info.get("version"), "tools": len(self.client.tools)}
            tool, args = self._route(op, {a: v for a, v in k.items() if v is not None})
            if tool not in self.client.tools:
                if op == "status":
                    return {"server": self.client.server_info, "tools": self.tool_names()}
                raise BackendError(f"{op!r} has no mapping for {self.name}: add mcp_servers.{self.name}.ops.{op}, "
                                   f"or use cad_native with one of: {', '.join(self.tool_names())}")
            return self.client.call(tool, args, mutating=False if op in {
                "status", "inspect", "get_parameters", "list_edges", "list_faces", "find_edge", "find_face"
            } else None)
        except McpError as e:
            raise BackendError(f"{self.name}: {e}") from e
