"""Inspection (faces, edges, mass, measurements), parameters, iProperties and materials."""
from __future__ import annotations

import math

from ..com import CM2_TO_MM2, CM3_TO_MM3, InventorError, K, items, r, safe, typed_call
from ..registry import tool
from ..session import Session

SURF = {"kPlaneSurface": "plane", "kCylinderSurface": "cylinder", "kConeSurface": "cone",
        "kSphereSurface": "sphere", "kTorusSurface": "torus", "kBSplineSurface": "bspline",
        "kEllipticalCylinderSurface": "elliptical_cylinder", "kEllipticalConeSurface": "elliptical_cone"}
CURVE = {"kLineSegmentCurve": "line", "kCircleCurve": "circle", "kCircularArcCurve": "arc",
         "kEllipseFullCurve": "ellipse", "kEllipticalArcCurve": "elliptical_arc", "kBSplineCurve": "bspline",
         "kLineCurve": "line", "kPolylineCurve": "polyline"}


def _name(value: int, table: dict) -> str:
    for k, v in table.items():
        if K.get(k) == value:
            return v
    return str(value)


def _mm(p) -> list[float]:
    return [r(p.X * 10), r(p.Y * 10), r(p.Z * 10)]


# --------------------------------------------------------------------------- geometry helpers
def face_normal(face) -> list[float]:
    """Outward unit normal at the face's PointOnFace (oriented by the face, not the raw surface)."""
    p = face.PointOnFace
    try:
        out = typed_call(face.Evaluator, "GetNormalAtPoint", [p.X, p.Y, p.Z], [0.0, 0.0, 0.0])
        n = list(out[-1])
        if len(n) == 3 and any(abs(v) > 1e-12 for v in n):
            return [r(v, 6) for v in n]
    except Exception:
        pass
    g = face.Geometry
    n = g.Normal
    sgn = -1.0 if safe(lambda: face.IsParamReversed, False) else 1.0
    return [r(sgn * n.X, 6), r(sgn * n.Y, 6), r(sgn * n.Z, 6)]


def face_centroid(face) -> list[float]:
    """Approximate centroid (centre of the face's range box projected through PointOnFace)."""
    rb = face.Evaluator.RangeBox
    c = [(rb.MinPoint.X + rb.MaxPoint.X) / 2, (rb.MinPoint.Y + rb.MaxPoint.Y) / 2, (rb.MinPoint.Z + rb.MaxPoint.Z) / 2]
    if face.SurfaceType == K.kPlaneSurface:
        # snap to the plane
        p = face.PointOnFace
        n = face_normal(face)
        d = sum((c[i] - [p.X, p.Y, p.Z][i]) * n[i] for i in range(3))
        c = [c[i] - d * n[i] for i in range(3)]
    return [r(v * 10) for v in c]


def face_row(s: Session, i: int, f) -> dict:
    kind = _name(f.SurfaceType, SURF)
    row = {"index": i, "type": kind, "area_mm2": r(f.Evaluator.Area * CM2_TO_MM2, 2),
           "centroid_mm": face_centroid(f)}
    if kind == "plane":
        row["normal"] = face_normal(f)
    elif kind in ("cylinder", "cone"):
        g = f.Geometry
        if kind == "cylinder":
            row["diameter_mm"] = r(g.Radius * 20)
        row["axis"] = [r(g.AxisVector.X, 4), r(g.AxisVector.Y, 4), r(g.AxisVector.Z, 4)]
        row["axis_point_mm"] = _mm(g.BasePoint)
        n = face_normal(f)
        # concave (a hole) when the outward normal points toward the axis
        p = f.PointOnFace
        to_axis = [g.BasePoint.X - p.X, g.BasePoint.Y - p.Y, g.BasePoint.Z - p.Z]
        a = [g.AxisVector.X, g.AxisVector.Y, g.AxisVector.Z]
        dot_a = sum(to_axis[k] * a[k] for k in range(3))
        radial = [to_axis[k] - dot_a * a[k] for k in range(3)]
        row["hole"] = sum(radial[k] * n[k] for k in range(3)) > 0
    return row


