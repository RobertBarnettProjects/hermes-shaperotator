"""Building with fewer calls: compact topology selections done on the server, parametric profiles with arcs and
circles extruded or revolved in one call, and transactional batches of steps with named outputs."""
from __future__ import annotations

import math
import re
import time

from ..com import MM, InventorError, K, items, r, safe
from ..registry import TOOLS, tool
from ..session import Session
from .inspect import edge_row, face_row


# --------------------------------------------------------------------------- selections
AXES = {"x": 0, "y": 1, "z": 2}


def _in_plane(points: list, plane: dict | None, tol: float) -> bool:
    if not plane:
        return True
    i = AXES[str(plane.get("axis", "z")).lower()]
    at = float(plane.get("at", 0.0))
    return all(p is not None and abs(p[i] - at) <= tol for p in points)


def _in_box(p, box) -> bool:
    if not box:
        return True
    return p is not None and all(min(box[i], box[i + 3]) - 1e-9 <= p[i] <= max(box[i], box[i + 3]) + 1e-9
                                 for i in range(3))


def edge_matches(row: dict, ends: list, plane=None, box=None, type: str = "", min_length: float = 0.0,
                 max_length: float = 0.0, diameter: float = 0.0, tol: float = 0.01) -> bool:
    """Whether an edge row (edge_row plus its end points) passes the filters."""
    if type and row.get("type") != type:
        return False
    length = row.get("length_mm", 0.0)
    if min_length and length < min_length - tol or max_length and length > max_length + tol:
        return False
    if diameter and abs(row.get("diameter_mm", -1) - diameter) > tol:
        return False
    mid = row.get("midpoint_mm")
    if not _in_plane([mid] + ends, plane, tol):
        return False
    return _in_box(mid, box)


def _ends(e) -> list:
    out = []
    for v in (safe(lambda: e.StartVertex.Point), safe(lambda: e.StopVertex.Point)):
        if v is not None:
            out.append([v.X * 10, v.Y * 10, v.Z * 10])
    return out


@tool("inspect", mutates=False)
def select_edges(s: Session, type: str = "", plane: dict | None = None, box: list[float] | None = None,
                 of_face: int = 0, min_length: float = 0.0, max_length: float = 0.0, diameter: float = 0.0,
                 tolerance: float = 0.01, body: str = "", samples: int = 0) -> dict:
    """Edge indices that pass filters, chosen on the server, so only the answer comes back (not a table of every
    edge): edges lying in a plane, inside a box, bounding a face, of a type, length or diameter. Indices are per body.

    type: line, circle, arc...
    plane: {"axis": "z", "at": 5}: edges lying entirely in the plane z = 5 mm
    box: [x0, y0, z0, x1, y1, z1] mm: edges whose midpoint is inside
    of_face: only edges of this face (its boundary)
    min_length: mm; max_length likewise (0 = no limit)
    diameter: circles and arcs of this diameter, mm
    tolerance: mm, strict match for plane, lengths and diameters
    body: body name or index of a multi-body part (default the first)
    samples: also return this many full rows, to check the selection
    """
    b = s.body(body or None)
    allowed = None
    if of_face:
        keys = {safe(lambda e=e: e.TransientKey) for e in items(s.face(of_face, body or None).Edges)}
        allowed = keys
    picked, rows = [], []
    total = b.Edges.Count
    for i, e in enumerate(items(b.Edges), 1):
        if allowed is not None and safe(lambda e=e: e.TransientKey) not in allowed:
            continue
        row = edge_row(i, e)
        if edge_matches(row, _ends(e), plane, box, type, min_length, max_length, diameter, tolerance):
            picked.append(i)
            if len(rows) < samples:
                rows.append(row)
    out = {"edges": picked, "count": len(picked), "total": total, "body": safe(lambda: b.Name, "")}
    if rows:
        out["samples"] = rows
    return out


