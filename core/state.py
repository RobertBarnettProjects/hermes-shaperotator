"""Process-wide plugin state: settings, the active backend, job folder, QA status."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backend import CadBackend

BUILTIN_BACKENDS = ("freecad", "inventor")


class _Backends(tuple):
    """Built-in backends plus every MCP server configured under ``mcp_servers`` (read live)."""

    def _all(self) -> tuple:
        return BUILTIN_BACKENDS + tuple(k.lower() for k in (STATE.settings.get("mcp_servers") or {}))

    def __iter__(self):
        return iter(self._all())

    def __contains__(self, x) -> bool:
        return x in self._all()

    def __len__(self) -> int:
        return len(self._all())


BACKENDS = _Backends()


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # Hermes helper
        return Path(plugin_data_dir("hermes-shaperotator"))
    except Exception:  # running outside Hermes (tests)
        p = Path(os.environ.get("HERMES_SHAPEROTATOR_DATA", Path.home() / ".hermes-shaperotator"))
        p.mkdir(parents=True, exist_ok=True)
        return p


@dataclass
class DrawingQA:
    dirty: bool = False          # drawing changed since the last clean check
    last_issues: int | None = None
    last_report: str = ""
    force_once: bool = False     # /cad force-export: the next PDF export skips the gate (user override)

    def export_blocked(self) -> str | None:
        """Why PDF export is not allowed right now (None = allowed)."""
        if self.force_once:
            return None
        if self.last_issues is None:
            return "the drawing has not been checked since it changed"
        if self.dirty or self.last_issues:
            return f"the last check found {self.last_issues} issue(s)"
        return None


@dataclass
class State:
    settings: dict[str, Any] = field(default_factory=dict)
    backend_name: str = "freecad"
    backends: dict[str, CadBackend] = field(default_factory=dict)
    qa: DrawingQA = field(default_factory=DrawingQA)
    job_dir: Path | None = None
    last_preview: str | None = None
    last_delta: str = ""
    session_id: str = ""            # the Hermes chat of the tool call being handled (set by the pre_tool_call hook)
    lock: threading.RLock = field(default_factory=threading.RLock)

    # -- backends --------------------------------------------------------
    def backend(self, name: str | None = None) -> CadBackend:
        name = (name or self.backend_name).lower()
        if name not in BACKENDS:
            raise ValueError(f"unknown backend {name!r} (choose {', '.join(BACKENDS)})")
        with self.lock:
            if name not in self.backends:
                if name == "freecad":
                    from ..backends.freecad.backend import FreeCADBackend
                    self.backends[name] = FreeCADBackend(self.settings)
                elif name == "inventor":
                    from ..backends.inventor.backend import InventorBackend
                    self.backends[name] = InventorBackend(self.settings)
                else:
                    from ..backends.mcp.backend import make_mcp_backend
                    cfg = {k.lower(): v for k, v in (self.settings.get("mcp_servers") or {}).items()}[name]
                    self.backends[name] = make_mcp_backend(name, cfg, self.settings)
            return self.backends[name]

    @property
    def family(self) -> str:
        """freecad / inventor / generic — what the active backend drives, whatever the transport."""
        n = self.backend_name
        if n in self.backends:
            return self.backends[n].family
        if n in BUILTIN_BACKENDS:
            return n
        cfg = {k.lower(): v for k, v in (self.settings.get("mcp_servers") or {}).items()}.get(n) or {}
        return str(cfg.get("family") or cfg.get("dialect") or "generic").replace("inventor-forge", "inventor")

    def jobs(self) -> Path:
        if self.job_dir is None:
            base = Path(self.settings.get("job_dir") or (_data_dir() / "jobs"))
            self.job_dir = base / time.strftime("%Y%m%d-%H%M%S")
            self.job_dir.mkdir(parents=True, exist_ok=True)
        return self.job_dir

    def shutdown(self) -> None:
        for b in list(self.backends.values()):
            try:
                b.shutdown()
            except Exception:
                pass
        self.backends.clear()


STATE = State()