def edge_row(i: int, e) -> dict:
    kind = _name(e.GeometryType, CURVE)
    row = {"index": i, "type": kind}
    try:
        pmin, pmax = typed_call(e.Evaluator, "GetParamExtents", 0.0, 0.0)[-2:]
        length = typed_call(e.Evaluator, "GetLengthAtParam", pmin, pmax, 0.0)[-1]
        mid = typed_call(e.Evaluator, "GetPointAtParam", [(pmin + pmax) / 2], [0.0, 0.0, 0.0])[-1]
        row["length_mm"] = r(length * 10)
        row["midpoint_mm"] = [r(v * 10) for v in mid]
    except Exception:
        sv, ev = safe(lambda: e.StartVertex.Point), safe(lambda: e.StopVertex.Point)
        if sv is not None and ev is not None:
            row["midpoint_mm"] = [r((sv.X + ev.X) * 5), r((sv.Y + ev.Y) * 5), r((sv.Z + ev.Z) * 5)]
            row["length_mm"] = r(math.dist([sv.X, sv.Y, sv.Z], [ev.X, ev.Y, ev.Z]) * 10)
    if kind in ("circle", "arc"):
        g = e.Geometry
        row["diameter_mm"] = r(g.Radius * 20)
        row["center_mm"] = _mm(g.Center)
    return row


def point_inside(body, xyz_cm) -> bool | None:
    """SurfaceBody.IsPointInside is an indexed property taking a SAFEARRAY — invoke it with explicit types."""
    import pythoncom
    try:
        o = body._oleobj_
        memid = o.GetIDsOfNames("IsPointInside")
        res = o.InvokeTypes(memid, 0, pythoncom.DISPATCH_PROPERTYGET, (pythoncom.VT_I4, 0),
                            ((pythoncom.VT_ARRAY | pythoncom.VT_R8 | pythoncom.VT_BYREF, 1), (pythoncom.VT_BOOL, 17)),
                            [float(v) for v in xyz_cm], False)
        res = res[0] if isinstance(res, tuple) else res
    except Exception:
        return None
    if res == K.kInsideContainment:
        return True
    if res == K.kOutsideContainment:
        return False
    return None


def find_planar_face(s: Session, axis: str = "Z", at: list[float] | None = None):
    """Outermost planar face whose outward normal is +axis (e.g. the top face for Z). With ``at``
    (the two other coordinates, mm, in X-Y-Z order), only faces that actually lie over that point count —
    so a hole on a stepped part starts on the step it is drilled through, not the highest face."""
    ai = "XYZ".index(axis.strip().upper()[-1])
    sign = -1 if axis.strip().startswith("-") else 1
    rest = [i for i in range(3) if i != ai]
    best, best_val = None, -1e18
    for f in s.faces():
        if f.SurfaceType != K.kPlaneSurface:
            continue
        n = face_normal(f)
        if n[ai] * sign < 0.999:
            continue
        p = f.PointOnFace
        v = [p.X, p.Y, p.Z][ai] * sign
        if at:
            # just under the face there must be material, just above it air
            q = [0.0, 0.0, 0.0]
            q[rest[0]], q[rest[1]] = at[0] * 0.1, at[1] * 0.1
            lvl = [p.X, p.Y, p.Z][ai]
            below, above = list(q), list(q)
            below[ai], above[ai] = lvl - sign * 0.002, lvl + sign * 0.002
            body = s.body()
            if point_inside(body, below) is not True or point_inside(body, above) is not False:
                continue
        if v > best_val:
            best, best_val = f, v
    return best


# --------------------------------------------------------------------------- tools
@tool("inspect", mutates=False)
def inspect(s: Session) -> dict:
    """Part summary: volume, surface area, bounding box, face/edge counts, features, mass, material."""
    d = s.part()
    cd = d.ComponentDefinition
    out = {"name": d.DisplayName, "sheet_metal": s.is_sheet_metal(),
           "bodies": cd.SurfaceBodies.Count, "features": [f.Name for f in items(cd.Features)],
           "sketches": cd.Sketches.Count, "material": safe(lambda: d.ActiveMaterial.DisplayName, "")}
    if cd.SurfaceBodies.Count:
        mp = cd.MassProperties
        rb = cd.RangeBox
        mn, mx = _mm(rb.MinPoint), _mm(rb.MaxPoint)
        out.update({"volume_mm3": r(mp.Volume * CM3_TO_MM3, 2), "area_mm2": r(mp.Area * CM2_TO_MM2, 2),
                    "mass_kg": r(mp.Mass, 5), "bbox_min_mm": mn, "bbox_max_mm": mx,
                    "size_mm": [r(mx[i] - mn[i]) for i in range(3)],
                    "faces": sum(b.Faces.Count for b in items(cd.SurfaceBodies)),
                    "edges": sum(b.Edges.Count for b in items(cd.SurfaceBodies))})
    return out


