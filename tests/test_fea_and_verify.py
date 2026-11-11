"""FEA against closed-form results, plus the claims checker."""
import json
import math

import pytest

from conftest import load_plugin

P = load_plugin()
from hermes_shaperotator.core.evidence import EVIDENCE, EvidenceLog  # noqa: E402
from hermes_shaperotator.core.materials import resolve  # noqa: E402
from hermes_shaperotator.core.state import STATE  # noqa: E402
from hermes_shaperotator.core.tools import HANDLERS  # noqa: E402
from hermes_shaperotator.core import verify  # noqa: E402

HAS_FC = STATE.backend("freecad").available()[0]


def call(_tool, **args):
    return json.loads(HANDLERS[_tool](args))


@pytest.fixture(scope="module")
def jobs(tmp_path_factory):
    STATE.settings["job_dir"] = str(tmp_path_factory.mktemp("jobs"))
    STATE.job_dir = None
    STATE.backend_name = "freecad"
    EVIDENCE.clear()
    yield
    STATE.shutdown()


def _beam(L=100, b=10, h=10):
    call("cad_document", action="new_part", name="Beam")
    call("cad_sketch", plane="YZ", shapes=[{"shape": "rect", "x": 0, "y": h / 2, "width": b, "height": h}])
    call("cad_feature", type="extrude", distance=L)


@pytest.mark.skipif(not HAS_FC, reason="FreeCAD not installed")
def test_cantilever_matches_beam_theory(jobs):
    L, b, h, F, E = 100.0, 10.0, 10.0, 1000.0, 200000.0
    _beam(L, b, h)
    r = call("cad_fea", action="run", material="AS/NZS 3678-250", thickness_mm=10,
             supports=[{"faces": {"plane": "x", "value": 0}}],
             loads=[{"type": "force", "faces": {"plane": "x", "value": L}, "force_N": F, "direction": [0, 0, -1]}],
             name="tip load")
    assert r["ok"], r
    I = b * h**3 / 12
    assert r["disp_max_mm"] == pytest.approx(F * L**3 / (3 * E * I), rel=0.03)
    assert r["vm_max_MPa"] == pytest.approx(F * L * (h / 2) / I, rel=0.05)
    assert r["equilibrium_error"] < 1e-3
    assert r["convergence"]["converged"]
    assert r["material"]["yield_MPa"] == 260  # AS/NZS 3678-250, 8 < t <= 12
    assert r["evidence_id"] == "FEA-001"
    assert any("exceeds yield" in w for w in r["warnings"])


@pytest.mark.skipif(not HAS_FC, reason="FreeCAD not installed")
def test_pressure_and_gravity_equilibrium(jobs):
    call("cad_document", action="new_part", name="Plate")
    call("cad_sketch", plane="XY", shapes=[{"shape": "rect", "width": 200, "height": 100}])
    call("cad_feature", type="extrude", distance=10)
    r = call("cad_fea", action="run", material="Aluminium 6061-T6",
             supports=[{"faces": [{"plane": "x", "value": -100}, {"plane": "x", "value": 100}]}],
             loads=[{"type": "pressure", "faces": {"normal": [0, 0, 1], "at_max": True}, "pressure_MPa": 0.1},
                    {"type": "gravity", "direction": [0, 0, -1]}],
             convergence=False)
    assert r["ok"], r
    expected = 0.1 * 200 * 100 + 200 * 100 * 10 * 1e-9 * 2700 * 9.80665
    assert r["applied_total_N"][2] == pytest.approx(-expected, rel=1e-3)
    assert r["equilibrium_error"] < 0.01
    assert r["mass_kg"] == pytest.approx(0.54, rel=1e-3)


@pytest.mark.skipif(not HAS_FC, reason="FreeCAD not installed")
def test_bad_selector_is_a_clean_error(jobs):
    _beam()
    r = call("cad_fea", action="run", supports=[{"faces": {"plane": "x", "value": 55}}],
             loads=[{"type": "force", "faces": {"face": 1}, "force_N": 1, "direction": [0, 0, 1]}])
    assert r["ok"] is False and "no planar face" in r["error"]


def test_material_bands():
    assert resolve("AS/NZS 3678-250", 6)["yield_MPa"] == 280
    assert resolve("grade 350", 16)["yield_MPa"] == 350
    assert resolve("AS/NZS 3678-250")["yield_MPa"] == 230  # unknown t -> lowest band
    with pytest.raises(ValueError):
        resolve({"E_MPa": 1})


