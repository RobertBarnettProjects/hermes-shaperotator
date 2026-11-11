"""Publish what the Desktop pane shows: <plugin data>/live/scene.json + a push event.

The agent tools and the pane's backend (dashboard/plugin_api.py) run in different processes, so the
hand-off is a file. scene.json = {rev, at, backend, kind, title, mesh, evidence, fea}.
"""
from __future__ import annotations

import json
import uuid
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from .evidence import EVIDENCE
from .state import STATE, _data_dir

logger = logging.getLogger(__name__)
_REV = {"n": 0}
SCENE_LOCK = threading.RLock()                 # one writer at a time (tools, the live-sync thread, the workbench)
KEEP = ("plans", "history", "artifacts", "model_info")   # survive a geometry refresh


def _write_scene(scene: dict) -> None:
    p = live_dir() / "scene.json"
    tmp = live_dir() / f"scene.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(scene, fh, default=str)
    os.replace(tmp, p)
_DRAWING: dict[str, Any] = {"v": None}     # last rendered drawing sheet, shown in the pane's Drawing tab


def live_dir() -> Path:
    p = _data_dir() / "live"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _evidence() -> list[dict]:
    return [{"id": r.id, "kind": r.kind, "summary": r.summary, "verified": r.verified,
             "warnings": r.warnings, "values": r.values, "at": r.at, "label": r.data.get("label")}
            for r in EVIDENCE.items()]


def publish(mesh_path: str | None, kind: str, title: str, fea: dict[str, Any] | None = None) -> None:
    """Best effort: the pane is optional and must never break a tool call."""
    try:
        mesh = None
        if mesh_path and os.path.isfile(mesh_path):
            with open(mesh_path, encoding="utf-8") as fh:
                mesh = json.load(fh)
            mesh["rev"] = uuid.uuid4().hex
        with SCENE_LOCK:
            _REV["n"] += 1
            scene = {"rev": f"{int(time.time() * 1000)}-{_REV['n']}", "at": time.time(), "backend": STATE.backend_name,
                     "kind": kind, "title": title, "mesh": mesh, "evidence": _evidence(), "fea": fea,
                     "drawing": _DRAWING["v"]}
            old = live_dir() / "scene.json"
            if old.is_file():
                try:
                    prev = json.loads(old.read_text(encoding="utf-8"))
                    scene.update({k: prev[k] for k in KEEP if k in prev})
                except ValueError:
                    pass
            _write_scene(scene)
        _notify({"rev": scene["rev"], "kind": kind})
    except Exception as e:
        logger.debug("hermes-shaperotator: scene publish failed: %s", e)


def publish_drawing(png_path: str, title: str, issues: int | None, kind: str = "sheet") -> None:
    """Attach the latest picture (a sheet render, or an Inventor result with kind="result") to the current scene."""
    try:
        _DRAWING["v"] = {"path": png_path, "title": title, "issues": issues, "at": time.time(), "kind": kind}
        with SCENE_LOCK:
            p = live_dir() / "scene.json"
            scene = {}
            if p.is_file():
                with open(p, encoding="utf-8") as fh:
                    scene = json.load(fh)
            _REV["n"] += 1
            scene.update({"rev": f"{int(time.time() * 1000)}-{_REV['n']}", "drawing": _DRAWING["v"],
                          "evidence": _evidence()})
            scene.setdefault("kind", "model")
            scene.setdefault("title", title)
            scene.setdefault("backend", STATE.backend_name)
            _write_scene(scene)
        _notify({"rev": scene["rev"], "kind": "drawing"})
    except Exception as e:
        logger.debug("hermes-shaperotator: drawing publish failed: %s", e)


def publish_geometry(title: str = "") -> None:
    """Re-mesh the current model for the pane (FreeCAD body directly; other backends via STEP)."""
    try:
        fc = STATE.backend("freecad")
        if not fc.available()[0]:
            return
        src = ""
        if STATE.backend_name != "freecad":
            doc = STATE.backend().call("status").get("document") or {}
            if not isinstance(doc, dict):
                doc = {}
            has_solid = (doc.get("type") == "part" and doc.get("bodies")) or (doc.get("type") == "assembly" and doc.get("occurrences"))
            if STATE.family == "inventor" and not has_solid:
                return                # a drawing or an empty document has nothing to show (or to export)
            src = str(STATE.jobs() / "viewer-source.step")
            STATE.backend().call("export", path=src, format="STEP")
        out = str(live_dir() / "mesh.json")
        fc.call("viewer_mesh", out_path=out, source_path=src)
        publish(out, "model", title or "model")
    except Exception as e:
        logger.debug("hermes-shaperotator: geometry publish failed: %s", e)


def _notify(payload: dict) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("hermes-shaperotator", "scene.updated", payload)
    except Exception:
        pass  # not running under a Desktop-connected gateway: the pane polls instead
