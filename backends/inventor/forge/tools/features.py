"""Solid features. Every geometry-changing tool returns a ``delta`` (volume / face / edge change)
so the caller can check the feature did what was intended."""
from __future__ import annotations

import math

from ..com import MM, InventorError, K, items, r, safe
from ..registry import tool
from ..session import Session
from .inspect import face_normal, find_planar_face

OPS = {"join": "kJoinOperation", "cut": "kCutOperation", "intersect": "kIntersectOperation",
       "new_body": "kNewBodyOperation"}
DIRS = {"positive": "kPositiveExtentDirection", "negative": "kNegativeExtentDirection",
        "symmetric": "kSymmetricExtentDirection"}


def _op(name: str) -> int:
    key = OPS.get(name.lower())
    if not key:
        raise InventorError(f"operation must be one of {sorted(OPS)}")
    return getattr(K, key)


def _dir(name: str) -> int:
    key = DIRS.get(name.lower())
    if not key:
        raise InventorError(f"direction must be one of {sorted(DIRS)} (or auto)")
    return getattr(K, key)


def _feature(s: Session, ref):
    """Feature by 1-based index (in the browser order) or by name."""
    feats = s.cdef().Features
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        i = int(ref)
        if not 1 <= i <= feats.Count:
            raise InventorError(f"feature {i} out of range 1..{feats.Count} (see list_features)")
        return feats.Item(i)
    for f in items(feats):
        if f.Name.lower() == str(ref).lower():
            return f
    raise InventorError(f"no feature named {ref!r} (see list_features)")


def _try_directions(s: Session, label: str, make, first: str, second: str):
    """Build a feature in one direction; if it removed/added nothing, delete it and try the other."""
    before = s.snapshot()
    feat = None
    try:
        feat = make(first)
    except Exception:
        feat = None
    after = s.snapshot()
    if feat is not None and abs(after.volume - before.volume) > 1e-6:
        return feat, first, before
    if feat is not None:
        safe(lambda: feat.Delete(True, False, False))   # keep the sketch for the retry
    feat = make(second)
    return feat, second, before


def _profile(s: Session, sketch_index: int = 0):
    sk = s.sketch(sketch_index)
    try:
        return sk, sk.Profiles.AddForSolid()
    except Exception as e:
        raise InventorError(f"sketch {sk.Name} has no closed profile — close the outline "
                            f"(draw_closed_profile, draw_rectangle...)") from e


@tool("features")
def extrude(s: Session, distance: float = 10.0, operation: str = "join", direction: str = "auto",
            through_all: bool = False, taper: float = 0.0, sketch_index: int = 0) -> dict:
    """Extrude the active sketch's closed profile(s) — all closed regions, inner loops become holes.

    distance: mm (ignored with through_all)
    operation: join | cut | intersect | new_body
    direction: auto | positive | negative | symmetric. auto = positive for join, and for cut whichever
      side actually removes material
    through_all: cut/extrude through everything
    taper: draft angle in degrees (negative tapers inward)
    sketch_index: 1-based sketch, 0 = the active sketch
    """
    sk, prof = _profile(s, sketch_index)
    ef = s.cdef().Features.ExtrudeFeatures
    opv = _op(operation)

    def make(dirname: str):
        d = ef.CreateExtrudeDefinition(prof, opv)
        if through_all:
            d.SetThroughAllExtent(_dir("positive" if dirname == "symmetric" else dirname))
        else:
            if distance <= 0:
                raise InventorError("distance must be positive (use direction for the side)")
            d.SetDistanceExtent(distance * MM, _dir(dirname))
        if taper:
            d.TaperAngle = f"{taper} deg"
        return ef.Add(d)

    if direction == "auto":
        if operation == "cut":
            feat, used, before = _try_directions(s, "Extrude", make, "negative", "positive")
        else:
            before = s.snapshot()
            feat, used = make("positive"), "positive"
    else:
        before = s.snapshot()
        feat, used = make(direction), direction
    out = s.delta(feat.Name, before)
    out.update({"feature": feat.Name, "direction": used, "sketch": sk.Name})
    return out


@tool("features")
def revolve(s: Session, axis: str = "Y", angle: float = 360.0, operation: str = "join",
            sketch_index: int = 0) -> dict:
    """Revolve the active sketch's profile about an axis.

    axis: X | Y | Z (model work axes), a work axis name, or "sketch" = the first construction line
      in the sketch (draw it with draw_line(..., construction=true))
    angle: degrees (360 = full)
    """
    sk, prof = _profile(s, sketch_index)
    rf = s.cdef().Features.RevolveFeatures
    if axis.lower() == "sketch":
        lines = [l for l in items(sk.SketchLines) if l.Construction]
        if not lines:
            raise InventorError("no construction line in the sketch to revolve about")
        ax = lines[0]
    else:
        ax = s.work_axis(axis)
    before = s.snapshot()
    if abs(angle) >= 360:
        feat = rf.AddFull(prof, ax, _op(operation))
    else:
        feat = rf.AddByAngle(prof, ax, math.radians(abs(angle)),
                             _dir("positive" if angle > 0 else "negative"), _op(operation))
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


