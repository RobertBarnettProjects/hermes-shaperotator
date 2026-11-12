"""hermes-shaperotator FreeCAD worker — runs inside FreeCAD's bundled Python (no GUI).

Protocol: one JSON request per line on stdin  {"id": n, "op": "...", "args": {...}}
          one JSON reply per line on stdout   {"id": n, "ok": true, "result": {...}}
                                           or {"id": n, "ok": false, "error": "..."}
Anything FreeCAD itself prints is redirected to stderr so stdout stays clean.

Modelling uses Part Design (Body -> Sketch -> Pad/Pocket/Revolution/Hole ...) so models
stay parametric. Edge/face indices are 1-based indices into body.Shape.Edges/Faces.
"""
import hashlib
import io
import json
import math
import os
import sys
import traceback
import contextlib

_REPLY = sys.stdout
sys.stdout = sys.stderr  # FreeCAD chatter must never reach the protocol stream

_BIN = os.path.dirname(sys.executable)
sys.path.append(_BIN)
# ccx and gmsh ship next to FreeCAD's python; FEM finds them with shutil.which (process-local PATH only)
os.environ["PATH"] = _BIN + os.pathsep + os.environ.get("PATH", "")
# Never run from bin/: shutil.which would return a cwd-relative "ccx", and ccxtools changes
# directory into the analysis folder before launching it.
import tempfile  # noqa: E402
os.chdir(tempfile.gettempdir())
import FreeCAD as App  # noqa: E402
import Part  # noqa: E402
import Sketcher  # noqa: E402

V = App.Vector


class OpError(Exception):
    pass


S = {"doc": None, "body": None, "sketch": None, "ns": {}}

PLANE_NORMAL = {"XY": V(0, 0, 1), "XZ": V(0, 1, 0), "YZ": V(1, 0, 0)}
AXIS_VEC = {"X": V(1, 0, 0), "Y": V(0, 1, 0), "Z": V(0, 0, 1)}


# --------------------------------------------------------------- helpers
def _doc():
    if S["doc"] is None:
        raise OpError("no document open — call new_part or open first")
    return S["doc"]


def _body():
    if S["body"] is None:
        raise OpError("no Part Design body in the active document")
    return S["body"]


def _origin(role):
    for f in _body().Origin.OriginFeatures:
        if f.Role == role:
            return f
    raise OpError(f"origin feature {role} not found")


def _shape():
    b = S["body"]
    if b is None:
        return None
    # body.Shape can lag one recompute behind a new transform feature; the Tip is authoritative
    sh = b.Tip.Shape if b.Tip is not None else b.Shape
    return sh if sh and not sh.isNull() and sh.Volume > 1e-9 else None


def _state():
    sh = _shape()
    if sh is None:
        return (0.0, 0, 0)
    return (sh.Volume, len(sh.Faces), len(sh.Edges))


def _delta(label, before):
    after = _state()
    dv = after[0] - before[0]
    return f"{label} | V {after[0]:.0f} ({dv:+.0f} mm³) | F{after[1]} E{after[2]}"


def _features():
    return [o for o in _body().Group if o.isDerivedFrom("PartDesign::Feature")]


def _sketches():
    return [o for o in _body().Group if o.TypeId == "Sketcher::SketchObject"]


def _sketch(index=0):
    if index:
        sks = _sketches()
        if not 1 <= index <= len(sks):
            raise OpError(f"sketch {index} does not exist (have {len(sks)})")
        return sks[index - 1]
    if S["sketch"] is None:
        raise OpError("no sketch — call sketch first")
    return S["sketch"]


def _recompute(obj=None):
    _doc().recompute()
    bad = [o for o in _doc().Objects if "Invalid" in o.State or "Error" in o.State]
    if obj is not None and obj in bad:
        msg = getattr(obj, "getStatusString", lambda: "")() or "recompute failed"
        raise OpError(f"{obj.Name}: {msg}")


def _check_feature(feat, before, label, *, expect="any", retry_reverse=False):
    """Recompute, verify the volume moved the expected way; optionally flip Reversed once."""
    # Body.newObject does not always move the Tip (transform features in 1.1) — make it explicit
    _body().Tip = feat
    try:
        _recompute(feat)
        ok = True
    except OpError:
        ok = False
    dv = _state()[0] - before[0]
    good = ok and ((expect == "add" and dv > 1e-6) or (expect == "remove" and dv < -1e-6) or expect == "any")
    if not good and retry_reverse and hasattr(feat, "Reversed"):
        feat.Reversed = not feat.Reversed
        try:
            _recompute(feat)
            ok = True
        except OpError:
            ok = False
        dv = _state()[0] - before[0]
        good = ok and ((expect == "add" and dv > 1e-6) or (expect == "remove" and dv < -1e-6) or expect == "any")
    if not good:
        name = feat.Name
        _body().removeObject(feat)
        _doc().removeObject(name)
        _doc().recompute()
        raise OpError(f"{label} produced no {'material' if expect == 'add' else 'cut'} — removed it. "
                      "Check the sketch position/plane and the direction.")
    return _delta(f"{feat.Label}", before)


