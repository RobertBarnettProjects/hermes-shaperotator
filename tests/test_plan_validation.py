"""Predictions and model identity are enforced through the plan entry points."""
import pytest

from conftest import load_plugin

load_plugin()
from hermes_shaperotator.core import workbench as wb
from hermes_shaperotator.core.state import STATE


@pytest.fixture()
def plan_env(tmp_path, monkeypatch):
    monkeypatch.setattr(STATE, "job_dir", tmp_path)
    monkeypatch.setattr(STATE, "backend_name", "freecad")
    monkeypatch.setattr(wb, "publish_workbench", lambda: None)
    binding = {"backend": "freecad", "document_path": "C:/models/part.FCStd", "model_version": "v1"}
    monkeypatch.setattr(wb, "_model_binding", lambda: dict(binding))
    return binding


def setup_args():
    return {"supports": [{"face": 1}], "loads": [{"face": 2, "force": [0, 0, -100]}]}


@pytest.mark.parametrize("estimates", [None, {}, {"disp_max_mm": 1},
    {"disp_max_mm": 1, "working": " "}, {"disp_max_mm": float("nan"), "working": "calc"},
    {"disp_max_mm": float("inf"), "working": "calc"}, {"disp_max_mm": 0, "working": "calc"},
    {"disp_max_mm": -1, "working": "calc"}, {"exempt": " "}])
def test_propose_requires_valid_estimates(plan_env, estimates):
    with pytest.raises(ValueError, match="estimates"):
        wb.plan_propose({**setup_args(), "estimates": estimates})


def test_positive_prediction_and_exemption_are_recorded(plan_env):
    p = wb.plan_propose({**setup_args(), "estimates": {"disp_max_mm": 0.1, "working": "FL^3 / 3EI"}})
    assert p["estimates"]["disp_max_mm"] == 0.1
    assert p["document_path"] == plan_env["document_path"]
    p = wb.plan_propose({**setup_args(), "estimates": {"exempt": "contact-dominated setup"}})
    assert wb.hand_check(p["estimates"], {})[0]["line"] == "CHECK SKIPPED: contact-dominated setup"


@pytest.mark.parametrize("field", ["backend", "document_path", "model_version"])
def test_changed_binding_requires_new_proposal(plan_env, field):
    p = wb.plan_propose({**setup_args(), "estimates": {"disp_max_mm": 0.1, "working": "calc"}})
    plan_env[field] = "changed"
    with pytest.raises(ValueError, match="Propose again"):
        wb.plan_run({"plan_id": p["id"]})


def test_edited_setup_requires_revised_estimates(plan_env):
    p = wb.plan_propose({**setup_args(), "estimates": {"disp_max_mm": 0.1, "working": "calc"}})
    with pytest.raises(ValueError, match="revised estimates"):
        wb.plan_run({"plan_id": p["id"], "edits": {"gravity": True}})


def test_unsupported_support_rejected_before_review(plan_env):
    with pytest.raises(ValueError, match="unsupported support"):
        wb.plan_propose({**setup_args(), "supports": [{"face": 1, "kind": "pin"}],
                         "estimates": {"disp_max_mm": 0.1, "working": "calc"}})


def test_zero_and_zero_agree():
    assert wb.hand_check({"disp_max_mm": 0}, {"disp_max_mm": 0})[0]["agrees"]


def test_runs_keep_setup_predictions_and_gravity_mesh(plan_env, monkeypatch, tmp_path):
    import json
    from hermes_shaperotator.core import tools
    calls = []
    def solve(args, source):
        calls.append(args)
        return json.dumps({"ok": True, "disp_max_mm": 0.1, "safety_factor_yield": 3})
    monkeypatch.setattr(tools, "_fea_run", solve)
    monkeypatch.setattr(tools, "_fea_source", lambda args: "")
    monkeypatch.setattr(wb, "checkpoint", lambda *a, **k: {"id": "V1"})
    p = wb.plan_propose({"supports": [{"face": 1}], "gravity": True, "mesh": "fine",
                         "estimates": {"disp_max_mm": 0.1, "working": "self weight bending"}})
    first = wb.plan_run({"plan_id": p["id"]})
    assert calls[0]["loads"] == [{"type": "gravity", "direction": [0, 0, -1]}]
    assert calls[0]["mesh_factor"] == 0.65
    first_path = tmp_path / "runs" / (first["run_id"] + ".json")
    original = first_path.read_bytes()
    second = wb.plan_run({"plan_id": p["id"], "rerun": True, "edits": {"mesh": "coarse"},
                          "estimates": {"disp_max_mm": 0.2, "working": "updated mesh prediction"}})
    assert second["run_id"] != first["run_id"]
    assert first_path.read_bytes() == original
    run = json.loads((tmp_path / "runs" / (second["run_id"] + ".json")).read_text())
    assert run["setup"]["mesh"] == "coarse"
    assert run["setup"]["estimates"]["disp_max_mm"] == 0.2


def test_stress_requires_corresponding_location_and_basis():
    nominal = {"vm_max_MPa": 10, "working": "M/Z", "location": "midspan",
               "stress_definition": "normal bending", "applicability": "away from support"}
    check = wb.hand_check(nominal, {"vm_max_MPa": 1000})
    assert check[0]["line"].startswith("CHECK SKIPPED")
    global_peak = {**nominal, "location": "global maximum", "stress_definition": "von Mises",
                   "applicability": "uniform uniaxial stress, no local singularity"}
    assert wb.hand_check(global_peak, {"vm_max_MPa": 10})[0]["agrees"]