def _hole_feature(s: Session, placement, dia_or_tap, depth, dirname, cbore, csink):
    hf = s.cdef().Features.HoleFeatures
    d = _dir(dirname)
    if depth and depth > 0:
        dep = depth * MM
        if cbore:
            return hf.AddCBoreByDistanceExtent(placement, dia_or_tap, dep, d, cbore[0] * MM, cbore[1] * MM)
        if csink:
            return hf.AddCSinkByDistanceExtent(placement, dia_or_tap, dep, d, csink[0] * MM, math.radians(csink[1]))
        return hf.AddDrilledByDistanceExtent(placement, dia_or_tap, dep, d)
    if cbore:
        return hf.AddCBoreByThroughAllExtent(placement, dia_or_tap, d, cbore[0] * MM, cbore[1] * MM)
    if csink:
        return hf.AddCSinkByThroughAllExtent(placement, dia_or_tap, d, csink[0] * MM, math.radians(csink[1]))
    return hf.AddDrilledByThroughAllExtent(placement, dia_or_tap, d)


@tool("features")
def hole(s: Session, x: float = 0.0, y: float = 0.0, diameter: float = 10.0, depth: float = 0.0,
         points: list[list[float]] | None = None, face_index: int = 0, axis: str = "Z",
         tap: str = "", tap_class: str = "6H", cbore_diameter: float = 0.0, cbore_depth: float = 0.0,
         csink_diameter: float = 0.0, csink_angle: float = 90.0, use_sketch_points: bool = False) -> dict:
    """Drilled, tapped, counterbored or countersunk hole(s), placed by model coordinates.

    x: mm, model X of the hole centre (y likewise); or give several centres in points
    points: [[x, y], ...] or [[x, y, z], ...] model coordinates (overrides x/y)
    depth: mm; 0 = through all
    face_index: planar face to drill from; 0 = the outermost face facing +axis
    axis: X | Y | Z — drilling axis used when face_index is 0 (holes go in from the + side)
    tap: thread designation, e.g. "M10x1.5" (ISO metric, class tap_class); diameter is then ignored
    cbore_diameter: counterbore Ø and cbore_depth (mm)
    csink_diameter: countersink Ø and csink_angle (degrees)
    use_sketch_points: drill every hole-centre point of the active sketch instead (draw_point)
    """
    cd = s.cdef()
    if use_sketch_points:
        sk = s.sketch()
        pts = [p for p in items(sk.SketchPoints) if p.HoleCenter]
        if not pts:
            raise InventorError("the active sketch has no hole-centre points")
    else:
        first = (points or [[x, y]])[0]
        face = s.face(face_index) if face_index else (find_planar_face(s, axis, at=first[:2]) or
                                                      find_planar_face(s, axis))
        if face is None:
            raise InventorError(f"no planar face facing +{axis}; pass face_index")
        sk = cd.Sketches.Add(face, False)
        n = face_normal(face)
        p0 = face.PointOnFace
        centres = points if points else [[x, y]]
        pts = []
        for c in centres:
            if len(c) >= 3:
                mp = s.p3(*c[:3])
            else:
                # put the 2-D coordinate on the face plane along the face normal
                ax = max(range(3), key=lambda i: abs(n[i]))
                full = [0.0, 0.0, 0.0]
                rest = [i for i in range(3) if i != ax]
                full[rest[0]], full[rest[1]] = c[0], c[1]
                full[ax] = [p0.X, p0.Y, p0.Z][ax] * 10
                mp = s.p3(*full)
            pts.append(sk.SketchPoints.Add(sk.ModelToSketchSpace(mp), True))
    placement = cd.Features.HoleFeatures.CreateSketchPlacementDefinition(s.collection(pts))
    if tap:
        dia_or_tap = cd.Features.HoleFeatures.CreateTapInfo(True, "ISO Metric profile", tap, tap_class, True)
    else:
        if diameter <= 0:
            raise InventorError("diameter must be positive")
        dia_or_tap = diameter * MM
    cbore = (cbore_diameter, cbore_depth) if cbore_diameter else None
    csink = (csink_diameter, csink_angle) if csink_diameter else None

    feat, used, before = _try_directions(
        s, "Hole", lambda dn: _hole_feature(s, placement, dia_or_tap, depth, dn, cbore, csink),
        "positive", "negative")
    out = s.delta(feat.Name, before)
    out.update({"feature": feat.Name, "holes": len(pts), "kind": "tapped " + tap if tap else
                ("counterbored" if cbore else "countersunk" if csink else "drilled"),
                "through": not depth})
    return out


