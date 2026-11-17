"""Parametric modelling helpers: linked parameters, fully constrained parametric profiles, model states,
named faces and split faces."""
from __future__ import annotations

import os

from ..com import MM, InventorError, K, items, r, safe
from ..registry import tool
from ..session import Session, set_name
from .assembly import _occ

NAME_SET, NAME_ATTR = "iLogicEntityNameSet", "iLogicEntityName"      # the names Inventor's own Assign Name uses
PLANE_AXES = {"XY": (0, 1, 2), "XZ": (0, 2, 1), "YZ": (1, 2, 0)}      # plane -> indices of (u, v, normal)


def _first(x):
    """Projecting into a sketch can hand back a collection or the single entity."""
    try:
        return x.Item(1)
    except Exception:
        return x


@tool("parameters")
def link_parameters(s: Session, source: str, names: list[str] | None = None) -> dict:
    """Link user parameters from another Inventor file (a master part) into this part or assembly, so one file drives
    them all. Linking the same file again adds any new names to the existing link.

    source: path to the file holding the parameters
    names: which parameters (default: all its user parameters)
    """
    path = os.path.abspath(os.path.expanduser(source))
    if not os.path.exists(path):
        raise InventorError(f"no such file: {path}")
    d = s.doc()
    cur = s.app.ActiveDocument
    src = s.app.Documents.Open(path, False)
    if cur is not None:
        safe(lambda: cur.Activate())
    user = list(items(src.ComponentDefinition.Parameters.UserParameters))
    chosen = [p for p in user if not names or p.Name in names]
    missing = sorted(set(names or []) - {p.Name for p in user})
    if missing or not chosen:
        raise InventorError(f"parameters not found in {os.path.basename(path)}: {missing or 'it has no user parameters'}")
    params = d.ComponentDefinition.Parameters
    tables = params.DerivedParameterTables
    existing = next((t for t in items(tables)
                     if safe(lambda t=t: t.ReferencedDocumentDescriptor.FullDocumentName, "").lower() == path.lower()), None)
    if existing is not None:
        existing.LinkedParameters = s.collection(chosen)
    else:
        tables.Add2(path, s.collection(chosen))
    d.Update()
    return {"linked": [{"name": p.Name, "expression": params.Item(p.Name).Expression} for p in chosen], "from": path}


def _expr_mm(d, text) -> float:
    """Value of a length expression in mm (it can use the document's parameters)."""
    return float(d.UnitsOfMeasure.GetValueFromExpression(str(text), K.kMillimeterLengthUnits)) * 10.0


