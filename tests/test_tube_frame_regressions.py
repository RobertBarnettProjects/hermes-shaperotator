"""Exercise both tube entry points with a minimal Inventor API double."""
import ast
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ROOT


@pytest.mark.parametrize("source", [
    Path(ROOT) / "backends/inventor/forge/tools/features.py",
    Path(ROOT).parent / "inventor-forge/inventor_forge/tools/features.py",
])
def test_solid_members_are_not_bored(source):
    if not source.is_file():
        pytest.skip("standalone inventor-forge source unavailable")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in ("_tube_members", "_end_kind", "tube_frame")]
    for node in funcs:
        node.decorator_list = []
    calls = []
    tx = SimpleNamespace(End=lambda: None, Abort=lambda: None)
    doc = SimpleNamespace(Update=lambda: None)
    sweep_features = SimpleNamespace(AddUsingPath=lambda profile, path, op: calls.append(op) or SimpleNamespace(Name="sweep"))
    sketch = SimpleNamespace(ModelToSketchSpace=lambda point: point,
                             SketchCircles=SimpleNamespace(AddByCenterRadius=lambda *a: None),
                             Profiles=SimpleNamespace(AddForSolid=lambda: "profile"))
    cd = SimpleNamespace(
        Features=SimpleNamespace(SweepFeatures=sweep_features, CreatePath=lambda line: line),
        Sketches3D=SimpleNamespace(Add=lambda: SimpleNamespace(
            SketchLines3D=SimpleNamespace(AddByTwoPoints=lambda *a: SimpleNamespace(StartSketchPoint="start")))),
        WorkPlanes=SimpleNamespace(AddByNormalToCurve=lambda *a: SimpleNamespace()),
        Sketches=SimpleNamespace(Add=lambda wp: sketch))
    session = SimpleNamespace(app=SimpleNamespace(TransactionManager=SimpleNamespace(StartTransaction=lambda *a: tx)),
                              doc=lambda: doc, snapshot=lambda: SimpleNamespace(bodies=0), cdef=lambda: cd,
                              p3=lambda *p: p, delta=lambda *a: {})
    def safe(fn):
        return fn()
    namespace = {"math": math, "Session": object, "InventorError": RuntimeError, "safe": safe,
                 "MM": 0.1, "K": SimpleNamespace(kNewBodyOperation="new", kJoinOperation="join", kCutOperation="cut"),
                 "r": round, "_bodies": lambda s: [object()]}
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(source), "exec"), namespace)
    result = namespace["tube_frame"](session, [[0, 0, 0, 100, 0, 0]], wall=0)
    assert calls == ["new"]
    assert result["cut_list"][0]["section"] == "33.7 round bar"
    with pytest.raises(RuntimeError):
        namespace["_tube_members"]([[0, 0, 0, 100, 0, 0]], 33.7, -1)
