"""hermes-shaperotator tool handlers on the Inventor backend (inventor-forge). Skips without Inventor.

Live: builds a plate in Inventor, checks volumes against hand calculations, runs cad_native tools,
makes a drawing and checks the QA gate. Leaves nothing open (documents are closed unsaved).
"""
import json
import math
import os

import pytest

from conftest import load_plugin

P = load_plugin()
from hermes_shaperotator.core.state import STATE  # noqa: E402
from hermes_shaperotator.core.tools import HANDLERS  # noqa: E402

INV = STATE.backend("inventor")
pytestmark = pytest.mark.skipif(not INV.available()[0] or os.environ.get("HERMES_SHAPEROTATOR_SKIP_INVENTOR"),
                                reason="Autodesk Inventor not available")


def call(_tool, **args):
    return json.loads(HANDLERS[_tool](args))


@pytest.fixture(autouse=True, scope="module")
def _setup(tmp_path_factory):
    STATE.settings["job_dir"] = str(tmp_path_factory.mktemp("invjobs"))
    prev = STATE.backend_name
    STATE.backend_name = "inventor"
    yield
    for _ in range(3):
        st = call("cad_native", tool="status")
        if st.get("active") and st["active"]["name"].startswith(("HcInv", "Drawing")):
            call("cad_document", action="close")
        else:
            break
    STATE.backend_name = prev


def approx(a, b, rel=1e-3):
    return abs(a - b) <= rel * max(abs(a), abs(b))


def test_plate_build_volumes_and_native_tools(tmp_path):
    assert call("cad_document", action="new_part", name="HcInvPlate")["ok"]
    r = call("cad_sketch", plane="XY", shapes=[{"shape": "rect", "width": 100, "height": 60}])
    assert r["ok"], r
    r = call("cad_feature", type="extrude", distance=8)
    assert r["ok"] and approx(r["volume_mm3"], 100 * 60 * 8), r
    r = call("cad_feature", type="hole", x=30, y=0, diameter=10)
    assert r["ok"] and approx(r["volume_change_mm3"], -math.pi * 25 * 8), r
    r = call("cad_native", tool="set_material", args={"name": "Steel, Mild"})
    assert r["ok"] and r["material"] == "Steel, Mild", r
    m = call("cad_native", tool="mass_properties")
    assert approx(m["mass_kg"], (100 * 60 * 8 - math.pi * 25 * 8) * 1e-9 * 7850, 1e-2), m
    e = call("cad_inspect", what="find_edge", x=50, y=0, z=8)
    assert call("cad_feature", type="fillet", radius=2, edges=[e["edge"]])["ok"]
    r = call("cad_native", tool="execute_python", args={"code": "1"})
    assert not r["ok"] and "cad_python" in r["error"]
    path = str(tmp_path / "HcInvPlate.ipt")
    assert call("cad_document", action="save", path=path)["ok"]


def test_drawing_sheet_and_qa_gate(tmp_path):
    model = str(tmp_path.parent / "test_plate_build_volumes_and0" / "HcInvPlate.ipt")
    if not os.path.exists(model):
        st = call("cad_native", tool="status")
        model = (st.get("active") or {}).get("path", "")
    assert call("cad_standard", action="set", standard="AS1100")["ok"]      # the user's choice for this chat
    r = call("cad_drawing", action="part_sheet", model_path=model)
    assert r["ok"], r
    assert P._pre_tool_call(tool_name="cad_drawing", args={"action": "export_pdf"})["action"] == "block"
    chk = call("cad_drawing", action="check")
    assert chk["ok"] and isinstance(chk["issues"], int)
    if chk["issues"] == 0:
        assert P._pre_tool_call(tool_name="cad_drawing", args={"action": "export_pdf"}) is None
        out = call("cad_drawing", action="export_pdf", path=str(tmp_path / "plate.pdf"))
        assert out["ok"] and out["bytes"] > 10000
    r = call("cad_native", tool="export_pdf", args={"path": "x.pdf"})
    assert not r["ok"] and "QA gate" in r["error"]
    call("cad_document", action="close")