@tool("features")
def hole_linear(s: Session, face_index: int, edge_one: int, distance_one: float, edge_two: int,
                distance_two: float, diameter: float = 10.0, depth: float = 0.0, tap: str = "") -> dict:
    """Hole on a planar face positioned by distances from two of its edges (Inventor's linear placement).

    distance_one: mm from edge_one (edge indices from list_edges)
    """
    cd = s.cdef()
    face = s.face(face_index)
    hf = cd.Features.HoleFeatures
    bias = face.PointOnFace
    placement = hf.CreateLinearPlacementDefinition(face, s.edge(edge_one), distance_one * MM,
                                                   s.edge(edge_two), distance_two * MM, bias)
    dia = hf.CreateTapInfo(True, "ISO Metric profile", tap, "6H", True) if tap else diameter * MM
    feat, used, before = _try_directions(
        s, "Hole", lambda dn: _hole_feature(s, placement, dia, depth, dn, None, None), "positive", "negative")
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def fillet(s: Session, radius: float, edges: list[int]) -> dict:
    """Constant-radius fillet on edges (1-based indices from list_edges / find_edge)."""
    before = s.snapshot()
    ec = s.edge_collection([s.edge(i) for i in edges])
    feat = s.cdef().Features.FilletFeatures.AddSimple(ec, radius * MM)
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def chamfer(s: Session, distance: float, edges: list[int], distance_two: float = 0.0, angle: float = 0.0,
            face_index: int = 0) -> dict:
    """Chamfer edges: equal distance, two distances, or distance + angle (needs face_index for the
    reference face of the last two forms)."""
    before = s.snapshot()
    cf = s.cdef().Features.ChamferFeatures
    ec = s.edge_collection([s.edge(i) for i in edges])
    if distance_two:
        feat = cf.AddUsingTwoDistances(ec, s.face(face_index), distance * MM, distance_two * MM)
    elif angle:
        feat = cf.AddUsingDistanceAndAngle(ec, s.face(face_index), distance * MM, math.radians(angle))
    else:
        feat = cf.AddUsingDistance(ec, distance * MM)
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def circular_pattern(s: Session, features: list[str], count: int = 6, angle: float = 360.0,
                     axis: str = "Z") -> dict:
    """Circular pattern of features (names or 1-based indices) about an axis (X/Y/Z or work axis name)."""
    before = s.snapshot()
    parents = s.collection([_feature(s, f) for f in features])
    feat = s.cdef().Features.CircularPatternFeatures.Add(parents, s.work_axis(axis), True, int(count),
                                                         f"{angle} deg", True)
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def rectangular_pattern(s: Session, features: list[str], x_count: int = 2, x_spacing: float = 20.0,
                        y_count: int = 1, y_spacing: float = 20.0, x_axis: str = "X", y_axis: str = "Y") -> dict:
    """Rectangular (grid) pattern of features along two axes; spacing in mm (negative flips the direction)."""
    before = s.snapshot()
    parents = s.collection([_feature(s, f) for f in features])
    rp = s.cdef().Features.RectangularPatternFeatures
    if y_count > 1:
        feat = rp.Add(parents, s.work_axis(x_axis), x_spacing >= 0, int(x_count), abs(x_spacing) * MM,
                      YDirectionEntity=s.work_axis(y_axis), NaturalYDirection=y_spacing >= 0,
                      YCount=int(y_count), YSpacing=abs(y_spacing) * MM)
    else:
        feat = rp.Add(parents, s.work_axis(x_axis), x_spacing >= 0, int(x_count), abs(x_spacing) * MM)
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def mirror(s: Session, features: list[str], plane: str = "YZ") -> dict:
    """Mirror features (names / indices) across a plane (XY, XZ, YZ or work plane name)."""
    before = s.snapshot()
    parents = s.collection([_feature(s, f) for f in features])
    feat = s.cdef().Features.MirrorFeatures.Add(parents, s.work_plane(plane))
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def shell(s: Session, thickness: float, remove_faces: list[int] | None = None, direction: str = "inside") -> dict:
    """Hollow the body to a wall thickness, leaving the listed faces open.

    direction: inside | outside | both
    """
    before = s.snapshot()
    sf = s.cdef().Features.ShellFeatures
    faces = s.face_collection([s.face(i) for i in (remove_faces or [])])
    dmap = {"inside": "kInsideShellDirection", "outside": "kOutsideShellDirection", "both": "kBothSidesShellDirection"}
    d = sf.CreateShellDefinition(faces, thickness * MM, getattr(K, dmap.get(direction, "kInsideShellDirection")))
    feat = sf.Add(d)
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def thread(s: Session, face_index: int, designation: str = "M10x1.5", internal: bool = False,
           thread_class: str = "", length: float = 0.0) -> dict:
    """Cosmetic (modelled-as-annotation) ISO metric thread on a cylindrical face — shows on drawings
    and in the BOM without heavy helical geometry.

    designation: e.g. M10x1.5, M8x1.25
    length: mm; 0 = full length
    """
    tf = s.cdef().Features.ThreadFeatures
    face = s.face(face_index)
    if face.SurfaceType != K.kCylinderSurface:
        raise InventorError(f"face {face_index} is not cylindrical")
    cls = thread_class or ("6H" if internal else "6g")
    info = tf.CreateStandardThreadInfo(internal, True, "ISO Metric profile", designation, cls)
    start = face.Edges.Item(1)
    before = s.snapshot()
    if length:
        feat = tf.Add(face, start, info, False, False, length * MM, 0)
    else:
        feat = tf.Add(face, start, info, False, True)
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def sweep(s: Session, profile_sketch: int, path_sketch: int, operation: str = "join") -> dict:
    """Sweep a closed profile (sketch index) along a path (first curve chain of another sketch)."""
    cd = s.cdef()
    _, prof = _profile(s, profile_sketch)
    psk = s.sketch(path_sketch)
    ents = [e for e in items(psk.SketchEntities) if safe(lambda e=e: e.Type != K.kSketchPointObject, False)]
    if not ents:
        raise InventorError("path sketch has no curves")
    path = cd.Features.SweepFeatures.CreatePath(ents[0])
    before = s.snapshot()
    feat = cd.Features.SweepFeatures.AddUsingPath(prof, path, _op(operation))
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def loft(s: Session, sketches: list[int], operation: str = "join") -> dict:
    """Loft through the closed profiles of two or more sketches (1-based indices, in order)."""
    cd = s.cdef()
    secs = s.collection([s.sketch(i).Profiles.AddForSolid() for i in sketches])
    lf = cd.Features.LoftFeatures
    before = s.snapshot()
    feat = lf.Add(lf.CreateLoftDefinition(secs, _op(operation)))
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def work_plane(s: Session, base: str = "XY", offset: float = 0.0, angle: float = 0.0, axis: str = "",
               name: str = "") -> dict:
    """Work plane offset from a base plane (mm), or rotated about an axis by an angle (degrees).

    base: XY | XZ | YZ | existing work plane name
    axis: X | Y | Z — required for angle planes
    name: optional name to refer to it later (create_sketch(plane=name), mirror(plane=name))
    """
    wps = s.cdef().WorkPlanes
    if angle:
        if not axis:
            raise InventorError("an angled work plane needs an axis")
        wp = wps.AddByLinePlaneAndAngle(s.work_axis(axis), s.work_plane(base), math.radians(angle))
    else:
        wp = wps.AddByPlaneAndOffset(s.work_plane(base), offset * MM)
    if name:
        wp.Name = name
    return {"work_plane": wp.Name}