def _bbox(sh):
    b = sh.BoundBox
    return [round(b.XMin, 3), round(b.YMin, 3), round(b.ZMin, 3), round(b.XMax, 3), round(b.YMax, 3), round(b.ZMax, 3)]


def _r(v, n=2):
    return [round(v.x, n), round(v.y, n), round(v.z, n)]


# --------------------------------------------------------------- documents
def op_status():
    if S["doc"] is None:
        return {"connected": True, "document": None}
    sh = _shape()
    return {
        "connected": True,
        "document": S["doc"].Name,
        "file": S["doc"].FileName or None,
        "model_version": hashlib.sha256((
            S["doc"].Name + (sh.exportBrepToString() if sh else "") + repr([
                (o.Name, [(p, str(getattr(o, p))) for p in o.PropertiesList if p not in ("Shape", "Visibility")])
                for o in S["doc"].Objects])).encode()).hexdigest(),
        "features": len(_features()) if S["body"] else 0,
        "volume_mm3": round(sh.Volume, 3) if sh else 0,
    }


def op_connect():
    return {"message": f"FreeCAD {'.'.join(App.Version()[:3])} worker ready (headless)"}


def op_new_part(name="Part"):
    doc = App.newDocument(name or "Part")
    doc.UndoMode = 1
    S.update(doc=doc, body=doc.addObject("PartDesign::Body", "Body"), sketch=None)
    return {"message": f"new part {doc.Name}", "document": doc.Name}


def op_new_assembly():
    raise OpError("assemblies are not implemented on the FreeCAD backend yet")