@tool("inspect", mutates=False)
def list_faces(s: Session, min_area: float = 0.0, type: str = "", max_count: int = 80) -> dict:
    """Face table: index, type (plane/cylinder/...), area, centroid, outward normal or diameter/axis,
    and hole=true for concave cylinders. Indices are what fillet/shell/hole/FEA tools take.

    type: filter, e.g. plane or cylinder
    """
    rows = []
    for i, f in enumerate(s.faces(), 1):
        row = face_row(s, i, f)
        if row["area_mm2"] < min_area or (type and row["type"] != type):
            continue
        rows.append(row)
        if len(rows) >= max_count:
            break
    return {"faces": rows, "total_faces": s.body().Faces.Count}


@tool("inspect", mutates=False)
def list_edges(s: Session, min_length: float = 0.0, type: str = "", max_count: int = 100) -> dict:
    """Edge table: index, type (line/circle/arc...), length, midpoint, diameter/centre for circles."""
    rows = []
    for i, e in enumerate(s.edges(), 1):
        row = edge_row(i, e)
        if row.get("length_mm", 0) < min_length or (type and row["type"] != type):
            continue
        rows.append(row)
        if len(rows) >= max_count:
            break
    return {"edges": rows, "total_edges": s.body().Edges.Count}


def _near(a, b) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3) if b[i] is not None))


@tool("inspect", mutates=False)
def find_edge(s: Session, x: float | None = None, y: float | None = None, z: float | None = None,
              type: str = "", tolerance: float = 0.5) -> dict:
    """Edge(s) whose midpoint is nearest (x, y, z) mm — omitted coordinates are ignored.
    Returns the best match plus any others within tolerance."""
    target = [x, y, z]
    scored = []
    for i, e in enumerate(s.edges(), 1):
        row = edge_row(i, e)
        if type and row["type"] != type or "midpoint_mm" not in row:
            continue
        scored.append((_near(row["midpoint_mm"], target), row))
    if not scored:
        raise InventorError("no edges")
    scored.sort(key=lambda t: t[0])
    best = scored[0]
    return {"edge": best[1]["index"], "distance_mm": r(best[0]), "match": best[1],
            "also_within_tolerance": [rw["index"] for d, rw in scored[1:] if d - best[0] <= tolerance]}


@tool("inspect", mutates=False)
def find_face(s: Session, x: float | None = None, y: float | None = None, z: float | None = None,
              normal: list[float] | None = None, type: str = "", min_area: float = 0.0) -> dict:
    """Face nearest a point (centroid distance) and/or best aligned with an outward normal.

    normal: e.g. [0, 0, 1] — with no coordinates, returns the outermost face facing that way
    """
    rows = [face_row(s, i, f) for i, f in enumerate(s.faces(), 1)]
    rows = [rw for rw in rows if rw["area_mm2"] >= min_area and (not type or rw["type"] == type)]
    if normal:
        nn = math.sqrt(sum(v * v for v in normal)) or 1
        nv = [v / nn for v in normal]
        rows = [rw for rw in rows if rw.get("normal") and sum(rw["normal"][i] * nv[i] for i in range(3)) > 0.999]
        if all(c is None for c in (x, y, z)):
            rows.sort(key=lambda rw: -sum(rw["centroid_mm"][i] * nv[i] for i in range(3)))
            if not rows:
                raise InventorError("no planar face with that normal")
            return {"face": rows[0]["index"], "match": rows[0], "others": [rw["index"] for rw in rows[1:6]]}
    if not rows:
        raise InventorError("no faces match")
    rows.sort(key=lambda rw: _near(rw["centroid_mm"], [x, y, z]))
    return {"face": rows[0]["index"], "distance_mm": r(_near(rows[0]["centroid_mm"], [x, y, z])),
            "match": rows[0], "next": [rw["index"] for rw in rows[1:4]]}