def test_claims_checker():
    log = EvidenceLog()
    log.add("fea", "case", {"vm_max_MPa": 116.09, "disp_max_mm": 0.345, "safety_factor_yield": 2.153,
                            "mass_kg": 1747.2, "yield_MPa": 250}, verified=True)
    text = ("Peak von Mises stress is 116 MPa, below the 250 MPa yield, giving a safety factor of 2.15. "
            "Max deflection is 0.35 mm. The frame weighs 1747 kg. A safety factor of 3.0 is required. "
            "The gusset sees 180 MPa.")
    claims = verify.check(text, log.items())
    by = {(c.quantity, c.value): c.status for c in claims}
    assert by[("stress_MPa", 116.0)] == "verified"
    assert by[("stress_MPa", 250.0)] == "verified"       # matches the material yield
    assert by[("safety_factor", 2.15)] == "verified"
    assert by[("disp_mm", 0.35)] == "verified"
    assert by[("mass_kg", 1747.0)] == "verified"
    assert by[("safety_factor", 3.0)] == "requirement"      # a target, not a result
    assert by[("stress_MPa", 180.0)] == "mismatch"
    foot = verify.footer(claims)
    assert "FEA-001" in foot and "❌" in foot


def test_unsupported_claims_flagged_without_evidence():
    claims = verify.check("The bracket sees about 80 MPa and deflects 1.2 mm.", [])
    assert {c.status for c in claims} == {"unsupported"}
    assert "❓" in verify.footer(claims)


def test_transform_hook_appends_footer(jobs):
    EVIDENCE.clear()
    EVIDENCE.add("fea", "c", {"vm_max_MPa": 50.0}, verified=True)
    out = P._transform_llm_output(response_text="Peak stress is 50 MPa.")
    assert out.startswith("Peak stress is 50 MPa.") and "✅" in out
    assert P._transform_llm_output(response_text="No numbers here.") is None


def test_footer_dedupes_and_lists_warnings_once():
    from hermes_shaperotator.core.evidence import Record
    rec = Record(id="FEA-009", kind="fea", summary="x",
                 values={"vm_max_MPa": 600.396, "yield_MPa": 260, "disp_max_mm": 2.00607, "safety_factor_yield": 0.433},
                 verified=False, warnings=["peak stress exceeds yield"], data={})
    text = ("Yield is 260 MPa. Max von Mises: 600.396 MPa. Theory gives 600 MPa and again 600 MPa. "
            "Max displacement 2.00607 mm. Safety factor on yield: 0.433.")
    claims = verify.check(text, [rec])
    assert any(c.quantity == "safety_factor" and c.status == "verified" for c in claims)
    out = verify.footer(claims)
    assert out.count("peak stress exceeds yield") == 1
    assert "yield strength **260**" in out and "✅ yield strength" in out
    assert out.count("matches FEA-009 (600.396)") == 1


def test_sf_in_markdown_table():
    claims = verify.extract("| Safety factor (yield) | **20.3** |\n| SF | 1.5 |")
    assert [c.value for c in claims if c.quantity == "safety_factor"] == [20.3, 1.5]


@pytest.mark.skipif(not HAS_FC, reason="FreeCAD not installed")
def test_normal_selector_picks_the_right_face(jobs):
    """Regression: faces FreeCAD marks 'Reversed' used to get their normal flipped twice."""
    call("cad_document", action="new_part", name="Step")
    call("cad_sketch", plane="XY", shapes=[{"shape": "rect", "width": 60, "height": 20}])
    call("cad_feature", type="extrude", distance=5)
    r = call("cad_fea", action="run", convergence=False,
             supports=[{"faces": {"normal": [0, 0, -1]}}],
             loads=[{"type": "pressure", "faces": {"normal": [0, 0, 1], "at_max": True}, "pressure_MPa": 0.1}])
    assert r["ok"], r
    sup = r["supports"][0]["resolved"]
    assert len(sup) == 1 and sup[0]["centroid"][2] == pytest.approx(0, abs=1e-6)
    assert sup[0]["area_mm2"] == pytest.approx(1200, rel=1e-3)
    assert r["equilibrium_error"] < 0.01      # applied pressure direction agrees with the reactions


def test_footer_only_in_cad_sessions():
    P._post_tool_call(tool_name="cad_status", args={}, result="{}", session_id="cad-chat")
    assert P._transform_llm_output(response_text="Peak stress is 50 MPa.", session_id="other-chat") is None
    out = P._transform_llm_output(response_text="Peak stress is 50 MPa.", session_id="cad-chat")
    assert out and "hermes-shaperotator verification" in out