@tool("inspect", mutates=False)
def select_faces(s: Session, type: str = "", normal: list[float] | None = None, plane: dict | None = None,
                 box: list[float] | None = None, min_area: float = 0.0, diameter: float = 0.0,
                 tolerance: float = 0.01, body: str = "", samples: int = 0) -> dict:
    """Face indices that pass filters, chosen on the server: type, outward normal, lying in a plane, centroid inside a
    box, area, cylinder diameter. Returns indices and counts only (samples adds a few full rows).

    normal: e.g. [0, 0, -1] for faces facing down
    plane: {"axis": "z", "at": 0}: planar faces lying in z = 0
    box: [x0, y0, z0, x1, y1, z1] mm for the centroid
    """
    b = s.body(body or None)
    picked, rows = [], []
    nv = None
    if normal:
        n = math.sqrt(sum(v * v for v in normal)) or 1.0
        nv = [v / n for v in normal]
    for i, f in enumerate(items(b.Faces), 1):
        row = face_row(s, i, f)
        if type and row["type"] != type or row["area_mm2"] < min_area:
            continue
        if diameter and abs(row.get("diameter_mm", -1) - diameter) > tolerance:
            continue
        if nv and not (row.get("normal") and sum(row["normal"][k] * nv[k] for k in range(3)) > 0.9999):
            continue
        if plane and not (row["type"] == "plane" and _in_plane([row["centroid_mm"]], plane, tolerance) and
                          abs(abs(row["normal"][AXES[str(plane.get('axis', 'z')).lower()]]) - 1) < 1e-6):
            continue
        if not _in_box(row["centroid_mm"], box):
            continue
        picked.append(i)
        if len(rows) < samples:
            rows.append(row)
    out = {"faces": picked, "count": len(picked), "total": b.Faces.Count, "body": safe(lambda: b.Name, "")}
    if rows:
        out["samples"] = rows
    return out


# --------------------------------------------------------------------------- parametric profiles
PLANE_AXES = {"XY": (0, 1, 2), "XZ": (0, 2, 1), "YZ": (1, 2, 0)}


def parse_loop(loop: list) -> list[dict]:
    """A loop as written by the caller -> steps: {"kind": "start"|"line"|"arc", "to": [u, v], "center": [u, v],
    "cw": bool} or a single {"kind": "circle", "center": [u, v], "radius": expr}."""
    if isinstance(loop, dict) and "circle" in loop:
        return [{"kind": "circle", "center": loop["circle"], "radius": loop.get("radius")}]
    steps = []
    for k, seg in enumerate(loop):
        if isinstance(seg, (list, tuple)):
            steps.append({"kind": "start" if k == 0 else "line", "to": list(seg)})
        elif isinstance(seg, dict) and "arc" in seg:
            if k == 0:
                raise InventorError("a loop starts with a point [u, v], not an arc")
            if "center" not in seg:
                raise InventorError("an arc needs its centre: {\"arc\": [u, v], \"center\": [u, v]}")
            steps.append({"kind": "arc", "to": list(seg["arc"]), "center": list(seg["center"]),
                          "cw": bool(seg.get("cw", False))})
        elif isinstance(seg, dict) and "circle" in seg:
            raise InventorError("a circle is a loop of its own: {\"circle\": [u, v], \"radius\": \"R\"}")
        else:
            raise InventorError(f"can't read loop item {seg!r}")
    if len(steps) < 2:
        raise InventorError("a loop needs a start point and at least two more segments")
    return steps


def arc_radius_mismatch(start, end, center) -> float:
    """How far apart (mm) the distances from an arc's centre to its two ends are; 0 for a true arc."""
    return abs(math.dist(start, center) - math.dist(end, center))


def _expr_mm(d, text) -> float:
    return float(d.UnitsOfMeasure.GetValueFromExpression(str(text), K.kMillimeterLengthUnits)) * 10.0


@tool("features")
def parametric_profile(s: Session, plane: str, loops: list, operation: str = "join", extrude: str = "",
                       symmetric: bool = False, revolve_axis: str = "", angle: str = "360 deg", name: str = "",
                       body: str = "") -> dict:
    """A closed profile of lines, arcs and circles, every point driven by parameter expressions and dimensioned from
    the origin (so the sketch is fully constrained and edits through parameters reshape it), then extruded or revolved
    in the same call. Loops: the first is the outline, others holes or islands.

    plane: XY, XZ or YZ; coordinates are [u, v] along the plane's two axes (XY: x, y; XZ: x, z; YZ: y, z)
    loops: [[["0", "0"], ["L", "0"], {"arc": ["L", "2*R"], "center": ["L", "R"]}, ["0", "2*R"]],
      {"circle": ["L/2", "R"], "radius": "r_hole"}]: a loop is a start point then segments; a point is a line to
      it, {"arc": end, "center": c, "cw": false} an arc (counter-clockwise unless cw); the loop closes itself.
      Values are expressions in mm (parameters allowed); a leading "-" puts a point on the negative side
    operation: join | cut | intersect | new_body
    extrude: distance expression (e.g. "T"); give this or revolve_axis
    symmetric: extrude equally both ways from the plane
    revolve_axis: X, Y, Z or a work axis name, to revolve instead of extrude
    angle: revolve angle expression
    name: feature name
    body: body a cut/join acts on in a multi-body part
    """
    cd = s.cdef()
    n0 = cd.Sketches.Count
    try:
        return _profile_feature(s, plane, loops, operation, extrude, symmetric, revolve_axis, angle, name, body)
    except Exception:
        for i in range(cd.Sketches.Count, n0, -1):      # don't leave a half-built sketch behind
            safe(lambda i=i: cd.Sketches.Item(i).Delete())
        raise