@tool("inspect", mutates=False)
def mass_properties(s: Session) -> dict:
    """Mass, volume, area, centre of gravity and inertia (about the CoG-aligned global axes and
    principal) of the active part or assembly, using its assigned material(s)."""
    d = s.doc()
    if s.kind(d) not in ("part", "assembly"):
        raise InventorError("open a part or assembly")
    mp = d.ComponentDefinition.MassProperties
    cog = mp.CenterOfMass
    out = {"mass_kg": r(mp.Mass, 6), "volume_mm3": r(mp.Volume * CM3_TO_MM3, 2),
           "area_mm2": r(mp.Area * CM2_TO_MM2, 2), "cog_mm": _mm(cog),
           "material": safe(lambda: d.ActiveMaterial.DisplayName, "(assembly)")}
    if out["volume_mm3"]:
        out["density_kg_m3"] = r(mp.Mass / (mp.Volume * 1e-6), 1)
    try:
        ixx, iyy, izz, ixy, iyz, ixz = typed_call(mp, "XYZMomentsOfInertia")[-6:]
        k = 100.0   # kg·cm² -> kg·mm²
        out["inertia_kg_mm2"] = {"Ixx": r(ixx * k, 3), "Iyy": r(iyy * k, 3), "Izz": r(izz * k, 3),
                                 "Ixy": r(ixy * k, 3), "Iyz": r(iyz * k, 3), "Ixz": r(ixz * k, 3),
                                 "about": "global axes at the origin (Inventor convention)"}
        i1, i2, i3 = typed_call(mp, "PrincipalMomentsOfInertia")[-3:]
        out["principal_kg_mm2"] = [r(i1 * k, 3), r(i2 * k, 3), r(i3 * k, 3)]
    except Exception as e:
        out["inertia_note"] = f"inertia unavailable: {e}"
    return out


@tool("inspect", mutates=False)
def measure(s: Session, face_one: int = 0, face_two: int = 0, edge_one: int = 0, edge_two: int = 0) -> dict:
    """Minimum distance (and angle between planar faces) for two faces, two edges, or a face and an edge."""
    ents = [s.face(face_one) if face_one else None, s.face(face_two) if face_two else None,
            s.edge(edge_one) if edge_one else None, s.edge(edge_two) if edge_two else None]
    ents = [e for e in ents if e is not None]
    if len(ents) != 2:
        raise InventorError("give exactly two entities")
    mt = s.app.MeasureTools
    d = mt.GetMinimumDistance(ents[0], ents[1])
    d = d[0] if isinstance(d, tuple) else d
    out = {"min_distance_mm": r(d * 10, 4)}
    if face_one and face_two:
        n1, n2 = face_normal(ents[0]), face_normal(ents[1])
        if ents[0].SurfaceType == K.kPlaneSurface and ents[1].SurfaceType == K.kPlaneSurface:
            c = max(-1.0, min(1.0, sum(n1[i] * n2[i] for i in range(3))))
            out["angle_between_normals_deg"] = r(math.degrees(math.acos(c)), 3)
    return out


# --------------------------------------------------------------------------- parameters
def _param_row(p) -> dict:
    units = safe(lambda: p.Units, "")
    val = safe(lambda: p.Value, None)
    shown = val
    if isinstance(val, (int, float)):
        if units in ("mm", "cm", "m", "in"):
            shown = r(val * 10, 6)     # database cm -> mm
        elif units in ("deg", "rad"):
            shown = r(math.degrees(val), 6)
    return {"name": p.Name, "expression": safe(lambda: p.Expression, ""), "value": shown,
            "units": units, "comment": safe(lambda: p.Comment, "")}


def _params(s: Session):
    d = s.doc()
    return d.ComponentDefinition.Parameters


@tool("parameters", mutates=False)
def get_parameters(s: Session, user_only: bool = False) -> dict:
    """Model (dimension) and user parameters with expressions and values (lengths in mm, angles in deg)."""
    ps = _params(s)
    out = {"user": [_param_row(p) for p in items(ps.UserParameters)]}
    if not user_only:
        out["model"] = [_param_row(p) for p in items(ps.ModelParameters)]
    return out


@tool("parameters")
def set_parameter(s: Session, name: str, expression: str) -> dict:
    """Set a parameter's expression (e.g. "40 mm", "d0 * 2", "Thickness + 2") and rebuild."""
    ps = _params(s)
    try:
        p = ps.Item(name)
    except Exception:
        raise InventorError(f"no parameter {name!r} (see get_parameters)") from None
    before = s.snapshot() if s.kind() == "part" else None
    if str(expression).replace(".", "", 1).lstrip("-").isdigit() and safe(lambda: p.Units, "") in ("mm", "cm"):
        expression = f"{expression} mm"
    p.Expression = str(expression)
    s.doc().Update()
    out = {"parameter": _param_row(p)}
    if before is not None:
        out.update(s.delta(f"Param {name}", before))
    return out


