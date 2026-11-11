"""Pane data path: geometry/FEA tools publish scene.json; dashboard/plugin_api.py serves it."""
import asyncio
import importlib.util
import json
import os

import pytest

from conftest import ROOT, load_plugin

P = load_plugin()
from hermes_shaperotator.core.state import STATE  # noqa: E402
from hermes_shaperotator.core.tools import HANDLERS  # noqa: E402

pytestmark = pytest.mark.skipif(not STATE.backend("freecad").available()[0], reason="FreeCAD not installed")


def call(_tool, **a):
    return json.loads(HANDLERS[_tool](a))


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SHAPEROTATOR_DATA", str(tmp_path))
    STATE.settings["job_dir"] = str(tmp_path / "jobs")
    STATE.job_dir = None
    STATE.backend_name = "freecad"
    P._PREVIEW.update(next_at=0.0, dirty=False)
    yield tmp_path


def _api():
    spec = importlib.util.spec_from_file_location("hcad_api", os.path.join(ROOT, "dashboard", "plugin_api.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_geometry_then_fea_scene(data_dir):
    call("cad_document", action="new_part", name="Block")
    call("cad_sketch", plane="XY", shapes=[{"shape": "rect", "width": 60, "height": 20}])
    res = HANDLERS["cad_feature"]({"type": "extrude", "distance": 5})
    P._post_tool_call(tool_name="cad_feature", args={"type": "extrude"}, result=res)
    api = _api()
    s = asyncio.run(api.scene())
    assert s["kind"] == "model" and len(s["mesh"]["faces"]) == 6
    assert len(s["mesh"]["face_of_tri"]) * 3 == len(s["mesh"]["triangles"])

    r = call("cad_fea", action="run", convergence=False,
             supports=[{"faces": {"plane": "x", "value": -30}}],
             loads=[{"type": "force", "faces": {"plane": "x", "value": 30}, "force_N": 500, "direction": [0, 0, -1]}])
    assert r["ok"], r
    assert "scene" not in r  # internal path is not handed to the model
    s = asyncio.run(api.scene())
    m = s["mesh"]
    assert s["kind"] == "fea" and s["fea"]["evidence_id"] == r["evidence_id"]
    assert len(m["values"]) * 3 == len(m["vertices"])
    assert max(m["values"]) == pytest.approx(r["vm_max_MPa"], rel=1e-3)
    assert m["support_faces"] and m["load_faces"]
    assert s["evidence"][-1]["id"] == r["evidence_id"]
    assert asyncio.run(api.rev())["rev"] == s["rev"]