def _profile_feature(s: Session, plane, loops, operation, extrude, symmetric, revolve_axis, angle, name, body) -> dict:
    from .features import _check_targets, _op
    if plane.upper() not in PLANE_AXES:
        raise InventorError("plane must be XY, XZ or YZ")
    if bool(extrude) == bool(revolve_axis):
        raise InventorError("give extrude (a distance) or revolve_axis, one of them")
    parsed = [parse_loop(lp) for lp in loops]
    d, cd = s.part(), s.cdef()
    before = s.snapshot()
    iu, iv, _ = PLANE_AXES[plane.upper()]
    tg = s.tg

    def split(e):
        t = str(e).strip()
        return (t.startswith("-"), t[1:].strip() if t.startswith("-") else t)

    def val(e) -> float:
        neg, core = split(e)
        v = 0.0 if core == "0" else _expr_mm(d, core)
        return -v if neg else v

    sk = cd.Sketches.Add(s.work_plane(plane))
    from .parametric import _first
    origin = _first(sk.AddByProjectingEntity(cd.WorkPoints.Item(1)))
    axis_u = _first(sk.AddByProjectingEntity(cd.WorkAxes.Item(iu + 1)))
    axis_v = _first(sk.AddByProjectingEntity(cd.WorkAxes.Item(iv + 1)))
    axis_u.Construction = axis_v.Construction = True
    o2 = sk.ModelToSketchSpace(tg.CreatePoint(0, 0, 0))
    unit = [0.0, 0.0, 0.0]
    unit[iu] = 1.0
    u2 = sk.ModelToSketchSpace(tg.CreatePoint(*unit))
    u_across = abs(u2.X - o2.X) > abs(u2.Y - o2.Y)
    u_dim, v_dim = (K.kHorizontalDim, K.kVerticalDim) if u_across else (K.kVerticalDim, K.kHorizontalDim)
    gc, dc = sk.GeometricConstraints, sk.DimensionConstraints
    skipped: list[str] = []

    def p2(uv):
        m = [0.0, 0.0, 0.0]
        m[iu], m[iv] = val(uv[0]) * MM, val(uv[1]) * MM
        return sk.ModelToSketchSpace(tg.CreatePoint(*m))

    def pin(sp, uv, only=None):
        """Fix a sketch point to its expressions: on an axis for "0", else a driven distance from the origin (only:
        just coordinate 0 or 1). Inventor refuses a constraint that is already implied (a point on a line that already
        lies on the axis); those are skipped, and the sketch's constraint status shows whether anything is left free."""
        g = sp.Geometry
        for i, (e, on_axis, orient) in enumerate(((uv[0], axis_v, u_dim), (uv[1], axis_u, v_dim))):
            if only is not None and i != only:
                continue
            _neg, core = split(e)
            try:
                if core == "0":
                    gc.AddCoincident(sp, on_axis)
                else:
                    dim = dc.AddTwoPointDistance(origin, sp, orient, tg.CreatePoint2d(g.X + 0.4, g.Y + 0.4))
                    dim.Parameter.Expression = core
            except Exception:  # noqa: BLE001
                skipped.append(f"{e} at {uv}")

    curves = 0
    for steps in parsed:
        if steps[0]["kind"] == "circle":
            c = sk.SketchCircles.AddByCenterRadius(p2(steps[0]["center"]), abs(val(steps[0]["radius"])) * MM)
            pin(c.CenterSketchPoint, steps[0]["center"])
            g = c.CenterSketchPoint.Geometry
            dim = dc.AddDiameter(c, tg.CreatePoint2d(g.X + 0.6, g.Y + 0.6))
            dim.Parameter.Expression = f"2 * ({steps[0]['radius']})"
            curves += 1
            continue
        start_uv = steps[0]["to"]
        segs = steps[1:] if steps[-1]["to"] == start_uv else steps[1:] + [{"kind": "line", "to": start_uv}]
        ends = [start_uv] + [sg["to"] for sg in segs]
        first_sp = prev_end = None
        for k, seg in enumerate(segs):
            a2, b2 = p2(ends[k]), p2(ends[k + 1])
            if seg["kind"] == "arc":
                ra = arc_radius_mismatch([val(x) for x in ends[k]], [val(x) for x in ends[k + 1]],
                                         [val(x) for x in seg["center"]])
                if ra > 1e-3:
                    raise InventorError(f"arc to {seg['to']} around {seg['center']}: its ends are {ra:.4f} mm apart "
                                        "in distance from the centre, so no arc fits; check the expressions")
                curve = sk.SketchArcs.AddByCenterStartEndPoint(p2(seg["center"]), a2, b2, not seg["cw"])
                cu = [val(x) for x in seg["center"]]
                if k < len(segs) - 1:
                    pin(curve.CenterSketchPoint, seg["center"])
                else:                                  # both ends fixed already: the centre has one freedom left
                    su, eu = [val(x) for x in ends[k]], [val(x) for x in ends[k + 1]]
                    pin(curve.CenterSketchPoint, seg["center"], only=0 if abs(eu[1] - su[1]) >= abs(eu[0] - su[0])
                        else 1)
            else:
                curve = sk.SketchLines.AddByTwoPoints(a2, b2)
            sp1, sp2 = curve.StartSketchPoint, curve.EndSketchPoint
            near = lambda sp, q: (sp.Geometry.X - q.X) ** 2 + (sp.Geometry.Y - q.Y) ** 2  # noqa: E731
            if near(sp1, a2) > near(sp2, a2):          # an arc stored the other way round
                sp1, sp2 = sp2, sp1
            if prev_end is not None:
                sp1.Merge(prev_end)                    # join to the previous segment (prev_end stays, pinned)
            else:
                first_sp = sp1
                pin(sp1, ends[k])
            if k < len(segs) - 1:
                if seg["kind"] == "arc":                # an arc's end has one freedom once its start and centre are
                    eu = [val(x) for x in ends[k + 1]]  # fixed: tie the coordinate that moves it along the arc
                    pin(sp2, ends[k + 1], only=0 if abs(eu[0] - cu[0]) <= abs(eu[1] - cu[1]) else 1)
                else:
                    pin(sp2, ends[k + 1])
            prev_end = sp2
            curves += 1
        prev_end.Merge(first_sp)                       # close the loop onto the pinned first point
    d.Update()
    prof = sk.Profiles.AddForSolid()
    opv = _op(operation)
    targets = s.bodies_named(body) if body and operation != "new_body" else []
    if extrude:
        ef = cd.Features.ExtrudeFeatures
        ed = ef.CreateExtrudeDefinition(prof, opv)
        ed.SetDistanceExtent(1.0, K.kSymmetricExtentDirection if symmetric else K.kPositiveExtentDirection)
        if targets:
            ed.AffectedBodies = s.collection(targets)
        feat = ef.Add(ed)
        feat.Definition.Extent.Distance.Expression = str(extrude)
    else:
        rf = cd.Features.RevolveFeatures
        ang = str(angle).strip()
        full = re.fullmatch(r"360(\.0*)?\s*(deg)?", ang) is not None
        ax = s.work_axis(revolve_axis)
        feat = rf.AddFull(prof, ax, opv) if full else rf.AddByAngle(prof, ax, math.pi, K.kPositiveExtentDirection, opv)
        if not full:
            safe(lambda: setattr(feat.Definition.Extent.Angle, "Expression", ang))
        if targets:
            safe(lambda: feat.SetAffectedBodies(s.collection(targets)))
    if name:
        from ..session import set_name
        set_name(feat, name, "feature")
    d.Update()
    s.set_sketch(sk)
    out = s.delta(feat.Name, before)
    status = sk.ConstraintStatus
    out.update(feature=feat.Name, sketch=sk.Name, curves=curves,
               fully_constrained=status == K.kFullyConstrainedConstraintStatus,
               constraint_status=K.name_of(status, "k"))
    if skipped and status != K.kFullyConstrainedConstraintStatus:
        out["warning"] = f"some positions couldn't be tied to their expressions ({skipped[:4]}); check them"
    return _check_targets(s, out, feat, targets, operation)