@tool("features")
def work_axis(s: Session, face_index: int = 0, edge_index: int = 0, name: str = "") -> dict:
    """Work axis through a cylindrical face's axis or along a straight edge (for patterns/revolves)."""
    was = s.cdef().WorkAxes
    if face_index:
        wa = was.AddByRevolvedFace(s.face(face_index))
    elif edge_index:
        wa = was.AddByLine(s.edge(edge_index))
    else:
        raise InventorError("give face_index (cylindrical face) or edge_index")
    if name:
        wa.Name = name
    return {"work_axis": wa.Name}


@tool("features", mutates=False)
def list_features(s: Session) -> dict:
    """Feature tree: index, name, type, suppressed, health."""
    out = []
    for i, f in enumerate(items(s.cdef().Features), 1):
        out.append({"index": i, "name": f.Name, "type": K.name_of(f.Type, "k").replace("k", "", 1).replace("Object", ""),
                    "suppressed": bool(safe(lambda f=f: f.Suppressed, False)),
                    "health": K.name_of(safe(lambda f=f: f.HealthStatus, 0), "k").replace("HealthStatus", "")})
    return {"features": out, "count": len(out)}


@tool("features")
def delete_feature(s: Session, features: list[str], keep_sketches: bool = False) -> dict:
    """Delete features by name or 1-based index (dependent features go too)."""
    before = s.snapshot()
    targets = [_feature(s, f) for f in features]
    names = [t.Name for t in targets]
    for t in targets:
        t.Delete(bool(keep_sketches), False, False)
    out = s.delta("Delete " + ", ".join(names), before)
    out["deleted"] = names
    return out


