"""hermes-shaperotator backend routes for the Desktop pane: /api/plugins/hermes-shaperotator/...

Read-only. Serves the scene the agent tools publish to <plugin data>/live/scene.json.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()


def _live() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir("hermes-shaperotator")) / "live"
    except Exception:
        return Path(os.environ.get("HERMES_SHAPEROTATOR_DATA", Path.home() / ".hermes-shaperotator")) / "live"


def _read(name: str):
    p = _live() / name
    if not p.is_file():
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


@router.get("/rev")
async def rev():
    """Cheap poll: just the scene revision."""
    p = _live() / "scene.json"
    if not p.is_file():
        return {"rev": None}
    return {"rev": str(p.stat().st_mtime_ns)}


@router.get("/scene")
async def scene():
    data = await asyncio.to_thread(_read, "scene.json")
    p = _live() / "scene.json"
    if data is None:
        return {"rev": None, "mesh": None, "evidence": [], "fea": None}
    data["rev"] = str(p.stat().st_mtime_ns)
    return data


def _drawing_data_url():
    import base64
    scene = _read("scene.json") or {}
    d = scene.get("drawing") or {}
    path = d.get("path")
    if not path or not os.path.isfile(path) or not path.lower().endswith(".png"):
        return {"data_url": None}
    if os.path.getsize(path) > 8_000_000:
        return {"data_url": None, "error": "drawing image too large"}
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    return {"data_url": "data:image/png;base64," + b64, "title": d.get("title"), "issues": d.get("issues"),
            "at": d.get("at")}


@router.get("/drawing")
async def drawing():
    """The latest rendered drawing sheet (PNG as a data URL) for the pane's Drawing view."""
    return await asyncio.to_thread(_drawing_data_url)