@tool("parameters")
def add_parameter(s: Session, name: str, expression: str, units: str = "mm", comment: str = "") -> dict:
    """Add a user parameter (units: mm, deg, ul for unitless, kg, N, MPa...)."""
    ps = _params(s)
    try:
        p = ps.UserParameters.AddByExpression(name, str(expression), units)
    except Exception as e:
        if type(e).__name__ != "com_error":
            raise
        raise InventorError(f"Inventor refused parameter {name!r} = {expression!r}. Names that are also units (H, T, N, m, "
                            "dK...) or already in use are rejected: try a longer name such as Height") from e
    if comment:
        safe(lambda: setattr(p, "Comment", comment))
    return {"parameter": _param_row(p)}


@tool("parameters", mutates=False)
def list_dimensions(s: Session, sketch_index: int = 0) -> dict:
    """Driving sketch dimensions (all sketches, or one) with the parameter that controls each."""
    cd = s.cdef()
    sketches = [s.sketch(sketch_index)] if sketch_index else list(items(cd.Sketches))
    out = []
    for sk in sketches:
        for dc in items(sk.DimensionConstraints):
            p = safe(lambda dc=dc: dc.Parameter)
            if p is None:
                continue
            row = _param_row(p)
            row["sketch"] = sk.Name
            row["driven"] = bool(safe(lambda dc=dc: dc.Driven, False))
            out.append(row)
    return {"dimensions": out}


@tool("parameters")
def edit_dimension(s: Session, parameter: str, value: str) -> dict:
    """Change a dimension by its parameter name (d0, d1 ... from list_dimensions) and rebuild."""
    return set_parameter(s, parameter, value)


# --------------------------------------------------------------------------- iProperties
IPROP_SETS = {"Summary": "Inventor Summary Information", "Document": "Inventor Document Summary Information",
              "Project": "Design Tracking Properties", "Custom": "Inventor User Defined Properties"}
IPROP_ALIASES = {"title": ("Summary", "Title"), "subject": ("Summary", "Subject"), "author": ("Summary", "Author"),
                 "keywords": ("Summary", "Keywords"), "comments": ("Summary", "Comments"),
                 "part_number": ("Project", "Part Number"), "description": ("Project", "Description"),
                 "designer": ("Project", "Designer"), "engineer": ("Project", "Engineer"),
                 "project": ("Project", "Project"), "revision": ("Summary", "Revision Number"),
                 "stock_number": ("Project", "Stock Number"), "vendor": ("Project", "Vendor"),
                 "checked_by": ("Project", "Checked By"), "company": ("Document", "Company"),
                 "material": ("Project", "Material"), "cost_center": ("Project", "Cost Center"),
                 "date_checked": ("Project", "Date Checked"), "creation_date": ("Project", "Creation Time")}


@tool("properties", mutates=False)
def get_iproperties(s: Session) -> dict:
    """Common iProperties (part number, description, designer, revision...) and custom properties."""
    d = s.doc()
    out = {}
    for alias, (setname, prop) in IPROP_ALIASES.items():
        v = safe(lambda: d.PropertySets.Item(IPROP_SETS[setname]).Item(prop).Value)
        if v not in (None, ""):
            out[alias] = str(v)
    custom = {p.Name: str(p.Value) for p in items(d.PropertySets.Item(IPROP_SETS["Custom"]))}
    return {"iproperties": out, "custom": custom}


@tool("properties")
def set_iproperties(s: Session, properties: dict) -> dict:
    """Set iProperties: keys like part_number, description, title, author, designer, revision,
    project, company, checked_by, vendor, stock_number — anything else becomes a custom property.
    Drawing title blocks read these."""
    d = s.doc()
    done, custom = {}, {}
    for k, v in properties.items():
        key = k.lower().replace(" ", "_")
        if key in IPROP_ALIASES:
            setname, prop = IPROP_ALIASES[key]
            d.PropertySets.Item(IPROP_SETS[setname]).Item(prop).Value = str(v)
            done[key] = str(v)
        else:
            cs = d.PropertySets.Item(IPROP_SETS["Custom"])
            try:
                cs.Item(k).Value = str(v)
            except Exception:
                cs.Add(str(v), k)
            custom[k] = str(v)
    return {"set": done, "custom": custom}