@pytest.mark.skipif(not HAS_FC, reason="FreeCAD not installed")
def test_sweep_finds_lightest_passing_thickness(jobs):
    """Cantilever 100 x 20 wide, tip load 300 N: SF grows with thickness; pick the thinnest with SF >= 2."""
    call("cad_document", action="new_part", name="Sweep")
    call("cad_sketch", plane="XY", shapes=[{"shape": "rect", "x": 50, "y": 10, "width": 100, "height": 20}])
    call("cad_feature", type="extrude", distance=4)
    params = call("cad_parameters", action="get")
    pad = next(p["name"] for p in params["parameters"] if p["name"].endswith(".Length"))
    before = len([e for e in EVIDENCE.items() if e.kind == "fea"])
    r = call("cad_fea", action="sweep", sweep={"parameter": pad, "values": [4, 6, 8], "is_thickness": True},
             target_sf=2.0, material="AS/NZS 3678-250",
             supports=[{"faces": {"plane": "x", "value": 0}}],
             loads=[{"type": "force", "faces": {"plane": "x", "value": 100}, "force_N": 300,
                     "direction": [0, 0, -1]}])
    assert r["ok"], r
    sfs = [row["safety_factor_yield"] for row in r["runs"]]
    assert sfs == sorted(sfs)                                  # thicker → safer
    assert r["best"]["value"] == min(row["value"] for row in r["runs"] if row["safety_factor_yield"] >= 2)
    assert r["confirmed"]["convergence"] is not None
    assert r["model_left_at"] == r["original_value"] == 4
    assert len([e for e in EVIDENCE.items() if e.kind == "fea"]) - before == 4   # 3 sweep runs + 1 confirmed


def test_claims_only_backed_by_own_session():
    rec = EVIDENCE.add("fea", "other chat", {"vm_max_MPa": 77.7}, verified=True)
    P._post_tool_call(tool_name="cad_status", args={}, result="{}", session_id="mine")
    out = P._transform_llm_output(response_text="Peak stress is 77.7 MPa.", session_id="mine")
    assert "❓" in out                      # FEA run in another chat does not back this one
    P._post_tool_call(tool_name="cad_fea", args={}, result='{"ok": true, "evidence_id": "%s"}' % rec.id,
                      session_id="mine")
    out = P._transform_llm_output(response_text="Peak stress is 77.7 MPa.", session_id="mine")
    assert "✅" in out


def test_hand_calc_operands_and_modulus_are_not_claims():
    from hermes_shaperotator.core.evidence import Record
    from hermes_shaperotator.core.verify import check
    rec = Record("FEA-900", "fea", "x", {"vm_max_MPa": 88.06, "safety_factor_yield": 2.351, "yield_MPa": 207.0,
                                         "disp_max_mm": 0.611}, True)
    text = ("Material E = 220000 MPa, yield 207 MPa. Peak stress 88.06 MPa, SF 2.351. "
            "Theory SF = 207/90 = 2.3; sigma = 6 × 200 MPa? no.")
    claims = check(text, [rec])
    vals = {(c.quantity, c.value) for c in claims}
    assert ("stress_MPa", 220000.0) not in vals
    assert ("safety_factor", 207.0) not in vals
    assert ("stress_MPa", 88.06) in vals


def test_slow_previews_are_throttled_and_caught_up_at_the_end_of_the_reply(monkeypatch):
    P = load_plugin()
    calls = []

    def slow_publish(label):
        calls.append(label)
        import time
        time.sleep(0.05)

    monkeypatch.setattr(P, "publish_geometry", slow_publish)
    monkeypatch.setattr(P, "PREVIEW_MIN_INTERVAL_S", 60.0)
    P._PREVIEW.update(next_at=0.0, dirty=False)
    ok = '{"ok": true}'
    args = {"tool": "place_component"}
    P._post_tool_call(tool_name="cad_native", args=args, result=ok)
    P._post_tool_call(tool_name="cad_native", args=args, result=ok)
    P._post_tool_call(tool_name="cad_native", args=args, result=ok)
    assert len(calls) == 1 and P._PREVIEW["dirty"]
    P._post_tool_call(tool_name="cad_native", args={"tool": "list_faces"}, result=ok)     # read-only: nothing to redraw
    assert len(calls) == 1
    P._transform_llm_output(response_text="done", session_id="")
    assert len(calls) == 2 and not P._PREVIEW["dirty"]
    P._PREVIEW.update(next_at=0.0, dirty=False)


def test_inventor_stress_analysis_needs_approval_and_feeds_the_evidence_log():
    P = load_plugin()
    from hermes_shaperotator.core.evidence import EVIDENCE
    from hermes_shaperotator.core.tools import _native_fea_evidence
    from hermes_shaperotator.core.verify import check
    d = P._pre_tool_call(tool_name="cad_native", args={"tool": "analyze_stress", "args": {}})
    assert d["action"] == "approve" and "MOUSE" in d["message"]
    assert P._pre_tool_call(tool_name="cad_native", args={"tool": "mass_properties"}) is None
    r = _native_fea_evidence({}, {"max_von_mises_MPa": 88.5357, "max_displacement_mm": 0.611419,
                                  "safety_factor_yield": 2.338, "equilibrium_error_pct": 0.0,
                                  "moment_check": {"ok": True}, "material": {"yield_MPa": "207 MPa"},
                                  "mesh_avg_size_fraction": 0.04})
    rec = [x for x in EVIDENCE.items() if x.id == r["evidence_id"]][0]
    assert rec.verified and rec.values["yield_MPa"] == 207
    claims = check("Peak von Mises 88.54 MPa, deflection 0.6114 mm, SF 2.34.", [rec])
    assert all(c.status == "verified" for c in claims), [(c.text, c.status) for c in claims]
