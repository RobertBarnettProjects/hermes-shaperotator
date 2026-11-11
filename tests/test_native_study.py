"""Peaks read back from Inventor's studies are logged, so the claims checker can back them."""
import json

from conftest import load_plugin


def test_study_peaks_become_evidence(tmp_path):
    load_plugin()
    from hermes_shaperotator.core.evidence import EVIDENCE
    from hermes_shaperotator.core.state import STATE
    from hermes_shaperotator.core.tools import _native_study_evidence
    STATE.settings["job_dir"] = str(tmp_path)
    r = _native_study_evidence("run_study_cases", {"cases": {
        "Heavy driver": {"results": {"Von Mises Stress": 240.3, "Displacement": 0.4628}, "warnings": []},
        "Warned": {"results": {"Von Mises Stress": 300.0}, "warnings": ["model separates"]}}})
    assert len(r["evidence_ids"]) == 2
    recs = {x.id: x for x in EVIDENCE.items()}
    first, second = recs[r["evidence_ids"][0]], recs[r["evidence_ids"][1]]
    assert first.values["vm_max_MPa"] == 240.3 and first.values["disp_max_mm"] == 0.4628 and first.verified
    assert not second.verified
    json.dumps(first.values)