# --------------------------------------------------------------------------- materials
def _library_materials(s: Session):
    lib = s.app.ActiveMaterialLibrary
    return lib, list(items(lib.MaterialAssets))


@tool("materials", mutates=False)
def list_materials(s: Session, filter: str = "") -> dict:
    """Material names available in the active material library (filter is a case-insensitive substring)."""
    lib, mats = _library_materials(s)
    names = [m.DisplayName for m in mats if filter.lower() in m.DisplayName.lower()]
    return {"library": lib.DisplayName, "materials": names[:200], "count": len(names)}


def _asset_value(asset, *names):
    for n in names:
        try:
            v = asset.Item(n).Value
            if v is not None:
                return v
        except Exception:
            continue
    return None


def material_data(s: Session, d=None) -> dict:
    """E (MPa), Poisson's ratio, density (kg/m³), yield and ultimate (MPa) of the part's material."""
    d = d or s.part()
    m = d.ActiveMaterial
    phys = safe(lambda: m.PhysicalPropertiesAsset)
    out = {"name": safe(lambda: m.DisplayName, "")}
    if phys is not None:
        e = _asset_value(phys, "structural_Young_modulus", "structural_YoungModulus")
        nu = _asset_value(phys, "structural_Poisson_ratio")
        rho = _asset_value(phys, "structural_Density")
        sy = _asset_value(phys, "structural_Minimum_yield_stress")
        su = _asset_value(phys, "structural_Minimum_tensile_strength")
        first = lambda v: v[0] if isinstance(v, (tuple, list)) else v
        conv = lambda v: None if v is None else float(first(v))
        e, nu, rho, sy, su = map(conv, (e, nu, rho, sy, su))
        # asset units: stresses/moduli in kPa (220 GPa steel reads 2.2e8), density in kg/m³ (g/cm³ also handled)
        out.update({"E_MPa": r(e / 1000, 1) if e else None, "poisson": r(nu, 4) if nu else None,
                    "density_kg_m3": (r(rho, 1) if rho > 100 else r(rho * 1000, 1)) if rho else None,
                    "yield_MPa": r(sy / 1000, 2) if sy else None, "ultimate_MPa": r(su / 1000, 2) if su else None})
        if out.get("E_MPa") and not 1000 <= out["E_MPa"] <= 1.5e6:
            out["units_warning"] = f"E = {out['E_MPa']} MPa looks implausible — pass E/yield_strength explicitly"
    return out


@tool("materials")
def set_material(s: Session, name: str) -> dict:
    """Assign a material from the library by name (exact or unique partial match, e.g. "Steel, Mild",
    "Aluminum 6061"). Returns its mechanical properties."""
    d = s.part()
    lib, mats = _library_materials(s)
    exact = [m for m in mats if m.DisplayName.lower() == name.lower()]
    partial = [m for m in mats if name.lower() in m.DisplayName.lower()]
    pick = exact or partial
    if not pick:
        raise InventorError(f"no material matching {name!r} (see list_materials)")
    if not exact and len(partial) > 1:
        names = [m.DisplayName for m in partial[:15]]
        if len(partial) > 1 and not any(n.lower() == name.lower() for n in names):
            pass
    asset = pick[0]
    try:
        local = d.MaterialAssets.Item(asset.DisplayName)
    except Exception:
        local = asset.CopyTo(d)
    d.ActiveMaterial = local
    d.Update()
    out = {"material": asset.DisplayName, **material_data(s, d)}
    if not exact and len(partial) > 1:
        out["other_matches"] = [m.DisplayName for m in partial[1:10]]
    out["mass_kg"] = r(d.ComponentDefinition.MassProperties.Mass, 5)
    return out


@tool("inspect", mutates=False)
def face_at_point(s: Session, x: float, y: float, z: float, tolerance: float = 0.5) -> dict:
    """The face a point (mm) lies on — for references picked in a viewer. Unlike find_face (nearest face centre) this
    checks the surface itself, and says when the point is on an edge shared by two faces.

    tolerance: how far off the surface the point may be, mm
    """
    from ..com import typed_call as _tc
    cd = s.cdef()
    out = _tc(cd, "FindUsingPoint", s.p3(x, y, z), [K.kFaceObject], tolerance * 0.1, False)
    found = list(items(out[-1] if isinstance(out, tuple) else out))
    keys = {f.TransientKey: i for i, f in enumerate(s.faces(), 1)}
    hits = [keys[f.TransientKey] for f in found if safe(lambda f=f: f.TransientKey) in keys]
    if not hits:
        raise InventorError(f"no face within {tolerance} mm of ({x}, {y}, {z})")
    return {"face": hits[0], "ambiguous": len(hits) > 1, "candidates": hits[:6],
            "match": face_row(s, hits[0], s.face(hits[0]))}