@tool("features")
def parametric_extrusion(s: Session, plane: str, vertices: list[list], width: str, name: str = "", operation: str = "join",
                         symmetric: bool = True) -> dict:
    """A closed profile whose every corner is driven by parameter expressions, extruded to a parametric width. The sketch
    comes out fully constrained, so changing a parameter (or a linked master) reshapes the part reliably.

    plane: XY, XZ or YZ
    vertices: corners in order as [u, v] along the plane's two axes, e.g. [["0", "0"], ["L", "0"], ["L", "H"], ["0", "H"]];
      "0" puts the corner on the axis, a leading "-" gives the negative side
    width: extrusion distance expression, e.g. "T" or "12 mm"
    name: feature name
    operation: join | cut | intersect | new_body
    symmetric: extrude equally both ways from the plane
    """
    from .features import _op
    if plane.upper() not in PLANE_AXES:
        raise InventorError("plane must be XY, XZ or YZ")
    if len(vertices) < 3:
        raise InventorError("a profile needs at least three corners")
    d, cd = s.part(), s.cdef()
    before = s.snapshot()
    iu, iv, _ = PLANE_AXES[plane.upper()]

    def split(e) -> tuple[bool, str]:
        t = str(e).strip()
        return (t.startswith("-"), t[1:].strip() if t.startswith("-") else t)

    def value(e) -> float:
        neg, core = split(e)
        v = 0.0 if core == "0" else _expr_mm(d, core)
        return -v if neg else v

    sk = cd.Sketches.Add(s.work_plane(plane))
    origin = _first(sk.AddByProjectingEntity(cd.WorkPoints.Item(1)))
    axis_u = _first(sk.AddByProjectingEntity(cd.WorkAxes.Item(iu + 1)))
    axis_v = _first(sk.AddByProjectingEntity(cd.WorkAxes.Item(iv + 1)))
    axis_u.Construction = axis_v.Construction = True
    # which model axis runs across the sketch depends on the plane
    o2 = sk.ModelToSketchSpace(s.tg.CreatePoint(0, 0, 0))
    unit = [0.0, 0.0, 0.0]
    unit[iu] = 1.0
    u2 = sk.ModelToSketchSpace(s.tg.CreatePoint(*unit))
    u_across = abs(u2.X - o2.X) > abs(u2.Y - o2.Y)
    horiz, vert = K.kHorizontalDim, K.kVerticalDim
    u_dim, v_dim = (horiz, vert) if u_across else (vert, horiz)

    pts = []
    for u, v in vertices:
        m = [0.0, 0.0, 0.0]
        m[iu], m[iv] = value(u) * MM, value(v) * MM
        pts.append(sk.ModelToSketchSpace(s.tg.CreatePoint(*m)))
    lines: list = []
    n = len(pts)
    for i in range(n):
        if i == 0:
            ln = sk.SketchLines.AddByTwoPoints(pts[0], pts[1])
        elif i < n - 1:
            ln = sk.SketchLines.AddByTwoPoints(lines[-1].EndSketchPoint, pts[i + 1])
        else:
            ln = sk.SketchLines.AddByTwoPoints(lines[-1].EndSketchPoint, lines[0].StartSketchPoint)
        lines.append(ln)
    gc, dc = sk.GeometricConstraints, sk.DimensionConstraints
    for (u, v), ln in zip(vertices, lines):
        p = ln.StartSketchPoint
        g = p.Geometry
        for e, on_axis, orient in ((u, axis_v, u_dim), (v, axis_u, v_dim)):
            _neg, core = split(e)
            if core == "0":                   # a corner at the origin goes on both axes; tying it to the origin point fails
                gc.AddCoincident(p, on_axis)
            else:
                dim = dc.AddTwoPointDistance(origin, p, orient, s.tg.CreatePoint2d(g.X + 0.4, g.Y + 0.4))
                dim.Parameter.Expression = core
    d.Update()
    ef = cd.Features.ExtrudeFeatures
    ed = ef.CreateExtrudeDefinition(sk.Profiles.AddForSolid(), _op(operation))
    ed.SetDistanceExtent(1.0, K.kSymmetricExtentDirection if symmetric else K.kPositiveExtentDirection)
    feat = ef.Add(ed)
    if name:
        set_name(feat, name, "feature")
    feat.Definition.Extent.Distance.Expression = str(width)
    d.Update()
    s.set_sketch(sk)
    out = s.delta(feat.Name, before)
    status = sk.ConstraintStatus
    out.update(feature=feat.Name, sketch=sk.Name, fully_constrained=status == K.kFullyConstrainedConstraintStatus,
               constraint_status=K.name_of(status, "k"))
    return out


# --------------------------------------------------------------------------- model states
def _states(s: Session):
    states = safe(lambda: s.doc().ComponentDefinition.ModelStates)
    if states is None:
        raise InventorError("this document has no model states")
    return states


def _state_rows(states) -> list[dict]:
    active = safe(lambda: states.ActiveModelState.Name, "")
    return [{"name": m.Name, "active": m.Name == active} for m in items(states)]


@tool("parameters", mutates=False)
def list_model_states(s: Session) -> dict:
    """Model states of the active part or assembly (variants of one file, e.g. with different features suppressed)."""
    return {"states": _state_rows(_states(s))}


def _suppressible(s: Session, name: str):
    """A feature of the active part, or an occurrence of the active assembly, by name."""
    d = s.doc()
    if s.kind(d) == "assembly":
        return _occ(s, name)
    from .features import _feature
    return _feature(s, name)


def _set_suppressed(s: Session, obj, on: bool) -> bool:
    """Suppress or unsuppress a feature or an occurrence; returns the state read back."""
    if s.kind() == "assembly":
        if on and not safe(lambda: obj.Suppressed, False):
            obj.Suppress()
        elif not on and safe(lambda: obj.Suppressed, False):
            obj.Unsuppress()
    else:
        obj.Suppressed = bool(on)
    return bool(safe(lambda: obj.Suppressed, on))


@tool("parameters")
def add_model_state(s: Session, name: str, activate: bool = True, suppress: list[str] | None = None,
                    unsuppress: list[str] | None = None) -> dict:
    """Add a model state. Inventor copies the active state into it, suppression included, so say explicitly what the
    new state suppresses: list every feature (part) or component (assembly) that must be off, and anything copied in
    suppressed that must be on. While a state is active, suppressing features or changing parameters only affects it.

    activate: leave the new state active (otherwise the previously active state is restored)
    suppress: feature or occurrence names to suppress in the new state
    unsuppress: names to make sure are not suppressed in the new state
    """
    st = _states(s)
    prev = safe(lambda: st.ActiveModelState.Name, "")
    targets = [(n, True) for n in suppress or []] + [(n, False) for n in unsuppress or []]
    objs = [(n, _suppressible(s, n), on) for n, on in targets]          # resolve names before changing anything
    m = st.Add(name)
    m.Activate()
    applied = {}
    for n, obj, on in objs:
        obj = _suppressible(s, n)                                       # the object as seen in the new state
        applied[n] = _set_suppressed(s, obj, on)
    s.doc().Update()
    wrong = [n for n, on in targets if applied.get(n) != on]
    if not activate and prev:
        back = next((x for x in items(st) if x.Name == prev), None)
        if back is not None:
            back.Activate()
            s.doc().Update()
    out = {"states": _state_rows(st), "copied_from": prev, "suppressed_in_new_state": sorted(
        n for n, v in applied.items() if v)}
    if wrong:
        out["warning"] = f"Inventor did not take the suppression of {wrong}; check them in that state"
    return out


