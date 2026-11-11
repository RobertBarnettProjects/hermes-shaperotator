"""Assemblies: placing, constraining, grounding, moving, interference, BOM."""
from __future__ import annotations

import csv
import math
import os

from ..com import CM3_TO_MM3, MM, InventorError, K, items, r, safe, typed_call
from ..registry import tool
from ..session import Session
from .inspect import face_row


def _occ(s: Session, name: str):
    occs = s.assembly().ComponentDefinition.Occurrences
    if str(name).isdigit():
        i = int(name)
        if not 1 <= i <= occs.Count:
            raise InventorError(f"occurrence {i} out of range 1..{occs.Count}")
        return occs.Item(i)
    for o in items(occs):
        if o.Name.lower() == str(name).lower():
            return o
    for o in items(occs):
        if o.Name.lower().startswith(str(name).lower()):
            return o
    raise InventorError(f"no occurrence {name!r} (see inspect_assembly)")


def _proxy(occ, geometry):
    return typed_call(occ, "CreateGeometryProxy", geometry, None)[-1]


def _entity(s: Session, occ_name: str, entity: str):
    """Entity spec: "face:3", "edge:5", "XY"/"XZ"/"YZ" origin plane, "X"/"Y"/"Z" origin axis,
    "point" = origin point. occ_name "" = the assembly's own origin geometry."""
    spec = str(entity).strip()
    asm_cd = s.assembly().ComponentDefinition
    if not occ_name:
        cd = asm_cd
        if spec.upper() in ("XY", "XZ", "YZ"):
            return cd.WorkPlanes.Item({"YZ": 1, "XZ": 2, "XY": 3}[spec.upper()])
        if spec.upper() in ("X", "Y", "Z"):
            return cd.WorkAxes.Item({"X": 1, "Y": 2, "Z": 3}[spec.upper()])
        if spec.lower() == "point":
            return cd.WorkPoints.Item(1)
        raise InventorError("assembly-level entities are XY/XZ/YZ, X/Y/Z or point")
    occ = _occ(s, occ_name)
    kind, _, idx = spec.partition(":")
    if kind.lower() == "face":
        return occ.SurfaceBodies.Item(1).Faces.Item(int(idx))
    if kind.lower() == "edge":
        return occ.SurfaceBodies.Item(1).Edges.Item(int(idx))
    cd = occ.Definition
    if spec.upper() in ("XY", "XZ", "YZ"):
        return _proxy(occ, cd.WorkPlanes.Item({"YZ": 1, "XZ": 2, "XY": 3}[spec.upper()]))
    if spec.upper() in ("X", "Y", "Z"):
        return _proxy(occ, cd.WorkAxes.Item({"X": 1, "Y": 2, "Z": 3}[spec.upper()]))
    if spec.lower() == "point":
        return _proxy(occ, cd.WorkPoints.Item(1))
    raise InventorError(f"bad entity {entity!r}: use face:N, edge:N, XY/XZ/YZ, X/Y/Z or point")


def _occ_row(o) -> dict:
    t = o.Transformation
    return {"name": o.Name, "file": os.path.basename(safe(lambda: o.Definition.Document.FullFileName, "") or ""),
            "grounded": bool(o.Grounded), "suppressed": bool(safe(lambda: o.Suppressed, False)),
            "position_mm": [r(t.Cell(1, 4) * 10), r(t.Cell(2, 4) * 10), r(t.Cell(3, 4) * 10)],
            "mass_kg": safe(lambda: r(o.MassProperties.Mass, 5))}