def op_open(path):
    if not os.path.isfile(path):
        raise OpError(f"file not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fcstd":
        doc = App.openDocument(path)
        bodies = [o for o in doc.Objects if o.TypeId == "PartDesign::Body"]
        S.update(doc=doc, body=bodies[0] if bodies else None, sketch=None)
    elif ext in (".step", ".stp", ".iges", ".igs", ".brep"):
        doc = App.newDocument(os.path.splitext(os.path.basename(path))[0].replace(" ", "_"))
        doc.UndoMode = 1
        shape = Part.read(path)
        body = doc.addObject("PartDesign::Body", "Body")
        base = doc.addObject("Part::Feature", "Imported")
        base.Shape = shape
        body.BaseFeature = base
        S.update(doc=doc, body=body, sketch=None)
        doc.recompute()
    else:
        raise OpError(f"unsupported file type {ext}")
    sh = _shape()
    return {"message": f"opened {path}", "volume_mm3": round(sh.Volume, 3) if sh else 0}


def op_save(path=""):
    doc = _doc()
    if path:
        doc.saveAs(path)
    elif doc.FileName:
        doc.save()
    else:
        raise OpError("document has never been saved — give a path ending in .FCStd")
    return {"message": f"saved {doc.FileName}", "path": doc.FileName}


def op_close():
    if S["doc"] is not None:
        App.closeDocument(S["doc"].Name)
    S.update(doc=None, body=None, sketch=None)
    return {"message": "closed"}


def op_export(path, format=""):
    fmt = (format or os.path.splitext(path)[1].lstrip(".")).upper()
    sh = _shape()
    if sh is None:
        raise OpError("nothing to export — the body has no solid yet")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if fmt in ("STEP", "STP"):
        sh.exportStep(path)
    elif fmt in ("IGES", "IGS"):
        sh.exportIges(path)
    elif fmt == "BREP":
        sh.exportBrep(path)
    elif fmt in ("STL", "3MF", "OBJ"):
        import Mesh
        import MeshPart
        m = MeshPart.meshFromShape(Shape=sh, LinearDeflection=0.05, AngularDeflection=0.2, Relative=False)
        Mesh.Mesh(m).write(path) if fmt == "STL" else Mesh.export([_body()], path)
    elif fmt == "FCSTD":
        _doc().saveAs(path)
    else:
        raise OpError(f"export format {fmt} not supported (STEP, IGES, BREP, STL, 3MF, OBJ, FCStd)")
    return {"message": f"exported {fmt} to {path}", "path": path, "bytes": os.path.getsize(path)}


# --------------------------------------------------------------- sketches
def op_sketch(plane="XY", face_index=0, offset=0.0):
    body = _body()
    sk = body.newObject("Sketcher::SketchObject", "Sketch")
    if face_index:
        if body.Tip is None:
            raise OpError("no solid to sketch on yet")
        sk.AttachmentSupport = [(body.Tip, f"Face{int(face_index)}")]
    else:
        plane = plane.upper()
        if plane not in PLANE_NORMAL:
            raise OpError("plane must be XY, XZ or YZ")
        sk.AttachmentSupport = [(_origin(f"{plane}_Plane"), "")]
    sk.MapMode = "FlatFace"
    _doc().recompute()
    if offset and not face_index:
        # make positive offset follow the documented normal (XY->+Z, XZ->+Y, YZ->+X)
        n = sk.Placement.Rotation.multVec(V(0, 0, 1))
        sign = 1 if n.dot(PLANE_NORMAL[plane]) > 0 else -1
        sk.AttachmentOffset = App.Placement(V(0, 0, sign * float(offset)), App.Rotation())
        _doc().recompute()
    S["sketch"] = sk
    rot = sk.Placement.Rotation
    axes = {k: _r(rot.multVec(v), 3) for k, v in (("u", V(1, 0, 0)), ("v", V(0, 1, 0)), ("normal", V(0, 0, 1)))}
    return {"message": f"{sk.Label} on {'Face' + str(face_index) if face_index else plane}"
                       f"{f' offset {offset} mm' if offset else ''}",
            "sketch_index": len(_sketches()), "axes_global": axes,
            "origin_global": _r(sk.Placement.Base, 3)}


def _add_closed(sk, pts):
    n = len(pts)
    first = sk.GeometryCount
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        sk.addGeometry(Part.LineSegment(V(a[0], a[1], 0), V(b[0], b[1], 0)), False)
    for i in range(n):
        sk.addConstraint(Sketcher.Constraint("Coincident", first + i, 2, first + (i + 1) % n, 1))
    return first


def op_rect(x=0.0, y=0.0, width=100.0, height=50.0, sketch_index=0):
    sk = _sketch(sketch_index)
    w, h = float(width) / 2, float(height) / 2
    g = _add_closed(sk, [(x - w, y - h), (x + w, y - h), (x + w, y + h), (x - w, y + h)])
    sk.addConstraint(Sketcher.Constraint("Horizontal", g))
    sk.addConstraint(Sketcher.Constraint("Horizontal", g + 2))
    sk.addConstraint(Sketcher.Constraint("Vertical", g + 1))
    sk.addConstraint(Sketcher.Constraint("Vertical", g + 3))
    sk.addConstraint(Sketcher.Constraint("DistanceX", g, 1, g, 2, float(width)))
    sk.addConstraint(Sketcher.Constraint("DistanceY", g + 1, 1, g + 1, 2, float(height)))
    _doc().recompute()
    return {"message": f"rectangle {width}×{height} at ({x}, {y}) in {sk.Label}"}


def op_circle(x=0.0, y=0.0, diameter=50.0, sketch_index=0, construction=False):
    sk = _sketch(sketch_index)
    g = sk.addGeometry(Part.Circle(V(x, y, 0), V(0, 0, 1), float(diameter) / 2), bool(construction))
    sk.addConstraint(Sketcher.Constraint("Diameter", g, float(diameter)))
    _doc().recompute()
    return {"message": f"circle Ø{diameter} at ({x}, {y}) in {sk.Label}{' (construction)' if construction else ''}"}


def op_line(x1=0.0, y1=0.0, x2=100.0, y2=0.0, sketch_index=0, construction=False):
    sk = _sketch(sketch_index)
    sk.addGeometry(Part.LineSegment(V(x1, y1, 0), V(x2, y2, 0)), bool(construction))
    _doc().recompute()
    return {"message": f"line ({x1},{y1})→({x2},{y2}) in {sk.Label}"}


def op_polygon(x=0.0, y=0.0, radius=25.0, sides=6, sketch_index=0):
    sk = _sketch(sketch_index)
    sides = int(sides)
    if sides < 3:
        raise OpError("polygon needs at least 3 sides")
    pts = [(x + radius * math.cos(2 * math.pi * i / sides), y + radius * math.sin(2 * math.pi * i / sides))
           for i in range(sides)]
    _add_closed(sk, pts)
    _doc().recompute()
    return {"message": f"{sides}-gon R{radius} at ({x}, {y}) in {sk.Label}"}


def op_profile(points, sketch_index=0):
    sk = _sketch(sketch_index)
    if len(points) < 3:
        raise OpError("profile needs at least 3 points")
    _add_closed(sk, [(float(p[0]), float(p[1])) for p in points])
    _doc().recompute()
    return {"message": f"closed profile with {len(points)} points in {sk.Label}"}


# --------------------------------------------------------------- features
def op_extrude(distance=10.0, direction="positive", operation="join", sketch_index=0, extent_type="distance"):
    body, sk = _body(), _sketch(sketch_index)
    before = _state()
    operation = operation.lower()
    if operation in ("join", "new"):
        f = body.newObject("PartDesign::Pad", "Pad")
        expect = "add"
    elif operation == "cut":
        f = body.newObject("PartDesign::Pocket", "Pocket")
        expect = "remove"
    else:
        raise OpError(f"operation {operation!r} not supported on FreeCAD (join, cut)")
    f.Profile = sk
    if extent_type == "all":
        f.Type = "ThroughAll" if operation == "cut" else "UpToLast"
    else:
        f.Length = float(distance)
    if direction == "symmetric":
        f.Midplane = True
    elif direction == "negative":
        f.Reversed = True
    sk.Visibility = False
    return {"delta": _check_feature(f, before, f.Name, expect=expect, retry_reverse=(operation == "cut"))}


def op_revolve(angle=360.0, axis="Y", sketch_index=0, operation="join"):
    body, sk = _body(), _sketch(sketch_index)
    before = _state()
    cut = operation.lower() == "cut"
    f = body.newObject("PartDesign::Groove" if cut else "PartDesign::Revolution", "Groove" if cut else "Revolution")
    f.Profile = sk
    f.ReferenceAxis = (_origin(f"{axis.upper()}_Axis"), [""])
    f.Angle = float(angle)
    sk.Visibility = False
    return {"delta": _check_feature(f, before, f.Name, expect="remove" if cut else "add")}


def op_hole(x=0.0, y=0.0, diameter=10.0, depth=0.0, tapped=False, pitch=0.0):
    body = _body()
    before = _state()
    if _shape() is None:
        raise OpError("no solid to drill")
    sk = body.newObject("Sketcher::SketchObject", "HoleSketch")
    sk.AttachmentSupport = [(_origin("XY_Plane"), "")]
    sk.MapMode = "FlatFace"
    g = sk.addGeometry(Part.Circle(V(x, y, 0), V(0, 0, 1), float(diameter) / 2), False)
    sk.addConstraint(Sketcher.Constraint("Diameter", g, float(diameter)))
    sk.Visibility = False
    h = body.newObject("PartDesign::Hole", "Hole")
    h.Profile = sk
    try:
        if tapped:
            h.Threaded = True
            h.ThreadType = "ISOMetricProfile" if not pitch else "ISOMetricFineProfile"
            sizes = h.getEnumerationsOfProperty("ThreadSize")
            want = f"M{float(diameter):g}x"
            match = [s for s in sizes if s.startswith(want) and (not pitch or s == f"{want}{float(pitch):g}")]
            if not match:
                raise OpError(f"no ISO thread M{diameter:g}{f'x{pitch:g}' if pitch else ''} in FreeCAD's table")
            h.ThreadSize = match[0]
            h.ModelThread = False
        else:
            h.Diameter = float(diameter)
        if depth and depth > 0:
            h.DepthType = "Dimension"
            h.Depth = float(depth)
        else:
            h.DepthType = "ThroughAll"
    except OpError:
        for o in (h, sk):
            name = o.Name
            body.removeObject(o)
            _doc().removeObject(name)
        _doc().recompute()
        raise
    delta = _check_feature(h, before, "Hole", expect="remove", retry_reverse=True)
    return {"delta": delta, "thread": h.ThreadSize if tapped else None}


def _edge_names(indices):
    sh = _shape()
    if sh is None:
        raise OpError("no solid")
    out = []
    for i in indices:
        i = int(i)
        if not 1 <= i <= len(sh.Edges):
            raise OpError(f"edge {i} out of range (1..{len(sh.Edges)}) — use list_edges/find_edge")
        out.append(f"Edge{i}")
    return out


def op_fillet(radius=2.0, edge_indices=(1,)):
    body = _body()
    before = _state()
    prev = body.Tip
    names = _edge_names(edge_indices)
    f = body.newObject("PartDesign::Fillet", "Fillet")
    f.Base = (prev, names)
    f.Radius = float(radius)
    return {"delta": _check_feature(f, before, "Fillet")}


def op_chamfer(distance=2.0, edge_indices=(1,)):
    body = _body()
    before = _state()
    prev = body.Tip
    names = _edge_names(edge_indices)
    f = body.newObject("PartDesign::Chamfer", "Chamfer")
    f.Base = (prev, names)
    f.Size = float(distance)
    return {"delta": _check_feature(f, before, "Chamfer")}


def op_pattern_circular(feature_index, count=8, angle=360.0, axis="Z"):
    body = _body()
    feats = _features()
    if not 1 <= int(feature_index) <= len(feats):
        raise OpError(f"feature {feature_index} does not exist (have {len(feats)})")
    before = _state()
    p = body.newObject("PartDesign::PolarPattern", "PolarPattern")
    p.Originals = [feats[int(feature_index) - 1]]
    p.Axis = (_origin(f"{axis.upper()}_Axis"), [""])
    p.Angle = float(angle)
    p.Occurrences = int(count)
    orig = feats[int(feature_index) - 1].TypeId
    subtractive = any(k in orig for k in ("Hole", "Pocket", "Groove", "Subtractive"))
    return {"delta": _check_feature(p, before, "PolarPattern", expect="remove" if subtractive else "add")}


def op_delete_feature(indices):
    feats = _features()
    names = []
    for i in sorted({int(i) for i in indices}, reverse=True):
        if not 1 <= i <= len(feats):
            raise OpError(f"feature {i} does not exist")
        f = feats[i - 1]
        names.append(f.Name)
        _body().removeObject(f)
        _doc().removeObject(f.Name)
    _doc().recompute()
    return {"message": f"deleted {', '.join(names)}", "delta": _delta("after delete", _state())}


# --------------------------------------------------------------- parameters
def _sheet(create=False):
    sh = _doc().getObject("Params")
    if sh is None and create:
        sh = _doc().addObject("Spreadsheet::Sheet", "Params")
    return sh


DIM_PROPS = ("Length", "Length2", "Angle", "Radius", "Size", "Diameter", "Depth", "Occurrences")


def op_get_parameters():
    out = []
    sh = _sheet()
    if sh is not None:
        for row in range(1, 200):
            a = sh.getAlias(f"B{row}")
            if a:
                out.append({"name": a, "value": sh.get(a), "kind": "user"})
    for f in _features():
        for p in DIM_PROPS:
            if p in f.PropertiesList:
                v = getattr(f, p)
                val = v.Value if hasattr(v, "Value") else v
                exprs = dict(f.ExpressionEngine)
                out.append({"name": f"{f.Name}.{p}", "value": val, "expression": exprs.get(p), "kind": "model"})
    return {"parameters": out}


def op_set_parameter(name, expression):
    before = _state()
    sh = _sheet()
    expr = str(expression).strip()
    if sh is not None and sh.getCellFromAlias(name):
        sh.set(sh.getCellFromAlias(name), f"={expr}")
    elif "." in name:
        obj_name, prop = name.split(".", 1)
        obj = _doc().getObject(obj_name)
        if obj is None or prop not in obj.PropertiesList:
            raise OpError(f"{name} not found — use get_parameters")
        try:
            value = float(expr.replace("mm", "").replace("deg", "").strip())
            obj.setExpression(prop, None)
            setattr(obj, prop, value)
        except ValueError:
            obj.setExpression(prop, expr)  # e.g. "Params.width * 2"
    else:
        raise OpError(f"parameter {name} not found")
    _recompute()
    return {"message": f"{name} = {expression}", "delta": _delta("recompute", before)}


def op_add_parameter(name, expression, units="mm"):
    sh = _sheet(create=True)
    row = 1
    while sh.getContents(f"B{row}"):
        row += 1
    sh.set(f"A{row}", name)
    expr = str(expression)
    sh.set(f"B{row}", f"={expr} {units}" if units and units not in expr else f"={expr}")
    sh.setAlias(f"B{row}", name)
    _recompute()
    return {"message": f"added Params.{name} = {expression} {units}",
            "hint": f"bind it with set_parameter(name='Pad.Length', expression='Params.{name}')"}


# --------------------------------------------------------------- inspection
def op_inspect():
    doc = _doc()
    sh = _shape()
    feats = []
    for i, f in enumerate(_features(), 1):
        feats.append({"i": i, "name": f.Name, "type": f.TypeId.split("::")[-1],
                      "ok": not ("Invalid" in f.State or "Error" in f.State)})
    return {
        "document": doc.Name,
        "volume_mm3": round(sh.Volume, 3) if sh else 0,
        "area_mm2": round(sh.Area, 3) if sh else 0,
        "bbox": _bbox(sh) if sh else None,
        "faces": len(sh.Faces) if sh else 0,
        "edges": len(sh.Edges) if sh else 0,
        "solids": len(sh.Solids) if sh else 0,
        "valid": bool(sh.isValid()) if sh else None,
        "features": feats,
        "sketches": [s.Label for s in _sketches()],
    }


def op_list_edges(min_length=0.0, max_count=60):
    sh = _shape()
    if sh is None:
        return {"edges": "no solid"}
    lines = []
    for i, e in enumerate(sh.Edges, 1):
        if e.Length < min_length:
            continue
        mid = e.valueAt(e.FirstParameter + (e.LastParameter - e.FirstParameter) / 2)
        kind = type(e.Curve).__name__
        lines.append(f"[{i}] {kind} L={e.Length:.1f} mid=({mid.x:.1f},{mid.y:.1f},{mid.z:.1f})")
        if len(lines) >= max_count:
            lines.append("... (truncated)")
            break
    return {"edges": "\n".join(lines)}


def op_list_faces(min_area=0.0, max_count=60):
    sh = _shape()
    if sh is None:
        return {"faces": "no solid"}
    lines = []
    for i, f in enumerate(sh.Faces, 1):
        if f.Area < min_area:
            continue
        c = f.CenterOfMass
        kind = type(f.Surface).__name__
        extra = ""
        if kind == "Plane":
            n = f.normalAt(0, 0)
            extra = f" n=({n.x:.2f},{n.y:.2f},{n.z:.2f})"
        elif kind == "Cylinder":
            extra = f" Ø{2 * f.Surface.Radius:.2f}"
        lines.append(f"[{i}] {kind} A={f.Area:.0f} c=({c.x:.1f},{c.y:.1f},{c.z:.1f}){extra}")
        if len(lines) >= max_count:
            lines.append("... (truncated)")
            break
    return {"faces": "\n".join(lines)}


def _match(p, x, y, z, tol):
    return all(t is None or abs(a - t) <= tol for a, t in ((p.x, x), (p.y, y), (p.z, z)))


def op_find_edge(x=None, y=None, z=None, tolerance=0.1):
    sh = _shape()
    hits = []
    for i, e in enumerate(sh.Edges if sh else [], 1):
        mid = e.valueAt(e.FirstParameter + (e.LastParameter - e.FirstParameter) / 2)
        if _match(mid, x, y, z, tolerance):
            hits.append(i)
    if not hits:
        raise OpError("no edge midpoint matches — widen tolerance or use list_edges")
    return {"edge": hits[0], "all_matches": hits}


def op_find_face(x=None, y=None, z=None, min_area=100.0, tolerance=0.5):
    sh = _shape()
    hits = []
    for i, f in enumerate(sh.Faces if sh else [], 1):
        if f.Area >= min_area and _match(f.CenterOfMass, x, y, z, tolerance):
            hits.append(i)
    if not hits:
        raise OpError("no face centroid matches — widen tolerance or use list_faces")
    return {"face": hits[0], "all_matches": hits}


def op_render(path, view="iso", width=1600):
    """Headless line render: project edges and save a PNG with matplotlib."""
    sh = _shape()
    if sh is None:
        raise OpError("nothing to render")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    dirs = {"iso": V(1, -1, 1), "front": V(0, -1, 0), "top": V(0, 0, 1), "right": V(1, 0, 0)}
    d = dirs.get(view, dirs["iso"])
    d.normalize()
    up = V(0, 0, 1) if view != "top" else V(0, 1, 0)
    u = up.cross(d)
    u.normalize()
    w = d.cross(u)
    fig = plt.figure(figsize=(width / 200, width / 200 * 0.75), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    for e in sh.Edges:
        pts = e.discretize(Deflection=0.05) if e.Length > 0 else []
        if len(pts) < 2:
            continue
        ax.plot([p.dot(u) for p in pts], [p.dot(w) for p in pts], color="#1f2937", linewidth=0.8)
    ax.set_aspect("equal")
    ax.axis("off")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return {"path": path, "view": view}


# --------------------------------------------------------------- misc
def op_transaction(action="begin", name="hermes-shaperotator"):
    doc = _doc()
    if action == "begin":
        doc.openTransaction(name)
    elif action == "commit":
        doc.commitTransaction()
    elif action == "abort":
        before = _state()
        doc.abortTransaction()
        doc.recompute()
        return {"message": "rolled back", "delta": _delta("abort", before)}
    else:
        raise OpError("action must be begin, commit or abort")
    return {"message": f"transaction {action}"}


def op_python(code):
    ns = S["ns"]
    ns.update(App=App, FreeCAD=App, Part=Part, Sketcher=Sketcher, doc=S["doc"], body=S["body"],
              shape=_shape(), math=math, V=V)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<cad_python>", "exec"), ns)
    except Exception:
        return {"output": buf.getvalue()[-6000:], "error": traceback.format_exc(limit=3)}
    out = buf.getvalue()
    return {"output": out[-6000:] if out else "(ok, no output)"}


def op_fea(spec, out_dir, source_path=""):
    """Linear static FEA of the active body, or of a STEP/IGES/BREP file (e.g. exported from Inventor)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fea
    if source_path:
        if not os.path.isfile(source_path):
            raise OpError(f"file not found: {source_path}")
        shape = Part.read(source_path)
        if len(shape.Solids) == 1:
            shape = shape.Solids[0]
    else:
        shape = _shape()
        if shape is None:
            raise OpError("no solid in the active document")
        shape = shape.copy()
    try:
        return fea.run(shape, spec, out_dir, lambda: App.newDocument("hermes_shaperotator_fea"))
    except fea.FeaError as e:
        raise OpError(str(e))


_DRAWING = {"sheet": None}


def _drawing_mod():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import drawing
    return drawing


def op_drawing_new(standard="AS1100", sheet_size="A3", orientation="landscape", properties=None, drawing_units="mm"):
    d = _drawing_mod()
    try:
        _DRAWING["sheet"] = d.Sheet(standard, sheet_size, orientation, properties or {})
    except d.DrawingError as e:
        raise OpError(str(e))
    s = _DRAWING["sheet"]
    return {"message": f"new {s.size} {orientation} drawing ({s.standard})", "standard": s.standard}


def op_drawing_part_sheet(model_path="", views="front,top,right,iso", scale=0, sheet_size="A3", up="z",
                          new_sheet=False, title="", standard="AS1100"):
    d = _drawing_mod()
    if _DRAWING["sheet"] is None or new_sheet:            # a sheet made by an explicit `new` keeps its own standard
        op_drawing_new(standard=standard, sheet_size=sheet_size)
    sheet = _DRAWING["sheet"]
    if model_path:
        if not os.path.isfile(model_path):
            raise OpError(f"file not found: {model_path}")
        if model_path.lower().endswith(".fcstd"):
            doc = App.openDocument(model_path)
            shapes = [o.Shape for o in doc.Objects if hasattr(o, "Shape") and o.Shape.Solids]
            shape = shapes[-1] if shapes else None
        else:
            shape = Part.read(model_path)
    else:
        shape = _shape()
    if shape is None:
        raise OpError("no solid to draw — build or open a part first")
    if not title:
        title = (os.path.splitext(os.path.basename(model_path))[0] if model_path else _doc().Label)
    try:
        return sheet.part_sheet(shape.copy(), views=str(views).split(","), scale=scale or 0, title=title)
    except d.DrawingError as e:
        raise OpError(str(e))


def _sheet_or_fail():
    if _DRAWING["sheet"] is None or not _DRAWING["sheet"].views:
        raise OpError("no drawing yet — cad_drawing(action='new') then 'part_sheet'")
    return _DRAWING["sheet"]


def op_drawing_set_properties(properties):
    s = _sheet_or_fail()
    s.props.update(properties or {})
    return {"properties": s.props}


def op_drawing_check():
    return _sheet_or_fail().check()


def op_drawing_render(path):
    return {"path": _sheet_or_fail().render(path)}


def op_drawing_export_pdf(path):
    return {"path": _sheet_or_fail().render(path), "message": "PDF written"}


def _tag_parts(mesh, shape, source_path):
    """For an assembly STEP: name each solid (from the STEP product names) and tag faces and edges with it, so the
    pane can colour parts and hide them (e.g. wheels and seat off before analysing the frame)."""
    sols = shape.Solids
    if len(sols) < 2 or len(mesh["faces"]) != len(shape.Faces):
        return
    import Import
    d = App.newDocument("hc_names")
    try:
        Import.insert(source_path, d.Name)
        objs = [o for o in d.Objects if o.TypeId == "Part::Feature" and not o.Shape.isNull() and o.Shape.Solids]
        def signature(solid):
            bb = solid.BoundBox
            c = solid.CenterOfMass
            return [solid.Volume, solid.Area, c.x, c.y, c.z,
                    bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax,
                    len(solid.Faces), len(solid.Edges)] + [coord for point in sorted(
                        (v.Point.x, v.Point.y, v.Point.z) for v in solid.Vertexes) for coord in point]
        names, signatures = [], []          # one entry per solid: a part can hold several bodies
        for o in objs:
            for so in o.Shape.Solids:
                names.append(o.Label)
                signatures.append(signature(so))
    finally:
        App.closeDocument(d.Name)
    matched = []
    used = set()
    for solid in sols:
        sig = signature(solid)
        candidates = [i for i, other in enumerate(signatures) if all(
            abs(a - b) <= 1e-5 * max(1.0, abs(a), abs(b)) for a, b in zip(sig, other)) and len(sig) == len(other)]
        if len(candidates) != 1 or candidates[0] in used:
            matched = []
            break
        used.add(candidates[0])
        matched.append(names[candidates[0]])
    if len(matched) != len(sols):          # fallback: nearest by volume + centre of mass (STEP readers differ slightly)
        matched, used = [], set()
        for solid in sols:
            v, c = solid.Volume, solid.CenterOfMass
            best, bd = None, None
            for i, sg in enumerate(signatures):
                if i in used or abs(sg[0] - v) > 1e-3 * max(1.0, v):
                    continue
                dist = ((sg[2] - c.x) ** 2 + (sg[3] - c.y) ** 2 + (sg[4] - c.z) ** 2) ** 0.5
                if dist < 0.5 and (bd is None or dist < bd):
                    best, bd = i, dist
            if best is None:
                matched = []
                break
            used.add(best)
            matched.append(names[best])
    names = matched if len(matched) == len(sols) else [f"Body {i + 1}" for i in range(len(sols))]
    face_part = []
    for k, s in enumerate(sols):
        face_part += [k] * len(s.Faces)
    if len(face_part) != len(mesh["faces"]):
        return
    for f in mesh["faces"]:
        f["part"] = face_part[f["id"] - 1]
    tol = (shape.BoundBox.DiagonalLength or 1.0) / 400.0
    edges, edge_part = [], []
    for k, s in enumerate(sols):
        for e in s.Edges:
            if e.Length <= 0:
                continue
            pts = e.discretize(Deflection=tol)
            edges.append([round(c, 3) for p in pts for c in (p.x, p.y, p.z)])
            edge_part.append(k)
    mesh["edges"], mesh["edge_part"] = edges, edge_part
    import re
    mesh["parts"] = [{"id": k, "name": n, "kind": re.sub(r"\d+$", "", n)} for k, n in enumerate(names)]


def op_viewer_mesh(out_path, source_path=""):
    """Write a pickable triangle mesh (face id per triangle) of the active body or a STEP file."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import fea
    shape = Part.read(source_path) if source_path else _shape()
    if shape is None or shape.isNull():
        raise OpError("no solid to show")
    mesh = fea.viewer_mesh(shape)
    if source_path:
        try:
            _tag_parts(mesh, shape, source_path)
        except Exception:
            pass                                   # names are a nicety; the mesh is still fine without them
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(mesh, fh)
    return {"path": out_path, "faces": len(mesh["faces"]), "triangles": len(mesh["face_of_tri"])}


def op_shape_faces(source_path="", min_area=0.0):
    """Face table for a STEP file or the active body (helps pick FEA faces on imported geometry)."""
    if source_path:
        shape = Part.read(source_path)
    else:
        shape = _shape()
    if shape is None:
        raise OpError("no solid")
    lines = []
    for i, f in enumerate(shape.Faces, 1):
        if f.Area < min_area:
            continue
        c = f.CenterOfMass
        lines.append(f"[{i}] {type(f.Surface).__name__} A={f.Area:.0f} c=({c.x:.1f},{c.y:.1f},{c.z:.1f})")
    return {"faces": "\n".join(lines[:120])}


OPS = {k[3:]: v for k, v in globals().items() if k.startswith("op_")}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid = req.get("id")
        op = req.get("op")
        if op == "__exit__":
            break
        try:
            fn = OPS[op]
        except KeyError:
            resp = {"id": rid, "ok": False, "error": f"unknown op {op}"}
        else:
            try:
                resp = {"id": rid, "ok": True, "result": fn(**(req.get("args") or {}))}
            except OpError as e:
                resp = {"id": rid, "ok": False, "error": str(e)}
            except Exception as e:  # unexpected: include a short traceback for debugging
                resp = {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}",
                        "trace": traceback.format_exc()}
        _REPLY.write(json.dumps(resp, default=str) + "\n")
        _REPLY.flush()


if __name__ == "__main__":
    _REPLY.write(json.dumps({"id": 0, "ok": True, "result": {"ready": True}}) + "\n")
    _REPLY.flush()
    main()