@tool("parameters")
def activate_model_state(s: Session, name: str) -> dict:
    """Switch the active part or assembly to a model state."""
    st = _states(s)
    match = next((m for m in items(st) if m.Name == name), None)
    if match is None:
        raise InventorError(f"no model state {name!r}; there are {[m.Name for m in items(st)]}")
    match.Activate()
    s.doc().Update()
    return {"states": _state_rows(st)}


@tool("assembly")
def set_component_model_state(s: Session, occurrence: str, state: str) -> dict:
    """Show a component in the assembly in one of its model states.

    occurrence: occurrence name
    state: model state name in that component
    """
    o = _occ(s, occurrence)
    o.ActiveModelState = state
    s.doc().Update()
    return {"occurrence": o.Name, "state": o.ActiveModelState}


# --------------------------------------------------------------------------- named faces
def _face_index(s: Session, face) -> int | None:
    key = face.TransientKey
    return next((i for i, f in enumerate(s.faces(), 1) if f.TransientKey == key), None)


@tool("inspect")
def name_face(s: Session, face: int, name: str) -> dict:
    """Give a face a name that stays with it when indices change (the same names as Inventor's Assign Name).

    face: face index
    name: the name to give it
    """
    f = s.face(face)
    sets = f.AttributeSets
    aset = sets.Item(NAME_SET) if sets.NameIsUsed(NAME_SET) else sets.Add(NAME_SET)
    if aset.NameIsUsed(NAME_ATTR):
        aset.Item(NAME_ATTR).Value = name
    else:
        aset.Add(NAME_ATTR, K.kStringType, name)
    return {"face": face, "name": name}


@tool("inspect", mutates=False)
def find_named_face(s: Session, name: str) -> dict:
    """Current face index (for list_faces and the analysis tools) of a named face."""
    found = list(items(s.doc().AttributeManager.FindObjects(NAME_SET, NAME_ATTR, name)))
    idx = [i for i in (_face_index(s, f) for f in found) if i]
    if not idx:
        raise InventorError(f"no face named {name!r}")
    return {"name": name, "face": idx[0], "matches": idx}


@tool("inspect", mutates=False)
def list_named_faces(s: Session) -> dict:
    """Every named face of the part and its current index."""
    out = []
    for f in items(s.doc().AttributeManager.FindObjects(NAME_SET, NAME_ATTR)):
        out.append({"name": f.AttributeSets.Item(NAME_SET).Item(NAME_ATTR).Value, "face": _face_index(s, f)})
    return {"faces": out}


# --------------------------------------------------------------------------- split faces
def _plane_of(face) -> tuple | None:
    if face.SurfaceType != K.kPlaneSurface:
        return None
    p, n = face.Geometry.RootPoint, face.Geometry.Normal
    return (p.X, p.Y, p.Z), (n.X, n.Y, n.Z)


def _coplanar(a: tuple, b: tuple) -> bool:
    (p, n), (q, m) = a, b
    if abs(abs(sum(n[i] * m[i] for i in range(3))) - 1) > 1e-6:
        return False
    return abs(sum((q[i] - p[i]) * n[i] for i in range(3))) < 1e-5


def _split_targets(s: Session, planes: list) -> list:
    """The current faces lying in the planes of the faces asked for (earlier splits replace faces with pieces)."""
    return [f for f in s.faces() if (lambda q: q is not None and any(_coplanar(q, p) for p in planes))(_plane_of(f))]


def _split_with(s: Session, sf, tool_obj, faces, planes):
    if faces:
        return sf.SplitFaces(tool_obj, False, s.face_collection(_split_targets(s, planes)))
    return sf.SplitFaces(tool_obj, True, s.body())


def _chains(cd, curves) -> list:
    """One representative curve per connected chain of sketch curves."""
    seen, reps = set(), []
    for c in curves:
        key = safe(lambda c=c: c.TransientKey)
        if key in seen:
            continue
        path = safe(lambda c=c: cd.Features.CreatePath(c))
        members = [safe(lambda e=e: e.SketchEntity.TransientKey) for e in items(path)] if path is not None else []
        seen.update(k for k in members if k is not None)
        seen.add(key)
        reps.append(c)
    return reps