@tool("assembly")
def place_component(s: Session, path: str, x: float = 0.0, y: float = 0.0, z: float = 0.0,
                    rotate_axis: str = "", rotate_angle: float = 0.0, grounded: bool = False) -> dict:
    """Place a part/assembly file (or an open document by name) into the active assembly at (x, y, z) mm,
    optionally rotated about X/Y/Z by rotate_angle degrees. The first component is usually grounded."""
    asm = s.assembly()
    full = path
    if not os.path.isabs(full) or not os.path.exists(full):
        for d in items(s.app.Documents):
            if safe(lambda d=d: d.DisplayName, "").lower().split(".")[0] == path.lower().split(".")[0]:
                full = d.FullFileName
                if not full:
                    raise InventorError(f"{path} is open but unsaved — save_document it first")
                break
    if not os.path.exists(full):
        raise InventorError(f"file not found: {path}")
    tg = s.tg
    m = tg.CreateMatrix()
    if rotate_axis and rotate_angle:
        ax = {"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[rotate_axis.upper()]
        m.SetToRotation(math.radians(rotate_angle), tg.CreateVector(*ax), tg.CreatePoint(0, 0, 0))
    m.SetTranslation(tg.CreateVector(x * MM, y * MM, z * MM))
    occ = asm.ComponentDefinition.Occurrences.Add(full, m)
    if grounded:
        occ.Grounded = True
    return {"placed": _occ_row(occ), "occurrences": asm.ComponentDefinition.Occurrences.Count}


@tool("assembly")
def add_constraint(s: Session, type: str, occurrence_one: str, entity_one: str, occurrence_two: str,
                   entity_two: str, offset: float = 0.0, angle: float = 0.0, opposed: bool = True,
                   offset_expression: str = "") -> dict:
    """Assembly constraint between two components (or a component and the assembly origin).

    type: mate | flush | angle | insert | tangent
    occurrence_one: occurrence name/index ("" = assembly origin geometry)
    entity_one: face:N | edge:N | XY | XZ | YZ | X | Y | Z | point   (face/edge indices as in the part's list_faces)
    offset: mm (mate/flush/tangent/insert distance)
    angle: degrees (angle constraints)
    opposed: insert — axes opposed (true for a bolt into a hole from the far side)
    offset_expression: drive the offset from parameters, e.g. "L / 2" (overrides offset). The offset's sign depends on the faces,
      so check the returned positions and use "-L / 2" if the part went the wrong way
    """
    cons = s.assembly().ComponentDefinition.Constraints
    e1 = _entity(s, occurrence_one, entity_one)
    e2 = _entity(s, occurrence_two, entity_two)
    t = type.lower()
    off = offset * MM
    if t == "mate":
        cyl = lambda e: safe(lambda: e.SurfaceType in (K.kCylinderSurface, K.kConeSurface), False)
        if cyl(e1) and cyl(e2):          # two round faces: line them up on their axes
            c = cons.AddMateConstraint(e1, e2, off, K.kInferredLine, K.kInferredLine)
        else:
            c = cons.AddMateConstraint(e1, e2, off)
    elif t == "flush":
        c = cons.AddFlushConstraint(e1, e2, off)
    elif t == "angle":
        c = cons.AddAngleConstraint(e1, e2, math.radians(angle))
    elif t == "insert":
        c = cons.AddInsertConstraint(e1, e2, bool(opposed), off)
    elif t == "tangent":
        c = cons.AddTangentConstraint(e1, e2, False, off)
    else:
        raise InventorError("type must be mate, flush, angle, insert or tangent")
    if offset_expression:
        if t == "angle":
            c.Angle.Expression = offset_expression
        else:
            c.Offset.Expression = offset_expression
    s.doc().Update()
    return {"constraint": c.Name, "health": K.name_of(safe(lambda: c.HealthStatus, 0), "k"),
            "positions": [_occ_row(o) for o in items(s.assembly().ComponentDefinition.Occurrences)][:20]}


@tool("assembly")
def ground_component(s: Session, occurrence: str, grounded: bool = True) -> dict:
    """Ground (fix in place) or unground a component."""
    o = _occ(s, occurrence)
    o.Grounded = bool(grounded)
    return {"occurrence": _occ_row(o)}


@tool("assembly")
def move_component(s: Session, occurrence: str, x: float, y: float, z: float) -> dict:
    """Move a (non-grounded, unconstrained) component's origin to (x, y, z) mm."""
    o = _occ(s, occurrence)
    m = o.Transformation
    m.SetTranslation(s.tg.CreateVector(x * MM, y * MM, z * MM))
    o.Transformation = m
    return {"occurrence": _occ_row(o)}


@tool("assembly", mutates=False)
def inspect_assembly(s: Session, faces_of: str = "") -> dict:
    """Components (position, grounded, mass), constraints (type, health), total mass; with faces_of,
    the face table of that component in assembly coordinates (for picking constraint faces)."""
    asm = s.assembly()
    cd = asm.ComponentDefinition
    out = {"occurrences": [_occ_row(o) for o in items(cd.Occurrences)],
           "constraints": [{"name": c.Name, "type": K.name_of(c.Type, "k").replace("k", "", 1).replace("Object", ""),
                            "health": K.name_of(safe(lambda c=c: c.HealthStatus, 0), "k"),
                            "suppressed": bool(safe(lambda c=c: c.Suppressed, False))} for c in items(cd.Constraints)],
           "mass_kg": safe(lambda: r(cd.MassProperties.Mass, 5))}
    if faces_of:
        o = _occ(s, faces_of)
        out["faces"] = [face_row(s, i, f) for i, f in enumerate(items(o.SurfaceBodies.Item(1).Faces), 1)][:80]
    return out


@tool("assembly", mutates=False)
def check_interference(s: Session) -> dict:
    """Interference check across all components: which pairs overlap and by how much volume."""
    cd = s.assembly().ComponentDefinition
    occs = list(items(cd.Occurrences))
    if len(occs) < 2:
        return {"interferences": [], "note": "fewer than two components"}
    res = cd.AnalyzeInterference(s.collection(occs))
    rows = []
    for it in items(res):
        c = it.Centroid
        rows.append({"one": it.OccurrenceOne.Name, "two": it.OccurrenceTwo.Name,
                     "volume_mm3": r(it.Volume * CM3_TO_MM3, 3),
                     "centroid_mm": [r(c.X * 10), r(c.Y * 10), r(c.Z * 10)]})
    return {"interferences": rows, "count": len(rows), "clear": not rows}


@tool("assembly", mutates=False)
def check_assembly(s: Session) -> dict:
    """One-call health check of an assembly: interferences, constraint health, overall size, mass, ungrounded components
    with no constraints."""
    from collections import Counter
    cd = s.assembly().ComponentDefinition
    occs = list(items(cd.Occurrences))
    clash = check_interference(s)
    health = Counter(K.name_of(safe(lambda c=c: c.HealthStatus, 0), "k") for c in items(cd.Constraints))
    rb = cd.RangeBox
    loose = [o.Name for o in occs if not safe(lambda o=o: o.Grounded, False) and safe(lambda o=o: o.Constraints.Count, 0) == 0]
    bad = sum(v for k, v in health.items() if k != "kUpToDateHealth")
    return {"components": len(occs), "interferences": clash.get("interferences", []), "constraints": dict(health),
            "size_mm": [r((getattr(rb.MaxPoint, a) - getattr(rb.MinPoint, a)) * 10, 1) for a in "XYZ"],
            "mass_kg": safe(lambda: r(cd.MassProperties.Mass, 3)), "unconstrained": loose,
            "ok": not clash.get("interferences") and bad == 0}


@tool("assembly", mutates=False)
def component_gap(s: Session, occurrence_one: str, occurrence_two: str) -> dict:
    """Smallest distance between two components, in mm (0 when they touch or overlap)."""
    d = s.app.MeasureTools.GetMinimumDistance(_occ(s, occurrence_one), _occ(s, occurrence_two))
    d = d[0] if isinstance(d, tuple) else d          # late binding returns the [out] arguments alongside it
    return {"gap_mm": r(d * 10, 4)}


@tool("assembly", mutates=False)
def bill_of_materials(s: Session, csv_path: str = "") -> dict:
    """Parts-only BOM: item, quantity, part number, description, material, mass; optional CSV export."""
    asm = s.assembly()
    bom = asm.ComponentDefinition.BOM
    bom.PartsOnlyViewEnabled = True
    view = None
    for v in items(bom.BOMViews):
        if "Parts" in v.Name:
            view = v
    view = view or bom.BOMViews.Item(1)
    rows = []
    for row in items(view.BOMRows):
        cdef = row.ComponentDefinitions.Item(1)
        doc = cdef.Document
        ps = lambda setname, prop: safe(lambda: str(doc.PropertySets.Item(setname).Item(prop).Value), "")
        rows.append({"item": row.ItemNumber, "qty": row.ItemQuantity,
                     "part_number": ps("Design Tracking Properties", "Part Number"),
                     "description": ps("Design Tracking Properties", "Description"),
                     "material": safe(lambda: doc.ActiveMaterial.DisplayName, ""),
                     "unit_mass_kg": safe(lambda: r(cdef.MassProperties.Mass, 4))})
    out = {"bom": rows}
    if csv_path:
        if not os.path.isabs(csv_path):
            csv_path = os.path.join(s.workdir, "exports", csv_path)
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["item"])
            w.writeheader()
            w.writerows(rows)
        out["csv"] = csv_path
    return out


@tool("assembly")
def pattern_component(s: Session, occurrences: list[str], axis: str = "Z", count: int = 6, angle: float = 60.0,
                      linear: bool = False, spacing: float = 0.0) -> dict:
    """Circular (angle = degrees between copies) or linear (linear=true, spacing in mm) pattern of components along an
    assembly origin axis X/Y/Z."""
    cd = s.assembly().ComponentDefinition
    parents = s.collection([_occ(s, o) for o in occurrences])
    ax = cd.WorkAxes.Item({"X": 1, "Y": 2, "Z": 3}[axis.upper()])
    if linear:
        pat = cd.OccurrencePatterns.AddRectangularPattern(parents, ax, True, spacing * MM, int(count))
    else:
        pat = cd.OccurrencePatterns.AddCircularPattern(parents, ax, True, math.radians(angle), int(count))
    return {"pattern": pat.Name, "occurrences": cd.Occurrences.Count}