@tool("features")
def suppress_feature(s: Session, feature: str, suppress: bool = True) -> dict:
    """Suppress (or unsuppress) a feature without deleting it — handy for what-if checks."""
    before = s.snapshot()
    f = _feature(s, feature)
    f.Suppressed = bool(suppress)
    s.part().Update()
    out = s.delta(("Suppress " if suppress else "Unsuppress ") + f.Name, before)
    return out


@tool("features")
def coil(s: Session, pitch: float, revolutions: float, axis: str = "Z", operation: str = "join", clockwise: bool = False,
         sketch_index: int = 0) -> dict:
    """Coil / spring: sweep the active sketch's closed profile helically about an axis.

    pitch: mm per turn
    axis: X | Y | Z or a work axis name; the profile must be offset from this axis
    """
    _, prof = _profile(s, sketch_index)
    before = s.snapshot()
    feat = s.cdef().Features.CoilFeatures.AddByPitchAndRevolution(prof, s.work_axis(axis), pitch * MM, float(revolutions),
                                                                  _op(operation), ClockwiseRotation=bool(clockwise))
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


def _bodies(s: Session):
    return list(items(s.cdef().SurfaceBodies))


@tool("features")
def combine_bodies(s: Session, base: int = 1, tools: list[int] | None = None, operation: str = "join",
                   keep_tools: bool = False) -> dict:
    """Boolean between solid bodies of a multi-body part. operation: join | cut | intersect.

    base: 1-based body kept as the result
    tools: bodies used as tools (default: every other body)
    """
    bodies = _bodies(s)
    if len(bodies) < 2:
        raise InventorError("the part needs at least two solid bodies")
    tool_ids = tools or [i for i in range(1, len(bodies) + 1) if i != base]
    before = s.snapshot()
    feat = s.cdef().Features.CombineFeatures.Add(bodies[base - 1], s.collection([bodies[i - 1] for i in tool_ids]),
                                                 _op(operation), bool(keep_tools))
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def split_part(s: Session, plane: str = "XY", remove: str = "negative", keep_both: bool = False) -> dict:
    """Cut the part with a plane (XY/XZ/YZ or work plane name), removing one side, or split it into two bodies.

    remove: positive | negative — the side discarded (ignored with keep_both)
    keep_both: split into two solid bodies, nothing removed
    """
    sf = s.cdef().Features.SplitFeatures
    before = s.snapshot()
    if keep_both:
        feat = sf.SplitBody(s.work_plane(plane), s.body())
    else:
        feat = sf.TrimSolid(s.work_plane(plane), s.body(), remove.lower().startswith("pos"))
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    out["bodies"] = s.cdef().SurfaceBodies.Count
    return out


@tool("features")
def thicken_face(s: Session, faces: list[int], distance: float, operation: str = "join", direction: str = "positive") -> dict:
    """Thicken or offset faces by a distance (mm). operation join adds material, cut removes it."""
    before = s.snapshot()
    fc = s.face_collection([s.face(i) for i in faces])
    feat = s.cdef().Features.ThickenFeatures.Add(fc, distance * MM, _dir(direction), _op(operation), False)
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def face_draft(s: Session, faces: list[int], angle: float, pull_plane: str = "XY") -> dict:
    """Mould draft on faces by an angle (degrees) relative to a pull direction normal to a plane (fixed at that plane)."""
    fd = s.cdef().Features.FaceDraftFeatures
    d = fd.CreateFaceDraftDefinition()
    d.SetFixedPlane(s.face_collection([s.face(i) for i in faces]), s.work_plane(pull_plane), f"{angle} deg")
    before = s.snapshot()
    feat = fd.Add(d)
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("features")
def emboss_text(s: Session, text: str, x: float = 0.0, y: float = 0.0, depth: float = 0.5, engrave: bool = True,
                height: float = 5.0, face_index: int = 0) -> dict:
    """Engrave (or emboss) text into a planar face. With face_index the text is centred on the face and (x, y) are
    offsets from that centre in mm; without it (x, y) are coordinates in the active sketch. height is the text height.

    face_index: planar face (0 = the active sketch)
    """
    cd = s.cdef()
    if face_index:
        sk = cd.Sketches.Add(s.face(face_index), False)
        s.set_sketch(sk)
    else:
        sk = s.sketch()
    size = f"{height * MM:.4f}"
    box = sk.TextBoxes.AddFitted(s.p2(x, y), f'<StyleOverride FontSize="{size}">{text}</StyleOverride>')
    if face_index:                     # a face sketch's origin can be at a corner; centre the text on the face
        rb = s.face(face_index).Evaluator.RangeBox
        mid = s.tg.CreatePoint((rb.MinPoint.X + rb.MaxPoint.X) / 2, (rb.MinPoint.Y + rb.MaxPoint.Y) / 2,
                               (rb.MinPoint.Z + rb.MaxPoint.Z) / 2)
        c2 = sk.ModelToSketchSpace(mid)
        box.Origin = s.tg.CreatePoint2d(c2.X + x * MM - box.Width / 2, c2.Y + y * MM + box.Height / 2)
    prof = sk.Profiles.AddForSolid()
    ef = cd.Features.EmbossFeatures
    fn = ef.AddEngraveFromFace if engrave else ef.AddEmbossFromFace
    feat, _used, before = _try_directions(s, "Emboss", lambda dn: fn(prof, depth * MM, _dir(dn)), "negative", "positive")
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


