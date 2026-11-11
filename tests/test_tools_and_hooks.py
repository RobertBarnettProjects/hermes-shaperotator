"""Tool handlers + hooks, end to end on the FreeCAD backend (no Hermes runtime needed)."""
import json
import math

import pytest

from conftest import load_plugin

P = load_plugin()
from hermes_shaperotator.core.state import STATE  # noqa: E402
from hermes_shaperotator.core.tools import HANDLERS  # noqa: E402

pytestmark = pytest.mark.skipif(not STATE.backend("freecad").available()[0], reason="FreeCAD not installed")


def call(_tool, **args):
    return json.loads(HANDLERS[_tool](args))


@pytest.fixture(autouse=True, scope="module")
def _setup(tmp_path_factory):
    STATE.settings["job_dir"] = str(tmp_path_factory.mktemp("jobs"))
    STATE.backend_name = "freecad"
    yield
    STATE.shutdown()


def test_bracket_build_and_export():
    assert call("cad_document", action="new_part", name="Bracket")["ok"]
    r = call("cad_sketch", plane="XY", shapes=[{"shape": "rect", "width": 80, "height": 40}])
    assert r["ok"] and len(r["drawn"]) == 1
    r = call("cad_feature", type="extrude", distance=6)
    assert r["ok"] and "+19200" in r["delta"]
    r = call("cad_feature", type="hole", x=-28, y=0, diameter=9)
    assert r["ok"]
    r = call("cad_feature", type="hole", x=28, y=0, diameter=9)
    assert r["ok"]
    vol = call("cad_inspect", what="summary")["volume_mm3"]
    assert vol == pytest.approx(80 * 40 * 6 - 2 * math.pi * 4.5**2 * 6, rel=1e-4)
    edge = call("cad_inspect", what="find_edge", x=40, y=0, z=6)["edge"]
    r = call("cad_feature", type="fillet", radius=1, edges=[edge])
    assert r["ok"], r
    r = call("cad_inspect", what="render", view="iso")
    assert r["ok"] and r["path"].endswith(".png")
    r = call("cad_document", action="export", format="STEP")
    assert r["ok"] and r["bytes"] > 1000


def test_errors_are_json_not_exceptions():
    r = call("cad_feature", type="fillet", radius=1, edges=[9999])
    assert r["ok"] is False and "out of range" in r["error"]
    r = call("cad_sketch", shapes=[{"shape": "spline"}])
    assert r["ok"] is False


def test_python_requires_approval():
    d = P._pre_tool_call(tool_name="cad_python", args={"code": "print(1)"})
    assert d["action"] == "approve"


def test_pdf_export_blocked_until_clean_check():
    STATE.qa.dirty, STATE.qa.last_issues = True, None
    d = P._pre_tool_call(tool_name="cad_drawing", args={"action": "export_pdf", "path": "x.pdf"})
    assert d and d["action"] == "block"
    STATE.qa.dirty, STATE.qa.last_issues = False, 0
    assert P._pre_tool_call(tool_name="cad_drawing", args={"action": "export_pdf"}) is None
    # force-export arms exactly one bypass
    STATE.qa.dirty, STATE.qa.last_issues = True, 3
    P._slash("force-export")
    assert P._pre_tool_call(tool_name="cad_document", args={"action": "export", "path": "d.pdf"}) is None
    STATE.qa.force_once = False      # the export handler consumes the one-shot override
    assert P._pre_tool_call(tool_name="cad_document", args={"action": "export", "path": "d.pdf"})["action"] == "block"


def test_parameters_drive_geometry():
    call("cad_document", action="new_part", name="Param")
    call("cad_sketch", plane="XY", shapes=[{"shape": "circle", "diameter": 20}])
    call("cad_feature", type="extrude", distance=10)
    call("cad_parameters", action="add", name="len", expression="25")
    r = call("cad_parameters", action="set", name="Pad.Length", expression="Params.len")
    assert r["ok"], r
    assert call("cad_inspect", what="summary")["volume_mm3"] == pytest.approx(math.pi * 100 * 25, rel=1e-4)
