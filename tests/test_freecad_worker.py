"""Integration test: drive the FreeCAD worker directly and check volumes against hand calcs.

Skips when FreeCAD is not installed. Run:  python -m pytest tests -q
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backends.freecad.client import FreeCADClient, find_freecad_python  # noqa: E402

PY = find_freecad_python("")
pytestmark = pytest.mark.skipif(PY is None, reason="FreeCAD not installed")


@pytest.fixture(scope="module")
def fc():
    c = FreeCADClient(PY)
    yield c
    c.close()


def vol(fc):
    return fc.request("inspect")["volume_mm3"]


def test_flange(fc, tmp_path):
    fc.request("new_part", name="Flange")
    fc.request("sketch", plane="XZ")
    fc.request("profile", points=[[0, 0], [60, 0], [60, 12], [25, 12], [25, 40], [0, 40]])
    fc.request("revolve", axis="Z")
    v_rev = math.pi * (60**2 * 12 + 25**2 * 28)
    assert vol(fc) == pytest.approx(v_rev, rel=1e-4)

    fc.request("hole", x=45, y=0, diameter=11)
    hole = math.pi * 5.5**2 * 12
    assert vol(fc) == pytest.approx(v_rev - hole, rel=1e-4)

    fc.request("pattern_circular", feature_index=2, count=6)
    assert vol(fc) == pytest.approx(v_rev - 6 * hole, rel=1e-4)

    r = fc.request("hole", x=0, y=0, diameter=16, tapped=True)
    assert r["thread"] == "M16x2.0"

    e = fc.request("find_edge", z=0, tolerance=0.01)["all_matches"]
    assert e
    before = vol(fc)
    fc.request("fillet", radius=3, edge_indices=[e[0]])
    assert vol(fc) < before

    step = tmp_path / "flange.step"
    fc.request("export", path=str(step))
    assert step.stat().st_size > 1000


def test_bad_thread_leaves_no_debris(fc):
    fc.request("new_part", name="Block")
    fc.request("sketch", plane="XY")
    fc.request("rect", width=50, height=30)
    fc.request("extrude", distance=10)
    n = len(fc.request("inspect")["features"])
    with pytest.raises(Exception, match="no ISO thread"):
        fc.request("hole", x=0, y=0, diameter=13, tapped=True)
    assert len(fc.request("inspect")["features"]) == n


def test_cut_direction_is_auto_resolved(fc):
    fc.request("new_part", name="Plate")
    fc.request("sketch", plane="XY")
    fc.request("rect", width=100, height=60)
    fc.request("extrude", distance=8)
    fc.request("sketch", plane="XY", offset=8)
    fc.request("rect", width=20, height=20)
    fc.request("extrude", distance=4, operation="cut")
    assert vol(fc) == pytest.approx(100 * 60 * 8 - 20 * 20 * 4, rel=1e-4)