def _on_side(px, py, members, skip) -> bool:
    """Does an end point lie inside another member's centre line (a T-joint, not a corner)?"""
    for j, (a1, b1, a2, b2) in enumerate(members, 1):
        if j == skip:
            continue
        L = math.hypot(a2 - a1, b2 - b1)
        if L < 1e-9:
            continue
        t = ((px - a1) * (a2 - a1) + (py - b1) * (b2 - b1)) / L ** 2
        if 1e-6 < t < 1 - 1e-6 and math.hypot(a1 + t * (a2 - a1) - px, b1 + t * (b2 - b1) - py) < 1e-3:
            return True
    return False


def _corner(px, py, members, skip) -> bool:
    return any(j != skip and (math.hypot(a1 - px, b1 - py) < 1e-3 or math.hypot(a2 - px, b2 - py) < 1e-3)
               for j, (a1, b1, a2, b2) in enumerate(members, 1))


def _cut_list(members: list[list[float]], width: float) -> list[dict]:
    """Length and end angles of each member, as a fabricator cuts them: an end that meets another member at a corner
    is mitred at half the angle between them; a free end or a member butting into another's side is square."""
    rows = []
    for i, (x1, y1, x2, y2) in enumerate(members, 1):
        length = math.hypot(x2 - x1, y2 - y1)
        ends = []
        for (px, py), (qx, qy) in (((x1, y1), (x2, y2)), ((x2, y2), (x1, y1))):
            angle = 90.0
            for j, (a1, b1, a2, b2) in enumerate(members, 1):
                if j == i:
                    continue
                for (ex, ey), (fx, fy) in (((a1, b1), (a2, b2)), ((a2, b2), (a1, b1))):
                    if math.hypot(ex - px, ey - py) < 1e-3:          # two members meet end to end: a mitred corner
                        u = math.atan2(qy - py, qx - px)
                        v = math.atan2(fy - ey, fx - ex)
                        between = abs((math.degrees(u - v) + 180) % 360 - 180)
                        angle = between / 2
            ends.append(round(angle, 1))
        stock = length
        for (px, py), a in zip(((x1, y1), (x2, y2)), ends):
            if _on_side(px, py, members, i):
                stock -= width / 2                     # butts into the side of another tube
            elif 1 < a < 89.9:
                stock += (width / 2) / math.tan(math.radians(a))   # mitred: the outside edge runs past the centre line
        rows.append({"member": i, "length_mm": round(length, 1), "end_angles_deg": ends, "stock_length_mm": round(stock, 1),
                     "joints": ["corner" if _corner(px, py, members, i) else "tee" if _on_side(px, py, members, i) else "free"
                                for px, py in ((x1, y1), (x2, y2))]})
    return rows


def _frame_params(s: Session, height: float, wall: float) -> list[str]:
    """User parameters Frame_Height and Frame_Wall drive the frame, so a change shows in the design history."""
    ups = s.cdef().Parameters.UserParameters
    made = []
    for name, val in (("Frame_Height", height), ("Frame_Wall", wall)):
        if val <= 0:
            continue
        p = safe(lambda name=name: ups.Item(name))
        if p is None:
            ups.AddByExpression(name, f"{val:g} mm", "mm")
        else:
            p.Expression = f"{val:g} mm"
        made.append(name)
    return made