# --------------------------------------------------------------------------- batches
BATCH_GROUPS = {"sketch", "features", "parameters", "inspect", "properties", "materials"}
BATCH_REFUSED = {"batch", "execute_python", "rename"}
KEEP = ("feature", "sketch", "delta", "volume_change_mm3", "bodies_changed", "warning", "parameter", "edges", "faces",
        "count", "edge", "face", "created", "fully_constrained", "changed")


def resolve_refs(value, outputs: dict):
    """Replace "$name.key" strings (and inside lists/dicts) with values from earlier steps' outputs."""
    if isinstance(value, str) and value.startswith("$"):
        name, _, key = value[1:].partition(".")
        if name not in outputs:
            raise InventorError(f"{value}: no earlier step named {name!r}")
        got = outputs[name]
        for part in [p for p in key.split(".") if p]:
            if isinstance(got, dict) and part in got:
                got = got[part]
            elif isinstance(got, list) and part.isdigit() and int(part) < len(got):
                got = got[int(part)]
            else:
                raise InventorError(f"{value}: step {name!r} has no {part!r}")
        return got
    if isinstance(value, list):
        return [resolve_refs(v, outputs) for v in value]
    if isinstance(value, dict):
        return {k: resolve_refs(v, outputs) for k, v in value.items()}
    return value