@tool("materials", mutates=False)
def material_properties(s: Session) -> dict:
    """Young's modulus (MPa), Poisson's ratio, density (kg/m³), yield and ultimate strength (MPa) of the part's material."""
    return material_data(s)


@tool("materials")
def create_material(s: Session, name: str, base: str = "Steel, Mild", yield_MPa: float | None = None,
                    ultimate_MPa: float | None = None, E_MPa: float | None = None, poisson: float | None = None,
                    density_kg_m3: float | None = None) -> dict:
    """Make a named material in the active part from a library one, with your own strengths, and assign it — for
    grades the library lacks, e.g. name="AS/NZS 1163 C250L0", yield_MPa=250, ultimate_MPa=320. Stress Analysis uses
    these values, so the safety factor is against the right yield."""
    d = s.part()
    lib, mats = _library_materials(s)
    src = [m for m in mats if m.DisplayName.lower() == base.lower()] or [m for m in mats if base.lower() in m.DisplayName.lower()]
    if not src:
        raise InventorError(f"no library material matching {base!r}")
    try:
        mat = d.MaterialAssets.Item(name)
    except Exception:
        local = safe(lambda: d.MaterialAssets.Item(src[0].DisplayName)) or src[0].CopyTo(d)
        mat = local.Duplicate(name)
    phys = mat.PhysicalPropertiesAsset
    isolated = safe(lambda: phys.DisplayName, "") == name
    if not isolated:                         # give the new material its own physical asset when Inventor allows it
        try:
            dup = phys.Duplicate(name)
            mat.PhysicalPropertiesAsset = dup
            phys, isolated = mat.PhysicalPropertiesAsset, True
        except Exception:
            pass

    def put(keys, value):
        if value is None:
            return
        for k in keys:
            try:
                phys.Item(k).Value = value
                return
            except Exception:
                continue
        raise InventorError(f"could not set {keys[0]} on {name}")
    put(["structural_Minimum_yield_stress"], float(yield_MPa) * 1000 if yield_MPa else None)
    put(["structural_Minimum_tensile_strength"], float(ultimate_MPa) * 1000 if ultimate_MPa else None)
    put(["structural_Young_modulus", "structural_YoungModulus"], float(E_MPa) * 1000 if E_MPa else None)
    put(["structural_Poisson_ratio"], poisson)
    put(["structural_Density"], density_kg_m3)
    d.ActiveMaterial = mat
    d.Update()
    out = {"material": name, "based_on": src[0].DisplayName, "own_physical_asset": isolated, **material_data(s, d)}
    if not isolated:
        out["warning"] = ("Inventor would not give this material its own physical-properties asset, so the values were "
                          f"written to the asset it shares with {src[0].DisplayName} in this document only")
    if yield_MPa and abs((out.get("yield_MPa") or 0) - float(yield_MPa)) > 0.5:
        out["warning"] = f"yield reads back as {out.get('yield_MPa')} MPa, not {yield_MPa}"
    out["mass_kg"] = r(d.ComponentDefinition.MassProperties.Mass, 5)
    return out


@tool("materials")
def set_appearance(s: Session, name: str) -> dict:
    """Give the active part a library appearance (colour/finish only; mass and stress use the material), by exact or
    partial name, e.g. "Rubber - Black", "Powder Coat - Red", "Chrome - Polished"."""
    d = s.part()
    lib = s.app.ActiveAppearanceLibrary
    apps = list(items(lib.AppearanceAssets))
    pick = [a for a in apps if a.DisplayName.lower() == name.lower()] or [a for a in apps if name.lower() in a.DisplayName.lower()]
    if not pick:
        raise InventorError(f"no appearance matching {name!r}; e.g. {', '.join(a.DisplayName for a in apps[:12])}")
    a = pick[0]
    local = safe(lambda: d.AppearanceAssets.Item(a.DisplayName)) or a.CopyTo(d)
    d.ActiveAppearance = local
    d.Update()
    return {"appearance": a.DisplayName, "other_matches": [x.DisplayName for x in pick[1:6]]}