@tool("features")
def frame_members(s: Session, members: list[list[float]], width: float = 32.0, height: float = 32.0, wall: float = 0.0,
                  z: float = 0.0, name: str = "Frame") -> dict:
    """A welded box-section frame in one step, from member centre lines in the XY plane: every member becomes a
    width x height bar, all joined into one solid; wall > 0 then hollows it into tube of that wall thickness (do this
    before adding pads and brackets, which should stay solid). Returns a cut list: each member's centre-line length, its
    end angles (between the cut face and the member's axis: 90 = square cut, 45 = mitred corner) and the stock length to cut.

    members: [[x1, y1, x2, y2], ...] centre lines in mm; members that share an end point form a mitred corner
    width: section width across the centre line, mm
    height: section height (extruded upwards from z), mm
    wall: tube wall thickness, mm (0 = solid bar); height and wall become the user parameters Frame_Height and Frame_Wall,
      so set_parameter(Frame_Wall, "2.5 mm") makes the tubes thinner
    z: height of the underside, mm
    """
    from .sketch import create_sketch, draw_closed_profile
    if not members:
        raise InventorError("give at least one member [x1, y1, x2, y2]")
    before = s.snapshot()
    create_sketch(s, plane="XY", offset=z)
    hw = width / 2.0
    for x1, y1, x2, y2 in members:
        L = math.hypot(x2 - x1, y2 - y1)
        if L < 1e-6:
            raise InventorError("a member has zero length")
        ux, uy = (x2 - x1) / L, (y2 - y1) / L
        nx, ny = -uy * hw, ux * hw
        i = members.index([x1, y1, x2, y2]) + 1
        # only an end at a corner runs on by half a width (so the corner closes); free ends and tee ends stop at the line
        e1 = hw if _corner(x1, y1, members, i) else 0.0
        e2 = hw if _corner(x2, y2, members, i) else 0.0
        draw_closed_profile(s, [[x1 - ux * e1 + nx, y1 - uy * e1 + ny], [x2 + ux * e2 + nx, y2 + uy * e2 + ny],
                                [x2 + ux * e2 - nx, y2 + uy * e2 - ny], [x1 - ux * e1 - nx, y1 - uy * e1 - ny]])
    members = [list(map(float, m)) for m in members]
    ext = extrude(s, distance=height, direction="positive", operation="join" if s.snapshot().bodies else "new_body")
    feats = [ext.get("feature")]
    params = _frame_params(s, height, wall)
    ef = s.cdef().Features.ExtrudeFeatures.Item(s.cdef().Features.ExtrudeFeatures.Count)
    safe(lambda: setattr(ef.Definition.Extent.Distance, "Expression", "Frame_Height"))
    if wall > 0:
        try:
            feats.append(shell(s, thickness=wall).get("feature"))
        except Exception as e:                       # leave no half-built frame behind
            safe(lambda: ef.Delete())
            raise InventorError(f"frame_members: hollowing failed ({e}); nothing was kept") from e
        sf = s.cdef().Features.ShellFeatures.Item(s.cdef().Features.ShellFeatures.Count)
        safe(lambda: setattr(sf.Definition.Thickness, "Expression", "Frame_Wall"))
        s.doc().Update()
    out = s.delta(name, before)
    out.update(features=feats, members=len(members), cut_list=_cut_list(members, width), parameters=params,
               total_length_mm=round(sum(math.hypot(m[2] - m[0], m[3] - m[1]) for m in members), 1))
    return out


def _tube_members(members, od, wall):
    out = []
    for m in members:
        if isinstance(m, dict):
            line, mod, mwall = m.get("line") or m.get("points"), float(m.get("od", od)), float(m.get("wall", wall))
            label = m.get("name", "")
        else:
            line, mod, mwall, label = m, float(od), float(wall), ""
        if not line or len(line) != 6:
            raise InventorError("each member is [x1, y1, z1, x2, y2, z2] (or {line: [...], od, wall, name})")
        line = [float(v) for v in line]
        if math.dist(line[:3], line[3:]) < 1e-6:
            raise InventorError("a member has zero length")
        if mwall * 2 >= mod or mwall < 0:
            raise InventorError(f"wall {mwall} mm doesn't fit a {mod} mm tube (0 = solid rod)")
        out.append({"a": line[:3], "b": line[3:], "od": mod, "wall": mwall, "name": label})
    return out


def _end_kind(p, i, ms) -> str:
    """corner: the end meets another member's end at an angle; inline: it continues another member straight on;
    tee: it lands on another member's side; free otherwise."""
    me = ms[i]
    d0 = [me["b"][k] - me["a"][k] for k in range(3)]
    shared = []
    for j, m in enumerate(ms):
        if j == i:
            continue
        if math.dist(p, m["a"]) < 0.01 or math.dist(p, m["b"]) < 0.01:
            d1 = [m["b"][k] - m["a"][k] for k in range(3)]
            c = abs(sum(d0[k] * d1[k] for k in range(3))) / (math.dist(me["a"], me["b"]) * math.dist(m["a"], m["b"]))
            shared.append(c)
    if shared:
        return "inline" if all(c > 0.985 for c in shared) else "corner"
    for j, m in enumerate(ms):
        if j == i:
            continue
        a, b = m["a"], m["b"]
        ab = [b[k] - a[k] for k in range(3)]
        L2 = sum(v * v for v in ab)
        t = sum((p[k] - a[k]) * ab[k] for k in range(3)) / L2
        if 0 < t < 1 and math.dist(p, [a[k] + t * ab[k] for k in range(3)]) < m["od"] / 2 + 0.01:
            return "tee"
    return "free"


