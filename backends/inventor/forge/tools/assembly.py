"""Assemblies: placing, constraining, grounding, moving, interference, BOM."""
from __future__ import annotations

import contextlib
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
    from .drawing import exact_occurrence
    return exact_occurrence(list(items(occs)), str(name))


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
    optionally rotated about X/Y/Z by rotate_angle degrees. The first component is usually grounded. In an assembly
    with several model states the component is added to every state (Inventor otherwise adds it to the active one
    only)."""
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
    with all_members(asm) as states:
        occ = asm.ComponentDefinition.Occurrences.Add(full, m)
        if grounded:
            occ.Grounded = True
    out = {"placed": _occ_row(occ), "occurrences": asm.ComponentDefinition.Occurrences.Count}
    if states > 1:
        out["model_states"] = f"added to all {states} model states"
    return out


@contextlib.contextmanager
def all_members(doc):
    """While inside, edits to an assembly or part with several model states go to every state (MemberEditScope =
    all members) instead of only the active one. Yields the number of model states."""
    states = safe(lambda: doc.ComponentDefinition.ModelStates)
    n = safe(lambda: states.Count, 0) or 0
    was = safe(lambda: states.MemberEditScope) if n > 1 else None
    if n > 1:
        safe(lambda: setattr(states, "MemberEditScope", K.kEditAllMembers))
    try:
        yield n
    finally:
        if was is not None:
            safe(lambda: setattr(states, "MemberEditScope", was))


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
    was_enabled = bool(safe(lambda: bom.PartsOnlyViewEnabled, True))
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
    if not was_enabled:                               # read the BOM without changing the assembly's settings
        safe(lambda: setattr(bom, "PartsOnlyViewEnabled", False))
    out = {"bom": rows}
    if csv_path:
        from ..writes import output_path
        csv_path = output_path(s.workdir, csv_path, "exports", ".csv")
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


@tool("assembly")
def sweep_clearance(s: Session, constraint: str, start: float, stop: float, steps: int = 12,
                    moving: str = "", against: str = "") -> dict:
    """Drive an angle or offset constraint through a range and, at every step, check for clashes and the smallest
    gap between the moving component and the rest: hinges, levers, steering, sliders. The constraint is put back
    to where it was afterwards. Gives the first value where parts collide and the tightest clearance.

    constraint: the constraint's name (inspect_assembly lists them); an angle constraint is driven in degrees,
      any other (mate/flush/insert offset) in mm
    start: first value
    stop: last value
    steps: number of intervals (steps + 1 positions are checked)
    moving: the component that moves (default: the constraint's second component)
    against: only measure the gap to this component (default: every other component)
    """
    asm = s.assembly()
    cd = asm.ComponentDefinition
    c = next((x for x in items(cd.Constraints) if x.Name.lower() == constraint.lower()), None)
    if c is None:
        raise InventorError(f"no constraint {constraint!r}; constraints: {[x.Name for x in items(cd.Constraints)]}")
    is_angle = safe(lambda: c.Angle, None) is not None
    param = c.Angle if is_angle else safe(lambda: c.Offset)
    if param is None:
        raise InventorError(f"{constraint} has no angle or offset to drive")
    unit = "deg" if is_angle else "mm"
    mover = _occ(s, moving) if moving else safe(lambda: c.OccurrenceTwo) or safe(lambda: c.OccurrenceOne)
    if mover is None:
        raise InventorError("couldn't tell which component moves; give moving")
    others = [_occ(s, against)] if against else [o for o in items(cd.Occurrences) if o.Name != mover.Name]
    original = param.Expression
    n = max(1, int(steps))
    rows, first_clash, tightest = [], None, None
    try:
        for i in range(n + 1):
            value = float(start) + (float(stop) - float(start)) * i / n
            param.Expression = f"{value:.6g} {unit}"
            asm.Update()
            clashes = []
            res = cd.AnalyzeInterference(s.collection([mover]), s.collection(others))
            for it in items(res):
                other = it.OccurrenceTwo if it.OccurrenceOne.Name == mover.Name else it.OccurrenceOne
                clashes.append({"with": other.Name, "volume_mm3": r(it.Volume * CM3_TO_MM3, 3)})
            gap, gap_to = None, ""
            if not clashes:
                for o in others:
                    d = safe(lambda o=o: s.app.MeasureTools.GetMinimumDistance(mover, o))
                    d = d[0] if isinstance(d, tuple) else d
                    if d is not None and (gap is None or d * 10 < gap):
                        gap, gap_to = d * 10, o.Name
            row = {"value": r(value, 4), "clashes": clashes,
                   "min_gap_mm": (0.0 if clashes else None) if gap is None else r(gap, 3),
                   "nearest": gap_to or (clashes[0]["with"] if clashes else "")}
            rows.append(row)
            if clashes and first_clash is None:
                first_clash = row
            if not clashes and gap is not None and (tightest is None or gap < tightest["min_gap_mm"]):
                tightest = row
    finally:
        param.Expression = original
        safe(lambda: asm.Update())
    return {"constraint": c.Name, "moving": mover.Name, "unit": unit, "steps": rows,
            "first_clash_at": first_clash["value"] if first_clash else None,
            "first_clash_with": [x["with"] for x in first_clash["clashes"]] if first_clash else [],
            "tightest_clearance": {"at": tightest["value"], "gap_mm": tightest["min_gap_mm"], "to": tightest["nearest"]}
            if tightest else None,
            "restored": original}


# --------------------------------------------------------------------------- copying an assembly
def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path or ""))


def renamed(name: str, prefix: str, old_prefix: str = "") -> str:
    """A file name with a new prefix: old_prefix replaced when the name starts with it, else prefix added. Only the
    file name changes, never a folder (a text replace on whole paths also rewrites folders that share the prefix)."""
    stem, ext = os.path.splitext(name)
    if old_prefix and stem.lower().startswith(old_prefix.lower()):
        stem = stem[len(old_prefix):]
    return prefix + stem + ext


def copy_plan(files: list[str], root: str, folder: str, prefix: str, old_prefix: str = "") -> dict:
    """{old path: new path} for every file under root: the same sub-folders under folder, file names renamed."""
    root_n = _norm(root).rstrip("\\/") + os.sep
    plan = {}
    for f in files:
        n = _norm(f)
        if not n.startswith(root_n):
            continue
        rel = os.path.relpath(os.path.dirname(os.path.abspath(f)), os.path.abspath(root))
        sub = "" if rel == "." else rel
        plan[n] = os.path.join(os.path.abspath(folder), sub, renamed(os.path.basename(f), prefix, old_prefix))
    return plan


def _walk_files(top) -> tuple[dict, list]:
    """Every file reachable through File.ReferencedFileDescriptors (which, unlike AllReferencedDocuments, includes
    files used only in model states that aren't active), and the references that are missing."""
    files, missing = {}, []
    todo = [top]
    while todo:
        f = todo.pop()
        fn = _norm(safe(lambda f=f: f.FullFileName, ""))
        if not fn or fn in files:
            continue
        files[fn] = f
        for fd in items(safe(lambda f=f: f.ReferencedFileDescriptors) or []):
            if safe(lambda fd=fd: fd.ReferenceMissing, False):
                missing.append(safe(lambda fd=fd: fd.FullFileName, "?"))
                continue
            rf = safe(lambda fd=fd: fd.ReferencedFile)
            if rf is not None:
                todo.append(rf)
    return files, missing


def _links_into(top, root: str) -> list[dict]:
    """References anywhere in the tree that still point into root."""
    root_n = _norm(root).rstrip("\\/") + os.sep
    out = []
    files, _ = _walk_files(top)
    for fn, f in files.items():
        for fd in items(safe(lambda f=f: f.ReferencedFileDescriptors) or []):
            target = _norm(safe(lambda fd=fd: fd.FullFileName, ""))
            if target.startswith(root_n):
                out.append({"in": os.path.basename(fn), "to": safe(lambda fd=fd: fd.FullFileName, "")})
    return out


def _contact_table(s: Session) -> dict | None:
    """{(study, contact): type} when the document has Stress Analysis studies with loads, else None."""
    from .studies import _contacts, _label, _sa_params, _study_nodes
    if not safe(lambda: _sa_params(s), {}):
        return None
    table = {}
    for study in _study_nodes(s):
        for g, c in _contacts(s, study):
            table[f"{study} | {_label(c)}"] = _label(g)
    return table


@tool("assembly")
def copy_assembly(s: Session, folder: str, prefix: str = "", old_prefix: str = "", source_root: str = "",
                  check_contacts: bool = True) -> dict:
    """Copy the active (saved) assembly and every file it uses from its own folder tree into a new folder, renaming
    the files with a prefix, then point every reference in the copies at the copied files and check that none still
    leads back. References are read from the files themselves, so parts used only in model states that aren't active
    are copied and repointed too (Inventor's Save As leaves those linked to the originals without saying so). Files
    outside the source folder (library and Content Center parts) stay shared. Stress Analysis results files are not
    copied: solve the copies again. Contact types are compared before and after the repoint, because a replaced
    reference can drop contacts that automatic contacts then recreate as Bonded. The source assembly is closed first
    (it must have no unsaved changes) and the copy is left open.

    folder: destination folder (must not already hold files of the same names)
    prefix: put in front of every copied file name (or in place of old_prefix)
    old_prefix: the prefix the source files' names start with, replaced by prefix
    source_root: the folder whose files are copied (default: the assembly's folder)
    check_contacts: compare Stress Analysis contact types before and after the repoint (enters Stress Analysis when
      the assembly has studies)
    """
    import shutil

    from ..writes import guard
    from .docs import _dialog_watchdog, _silent
    d = s.assembly()
    src = safe(lambda: d.FullFileName, "")
    if not src:
        raise InventorError("save the assembly first; copy_assembly copies files")
    if safe(lambda: d.Dirty, False) or any(safe(lambda x=x: x.Dirty, False) for x in items(d.AllReferencedDocuments)):
        raise InventorError("the assembly or one of its parts has unsaved changes; save (or undo) them first so the "
                            "copy matches what is on disk")
    root = os.path.abspath(source_root or os.path.dirname(src))
    folder = os.path.abspath(os.path.join(s.workdir, folder) if not os.path.isabs(folder) else folder)
    if _norm(folder) == _norm(root):
        raise InventorError("the destination folder is the source folder; give a new folder")
    if not prefix and not old_prefix and _norm(folder).startswith(_norm(root).rstrip("\\/") + os.sep):
        raise InventorError("copying into a sub-folder of the source without a prefix would give files that share "
                            "names with the originals; give a prefix")
    files, missing = _walk_files(d.File)
    if missing:
        raise InventorError(f"the assembly has missing references ({missing[:6]}); fix them before copying")
    originals = [safe(lambda f=f: f.FullFileName, "") or n for n, f in files.items()]   # names as written
    plan = copy_plan(originals, root, folder, prefix, old_prefix)
    plan.setdefault(_norm(src), os.path.join(folder, renamed(os.path.basename(src), prefix, old_prefix)))
    clash = [p for p in plan.values() if os.path.exists(p)]
    if clash:
        raise InventorError(f"{len(clash)} destination file(s) already exist, e.g. {clash[0]}; choose a new folder or "
                            "prefix (nothing was copied)")
    for p in plan.values():
        guard(p, "copy to")
    external = sorted(os.path.basename(f) for f in files if f not in plan)
    new_top = plan[_norm(src)]
    # close the source so the copy's documents aren't confused with the originals (they share internal names)
    if s.key(d) == getattr(s, "_current_key", None):
        s.set_current(None)
    d.Close(True)
    for f, dst in plan.items():
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(f, dst)
    with _silent(s), _dialog_watchdog(s):
        top = s.app.Documents.Open(new_top, True)
    top.Activate()
    s.set_current(top)
    out = {"copied": len(plan), "top": new_top, "shared_external_files": external[:30]}
    contacts_before = safe(lambda: _contact_table(s)) if check_contacts else None
    replaced, failed = 0, []
    for _ in range(12):                     # nested assemblies: their references appear once their parents point
        changed = 0
        tree, _m = _walk_files(top.File)
        for fn, f in tree.items():
            for fd in items(safe(lambda f=f: f.ReferencedFileDescriptors) or []):
                target = plan.get(_norm(safe(lambda fd=fd: fd.FullFileName, "")))
                if not target:
                    continue
                try:
                    fd.ReplaceReference(target)
                    changed += 1
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{os.path.basename(fn)} -> {os.path.basename(target)}: {e}")
        replaced += changed
        if not changed:
            break
    safe(lambda: top.Update())
    out["references_replaced"] = replaced
    if failed:
        out["replace_failures"] = sorted(set(failed))[:10]
    contacts_after = safe(lambda: _contact_table(s)) if contacts_before is not None else None
    with _silent(s):
        try:
            top.Save2(True)
        except Exception:  # noqa: BLE001
            top.Save()
    # verify from the saved files: close and read the links back
    top.Close(True)
    s.set_current(None)
    with _silent(s), _dialog_watchdog(s):
        top = s.app.Documents.Open(new_top, True)
    top.Activate()
    s.set_current(top)
    still = _links_into(top.File, root)
    _files, missing_now = _walk_files(top.File)
    out["still_linked_to_source"] = still[:20]
    out["missing_after_copy"] = missing_now[:20]
    out["verified"] = not still and not missing_now
    if contacts_before is not None:
        after = contacts_after or {}
        changed_c = [{"contact": k, "before": v, "after": after.get(k, "(gone)")} for k, v in contacts_before.items()
                     if after.get(k) != v]
        added = [k for k in after if k not in contacts_before]
        out["contacts_checked"] = len(contacts_before)
        if changed_c or added:
            out["contacts_changed"] = changed_c[:20]
            out["contacts_added"] = added[:20]
            out["warning"] = ("Stress Analysis contacts changed with the repoint; set their types again "
                              "(set_contact_type) and list them back before solving")
    elif check_contacts:
        out["contacts_checked"] = 0
    if not out["verified"]:
        out["warning"] = (out.get("warning", "") + "; " if out.get("warning") else "") + \
            "some references still lead to the source folder or are missing; see still_linked_to_source"
    return out
