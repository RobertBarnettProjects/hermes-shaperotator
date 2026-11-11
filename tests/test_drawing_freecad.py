"""FreeCAD drawings: views, dimensions, hole callouts, QA gate, PDF."""
import json
import os

import pytest

from conftest import load_plugin

P = load_plugin()
from hermes_shaperotator.core.state import STATE  # noqa: E402
from hermes_shaperotator.core.tools import HANDLERS  # noqa: E402

HAS_FC = STATE.backend("freecad").available()[0]
pytestmark = pytest.mark.skipif(not HAS_FC, reason="FreeCAD not installed")


def call(_tool, **a):
    return json.loads(HANDLERS[_tool](a))


@pytest.fixture()
def plate(tmp_path):
    STATE.settings["job_dir"] = str(tmp_path / "jobs")
    STATE.job_dir = None
    STATE.backend_name = "freecad"
    call("cad_document", action="new_part", name="Bracket plate")
    call("cad_sketch", plane="XY", shapes=[{"shape": "rect", "x": 60, "y": 25, "width": 120, "height": 50}])
    call("cad_feature", type="extrude", distance=10)
    call("cad_feature", type="hole", x=20, y=25, diameter=11)
    call("cad_feature", type="hole", x=100, y=25, diameter=11)
    return tmp_path


def test_sheet_check_gate_and_pdf(plate):
    r = call("cad_drawing", action="new", standard="AS1100", sheet_size="A3")
    assert r["ok"], r
    r = call("cad_drawing", action="part_sheet", views="front,top,right,iso")
    assert r["ok"], r
    assert r["views"] == ["front", "top", "right", "iso"] and r["projection"] == "third angle"
    assert "2× Ø11 THRU" in r["hole_callouts"]
    dims = " | ".join(r["dimensions"])
    assert "overall length: 120.0" in dims and "overall width: 50.0" in dims and "overall height: 10.0" in dims
    chk = call("cad_drawing", action="check")
    assert chk["issues"] > 0 and not chk["export_allowed"]          # title block still empty
    blocked = P._pre_tool_call(tool_name="cad_drawing", args={"action": "export_pdf"})
    assert blocked and blocked["action"] == "block"
    call("cad_drawing", action="properties", properties={"title": "Bracket plate", "drawing_number": "HC-001",
                                                           "material": "AS/NZS 3678-250 10 mm", "drawn_by": "Hermes"})
    chk = call("cad_drawing", action="check")
    assert chk["issues"] == 0, chk["report"]
    assert P._pre_tool_call(tool_name="cad_drawing", args={"action": "export_pdf"}) is None
    png = call("cad_drawing", action="render", path=str(plate / "sheet.png"))
    assert png["ok"] and os.path.getsize(png["path"]) > 20_000
    pdf = call("cad_drawing", action="export_pdf", path=str(plate / "sheet.pdf"))
    assert pdf["ok"] and open(pdf["path"], "rb").read(4) == b"%PDF"


def test_first_angle_layout(plate):
    call("cad_drawing", action="new", standard="ISO")
    r = call("cad_drawing", action="part_sheet", views="front,top,right")
    assert r["ok"] and r["projection"] == "first angle"


def test_rendered_sheet_reaches_the_pane(plate, monkeypatch):
    import asyncio
    import importlib.util
    monkeypatch.setenv("HERMES_SHAPEROTATOR_DATA", str(plate))
    call("cad_drawing", action="new")
    call("cad_drawing", action="part_sheet")
    call("cad_drawing", action="render", path=str(plate / "pane.png"))
    spec = importlib.util.spec_from_file_location(
        "hcad_api2", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard",
                                  "plugin_api.py"))
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    d = asyncio.run(api.drawing())
    assert d["data_url"].startswith("data:image/png;base64,") and len(d["data_url"]) > 20_000


def test_handler_enforces_gate_and_force_once(plate):
    call("cad_drawing", action="new")
    call("cad_drawing", action="part_sheet")
    r = call("cad_drawing", action="export_pdf", path=str(plate / "x.pdf"))
    assert not r["ok"] and "QA gate" in r["error"]                    # no check yet
    assert "QA gate" in P._slash("force-export")
    assert P._pre_tool_call(tool_name="cad_drawing", args={"action": "export_pdf"}) is None
    assert call("cad_drawing", action="export_pdf", path=str(plate / "x.pdf"))["ok"]
    r = call("cad_drawing", action="export_pdf", path=str(plate / "y.pdf"))
    assert not r["ok"]                                                  # override was one-shot