@tool("features")
def tube_frame(s: Session, members: list, od: float = 33.7, wall: float = 2.6, name: str = "Tube frame") -> dict:
    """A welded round-tube (CHS) frame in 3D, in one step: each member is a straight tube on a centre line
    [x1, y1, z1, x2, y2, z2] (mm). All members are swept solid and joined, then bored to the wall thickness, so
    joints are welded and tee'd tubes are coped into their neighbours. Ends that meet another member's end (corners)
    run on by half a diameter so the corner closes. Use real sizes (e.g. AS/NZS 1163 CHS 33.7x2.6, 26.9x2.3).
    Returns centreline lengths and untrimmed axial blank lengths including corner run-ons. Tee blanks reach the host centreline for subsequent coping. Stock lengths use the constructed sweep extents.

    members: [[x1, y1, z1, x2, y2, z2], ...] or [{"line": [...], "od": 26.9, "wall": 2.3, "name": "seat cross"}, ...]
    od: default outside diameter, mm
    wall: default wall thickness, mm (0 = solid rod; a short solid member makes a disc, e.g. a mounting tab)
    """
    ms = _tube_members(members, od, wall)
    if not ms:
        raise InventorError("give at least one tube member")
    tx = s.app.TransactionManager.StartTransaction(s.doc(), name)
    try:
        before = s.snapshot()
        cd = s.cdef()
        feats = cd.Features
        sk3 = cd.Sketches3D.Add()
        lines, extended = [], []
        for i, m in enumerate(ms):
            a, b = list(m["a"]), list(m["b"])
            L = math.dist(a, b)
            u = [(b[k] - a[k]) / L for k in range(3)]
            ka, kb = _end_kind(m["a"], i, ms), _end_kind(m["b"], i, ms)
            m["ends"] = [ka, kb]
            ea = m["od"] / 2 if ka == "corner" else 0.0
            eb = m["od"] / 2 if kb == "corner" else 0.0
            a2 = [a[k] - u[k] * ea for k in range(3)]
            b2 = [b[k] + u[k] * eb for k in range(3)]
            lines.append(sk3.SketchLines3D.AddByTwoPoints(s.p3(*a2), s.p3(*b2)))
            extended.append((a2, b2))
    
        def sweep(i, radius, op):
            ln = lines[i]
            wp = cd.WorkPlanes.AddByNormalToCurve(ln, ln.StartSketchPoint)
            safe(lambda: setattr(wp, "Visible", False))
            sk = cd.Sketches.Add(wp)
            c = sk.ModelToSketchSpace(s.p3(*extended[i][0]))
            sk.SketchCircles.AddByCenterRadius(c, radius * MM)
            prof = sk.Profiles.AddForSolid()
            path = feats.CreatePath(ln)
            return feats.SweepFeatures.AddUsingPath(prof, path, getattr(K, op))
    
        made = []
        for i, m in enumerate(ms):
            # join into the part's body (verified live in Inventor 2027.1); the first member starts it
            op = "kJoinOperation" if (i or s.snapshot().bodies) else "kNewBodyOperation"
            try:
                made.append(sweep(i, m["od"] / 2, op).Name)
            except Exception as e:
                raise InventorError(f"tube_frame: member {i + 1} ({m['name'] or m['a']}) failed: {e}") from e
        for i, m in enumerate(ms):
            if m["wall"] <= 0:
                continue                                  # solid rod / disc
            try:
                made.append(sweep(i, m["od"] / 2 - m["wall"], "kCutOperation").Name)
            except Exception as e:
                raise InventorError(f"tube_frame: boring member {i + 1} failed: {e}") from e
        safe(lambda: setattr(sk3, "Visible", False))
        s.doc().Update()
        out = s.delta(name, before)
        cut = []
        for i, m in enumerate(ms, 1):
            L = math.dist(m["a"], m["b"])
            cut.append({"member": i, "name": m["name"] or f"M{i}", "section": f"{m['od']:g}x{m['wall']:g} CHS" if m["wall"] > 0 else f"{m['od']:g} round bar",
                        "length_mm": r(L, 1), "ends": m["ends"],
                        "stock_mm": r(math.dist(*extended[i - 1]), 1)})
        by_section = {}
        for c in cut:
            by_section[c["section"]] = r(by_section.get(c["section"], 0) + c["stock_mm"], 1)
        out.update(features=made, members=len(ms), cut_list=cut, stock_by_section_mm=by_section,
                   stock_length_convention="Untrimmed axial blank length of the constructed sweep, including corner run-ons. Tee blanks extend to the host centreline for coping. No saddle-depth deduction is applied.")
        tx.End()
        return out
    except Exception:
        tx.Abort()
        raise