def _brief(res: dict) -> dict:
    return {k: v for k, v in (res or {}).items() if k in KEEP}


@tool("features")
def batch(s: Session, steps: list[dict], rollback: bool = True) -> dict:
    """Run several modelling steps (parameters, sketches, features, selections) in one call and one undo step, in
    order. A step's output can feed a later one: "$name.key" refers to the output of the step named name, e.g.
    {"tool": "select_edges", "as": "top", "args": {"plane": {"axis": "z", "at": 10}}} then
    {"tool": "fillet", "args": {"radius": 2, "edges": "$top.edges"}}. On the first failure everything is rolled back
    (unless rollback=false) and the failed step, its tool and the error are reported. Returns a short log with each
    step's key results and time.

    steps: [{"tool": name, "args": {...}, "as": optional name for its output}]
    rollback: undo every step if one fails
    """
    if not steps:
        raise InventorError("no steps")
    for k, st in enumerate(steps, 1):
        t = TOOLS.get(str(st.get("tool", "")))
        if t is None:
            raise InventorError(f"step {k}: unknown tool {st.get('tool')!r}")
        if t.name in BATCH_REFUSED or t.group not in BATCH_GROUPS:
            raise InventorError(f"step {k}: {t.name} can't run inside a batch (only sketch, feature, parameter and "
                                "inspection tools on the working document)")
    d = s.doc()
    tm = s.app.TransactionManager
    txn = tm.StartTransaction(d, "batch")
    outputs: dict = {}
    log = []
    t_all = time.time()
    for k, st in enumerate(steps, 1):
        t0 = time.time()
        name = str(st.get("tool"))
        try:
            args = resolve_refs(dict(st.get("args") or {}), outputs)
            res = TOOLS[name].fn(s, **args)
        except Exception as e:  # noqa: BLE001
            from ..com import com_error_text
            text = com_error_text(e) if type(e).__name__ == "com_error" else str(e)
            rolled = False
            if rollback:
                try:
                    txn.Abort()
                    rolled = True
                    safe(lambda: d.Update())
                except Exception:  # noqa: BLE001
                    rolled = False
            else:
                safe(lambda: txn.End())
            from ..hints import hint_for
            h = hint_for(name, text)
            raise InventorError(f"batch step {k} ({name}{' as ' + st['as'] if st.get('as') else ''}) failed: {text}"
                                + (f". Try: {h}" if h else "")
                                + (". Every step was rolled back" if rolled else
                                   f". Steps 1-{k - 1} were kept") + f". Done before it: {log}") from None
        res = res if isinstance(res, dict) else {"result": res}
        if st.get("as"):
            outputs[str(st["as"])] = res
        log.append({"step": k, "tool": name, **({"as": st["as"]} if st.get("as") else {}), **_brief(res),
                    "s": round(time.time() - t0, 2)})
    txn.End()
    return {"steps": log, "seconds": round(time.time() - t_all, 2), "undo": "one undo step reverses the batch"}