def _extent_cm(cd) -> float:
    rb = safe(lambda: cd.RangeBox)
    if rb is None:
        return 100.0
    return 2.0 * max(1.0, ((rb.MaxPoint.X - rb.MinPoint.X) ** 2 + (rb.MaxPoint.Y - rb.MinPoint.Y) ** 2 +
                           (rb.MaxPoint.Z - rb.MinPoint.Z) ** 2) ** 0.5)


@tool("features")
def split_faces(s: Session, sketch: int = 0, faces: list[int] | None = None, tool: str = "auto") -> dict:
    """Split faces along sketch lines, without changing the solid: for example to load only a patch of a face in an analysis.
    The sketch doesn't have to be on the face. Inventor refuses a sketch itself as the splitting tool, so the lines are
    turned into one of: paths (one split per line), a hidden work surface extruded from the sketch through the part
    (one split for a whole outline, curves included), or work planes through each straight line square to the sketch
    (each plane splits right across the face). A plate placed turned over shows its other face: split both.

    sketch: sketch index (0 = the current one)
    faces: face indices to split (default: every face the lines cross)
    tool: auto (paths, then a surface, then planes), lines, surface or plane
    """
    cd = s.cdef()
    sk = s.sketch(sketch)
    lines = [c for c in items(sk.SketchEntities) if safe(lambda c=c: c.Type) in
             (K.kSketchLineObject, K.kSketchArcObject, K.kSketchCircleObject) and not safe(lambda c=c: c.Construction, False)]
    if not lines:
        raise InventorError("the sketch has no lines to split with")
    mode = (tool or "auto").strip().lower()
    if mode not in ("auto", "lines", "surface", "plane"):
        raise InventorError("tool must be auto, lines, surface or plane")
    before = s.snapshot()
    sf = cd.Features.SplitFeatures
    planes = [_plane_of(s.face(i)) for i in faces or []]
    if any(p is None for p in planes):
        raise InventorError("split_faces with listed faces needs flat faces; leave faces out for curved ones")
    made, tried = [], []

    def by_lines():
        for i, ln in enumerate(lines):           # one line at a time: Inventor refuses a whole sketch
            made.append(_split_with(s, sf, cd.Features.CreatePath(ln), faces, planes).Name)
            if i == 0:                           # the first split consumes the sketch; only then can it be shared
                safe(lambda: setattr(sk, "Shared", True))

    def by_surface():
        safe(lambda: setattr(sk, "Shared", True))
        ef = cd.Features.ExtrudeFeatures
        for rep in _chains(cd, lines):
            prof = sk.Profiles.AddForSurface(rep)
            ed = ef.CreateExtrudeDefinition(prof, K.kSurfaceOperation)
            ed.SetDistanceExtent(_extent_cm(cd), K.kSymmetricExtentDirection)
            feat = ef.Add(ed)
            ws = cd.WorkSurfaces.Item(cd.WorkSurfaces.Count)
            safe(lambda: setattr(ws, "Visible", False))
            made.append(_split_with(s, sf, ws, faces, planes).Name + f" (surface {feat.Name})")

    def by_plane():
        straight = [c for c in lines if safe(lambda c=c: c.Type) == K.kSketchLineObject]
        if not straight:
            raise InventorError("plane splitting needs straight lines")
        import math as _m
        for ln in straight:
            wp = cd.WorkPlanes.AddByLinePlaneAndAngle(ln, sk.PlanarEntity, _m.pi / 2)
            safe(lambda wp=wp: setattr(wp, "Visible", False))
            made.append(_split_with(s, sf, wp, faces, planes).Name + f" (plane {wp.Name})")

    order = {"auto": [("lines", by_lines), ("surface", by_surface), ("plane", by_plane)],
             "lines": [("lines", by_lines)],
             "surface": [("surface", by_surface)], "plane": [("plane", by_plane)]}[mode]
    used, errors = "", []
    for label, fn in order:
        n0 = len(made)
        try:
            fn()
            used = label
            break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{label}: {e}")
            tried.append(label)
            if len(made) > n0:              # part of it went through: don't stack a second method on top
                used = label + " (partly)"
                break
    if not made:
        raise InventorError("Inventor refused every way of splitting with this sketch (" + "; ".join(errors)[:600] +
                            "). Check the lines cross the faces, or give faces explicitly")
    out = s.delta("split_faces", before)
    out.pop("warning", None)
    out.update(features=made, tool=used)
    if tried:
        out["fell_back_from"] = tried
    return out
