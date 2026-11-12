"""Drawings (IDW): sheets, views, dimensions, annotations, parts lists, one-shot sheets, QA, PDF.

Sheet coordinates are millimetres from the sheet's lower-left corner. The drafting standard (template, projection
angle, dimension style) is the session's choice (set_standard): AS 1100, ASME and JIS are third-angle; ISO, DIN,
BS 8888 and GB first-angle.
"""
from __future__ import annotations

import math
import os
import re
import time

from ..com import MM, InventorError, K, items, late, r, safe
from ..registry import tool
from ..session import Session
from .inspect import face_normal

SIZES = {"A0": "kA0DrawingSheetSize", "A1": "kA1DrawingSheetSize", "A2": "kA2DrawingSheetSize",
         "A3": "kA3DrawingSheetSize", "A4": "kA4DrawingSheetSize",
         "A": "kADrawingSheetSize", "B": "kBDrawingSheetSize", "C": "kCDrawingSheetSize",      # ANSI (ASME)
         "D": "kDDrawingSheetSize", "E": "kEDrawingSheetSize"}
ORIENT = {"front": "kFrontViewOrientation", "top": "kTopViewOrientation", "right": "kRightViewOrientation",
          "left": "kLeftViewOrientation", "back": "kBackViewOrientation", "bottom": "kBottomViewOrientation",
          "iso": "kIsoTopRightViewOrientation", "iso_left": "kIsoTopLeftViewOrientation"}
STYLES = {"hidden": "kHiddenLineDrawingViewStyle", "visible": "kHiddenLineRemovedDrawingViewStyle",
          "shaded": "kShadedDrawingViewStyle", "shaded_hidden": "kShadedHiddenLineDrawingViewStyle"}
STANDARD_SCALES = [10, 5, 4, 2.5, 2, 1, 1 / 2, 1 / 2.5, 1 / 4, 1 / 5, 1 / 10, 1 / 20, 1 / 25, 1 / 50, 1 / 100]
PDF_TRANSLATOR = "{0AC6FD96-2F4D-42CE-8BE0-8AEA580399E4}"


def _sheet(s: Session):
    return s.drawing().ActiveSheet


def _view(s: Session, name: str):
    sh = _sheet(s)
    vs = sh.DrawingViews
    if str(name).isdigit():
        i = int(name)
        if not 1 <= i <= vs.Count:
            raise InventorError(f"view {i} out of range 1..{vs.Count}")
        return vs.Item(i)
    for v in items(vs):
        if v.Name.lower() == str(name).lower() or safe(lambda v=v: v.Label.Text, "").lower().startswith(str(name).lower()):
            return v
    raise InventorError(f"no view {name!r} on sheet {sh.Name} (see list_views)")


def _scale_text(sc: float) -> str:
    if sc >= 1:
        return f"{sc:g}:1"
    return f"1:{1 / sc:g}"


def _box(v) -> tuple[float, float, float, float]:
    """(left, bottom, right, top) of a view in sheet mm."""
    left, top = v.Left * 10, v.Top * 10
    return left, top - v.Height * 10, left + v.Width * 10, top


def _lbox(v) -> tuple[float, float, float, float]:
    """View box including its label (drawn about 12 mm above the view when shown)."""
    b = _box(v)
    if not safe(lambda: v.ShowLabel, False):
        return b
    rb = safe(lambda: v.Label.RangeBox)
    if rb is None:
        return b[0], b[1], b[2], b[3] + 12.0
    return (min(b[0], rb.MinPoint.X * 10), min(b[1], rb.MinPoint.Y * 10),
            max(b[2], rb.MaxPoint.X * 10), max(b[3], rb.MaxPoint.Y * 10))

def _view_row(v) -> dict:
    b = _box(v)
    return {"name": v.Name, "scale": _scale_text(v.Scale), "center_mm": [r(v.Position.X * 10, 1), r(v.Position.Y * 10, 1)],
            "box_mm": [r(x, 1) for x in b], "size_mm": [r(b[2] - b[0], 1), r(b[3] - b[1], 1)],
            "type": K.name_of(safe(lambda: v.ViewType, 0), "k").replace("k", "", 1).replace("DrawingViewType", ""),
            "label_shown": bool(safe(lambda: v.ShowLabel, False))}


def _model_doc(s: Session, model: str):
    if not model:
        # the most recently active part/assembly
        for d in items(s.app.Documents.VisibleDocuments):
            if s.kind(d) in ("part", "assembly"):
                return d
        raise InventorError("no part/assembly open — give model=<path or document name>")
    if os.path.exists(model):
        return s.app.Documents.Open(os.path.abspath(model), False)
    for d in items(s.app.Documents):
        if safe(lambda d=d: d.DisplayName, "").lower().split(".")[0] == model.lower().split(".")[0]:
            return d
    raise InventorError(f"model {model!r} is neither a file nor an open document")


@tool("drawing")
def create_drawing(s: Session, standard: str = "", sheet_size: str = "A3", orientation: str = "landscape",
                   name: str = "") -> dict:
    """New drawing document to a drafting standard: the template, projection angle and dimension style follow it.

    standard: AS1100, ISO, ASME, DIN, BS8888, JIS or GB; empty = the session's standard (set_standard), else AS1100
    sheet_size: A0-A4, or A-E for ASME (A4..A0 are mapped to A..E)
    """
    from ..standards import STANDARDS
    k = s.standard_key(standard)
    spec = STANDARDS[k]
    standard = k
    if k == "ASME":
        sheet_size = {"A4": "A", "A3": "B", "A2": "C", "A1": "D", "A0": "E"}.get(sheet_size.upper(), sheet_size)
    app = s.app
    want = spec.get("template_in") if (k == "ASME" and s.drawing_units == "in") else spec["template"]
    try:
        tpl = s.stock_template(want) or s.stock_template("ISO.idw") or app.FileManager.GetTemplateFile(
            K.kDrawingDocumentObject, K.kMetricSystemOfMeasure, K.kISO_DraftingStandard)
    except Exception:
        tpl = ""
    from .docs import _dialog_watchdog, _silent
    with _silent(s), _dialog_watchdog(s) as dog:     # some templates (GB) prompt for title-block entries: take defaults
        d = app.Documents.Add(K.kDrawingDocumentObject, tpl, True)
    prompts = list(getattr(dog, "cancelled", []))
    d.Activate()
    s.set_current(d)
    std = safe(lambda: d.StylesManager.ActiveStandardStyle)
    third = spec["projection"] == "third"
    marker = {"comma": K.kCommaDecimalMarker, "point": K.kPeriodDecimalMarker}.get(spec["decimal"])   # else the template's
    if std is not None:
        safe(lambda: setattr(std, "FirstAngleProjection", not third))
        if True:                                      # every standard: no trailing zeros (metric), inline diameters
            od = safe(lambda: std.ActiveObjectDefaults)
            for attr in ("LinearDimensionStyle", "DiameterDimensionStyle", "RadialDimensionStyle",
                         "BaselineDimensionStyle"):
                ds = safe(lambda a=attr: getattr(od, a)) if od is not None else None
                if ds is not None:
                    if marker is not None:
                        safe(lambda ds=ds: setattr(ds, "DecimalMarkerType", marker))
                    safe(lambda ds=ds: setattr(ds, "TrailingZeroDisplay", False))
                    for attr2 in ("DiameterExternalTextOrientation", "DiameterInternalTextOrientation",
                                  "RadialTextOrientation"):
                        safe(lambda ds=ds, a2=attr2: setattr(ds, a2, K.kInlineHorizontalDimensionText))
    sh = d.ActiveSheet
    _resize(sh, sheet_size, orientation)
    if name:
        safe(lambda: setattr(d, "DisplayName", name))
    return {"drawing": d.DisplayName, "standard": standard, "template": os.path.basename(tpl) if tpl else "",
            "projection": f"{spec['projection']}-angle", "sheet": _sheet_row(sh),
            **({"prompts_cancelled": prompts} if prompts else {}),
            **({"warning": f"Inventor's {want} template isn't installed; used {os.path.basename(tpl) or 'the default'} "
                           "with the standard's projection and dimension style"}
               if os.path.basename(tpl or "") != want else {})}


def _third(standard: str) -> bool:
    from ..standards import third_angle
    return third_angle(standard)


def _resize(sh, size: str, orientation: str):
    key = SIZES.get(size.upper())
    if key:
        safe(lambda: setattr(sh, "Size", getattr(K, key)))
    safe(lambda: setattr(sh, "Orientation", K.kLandscapePageOrientation if orientation.startswith("l")
                         else K.kPortraitPageOrientation))


def _sheet_row(sh) -> dict:
    tb = safe(lambda: sh.TitleBlock)
    row = {"name": sh.Name, "size_mm": [r(sh.Width * 10, 1), r(sh.Height * 10, 1)],
           "views": sh.DrawingViews.Count}
    if tb is not None:
        rb = tb.RangeBox
        row["title_block_mm"] = [r(rb.MinPoint.X * 10, 1), r(rb.MinPoint.Y * 10, 1),
                                 r(rb.MaxPoint.X * 10, 1), r(rb.MaxPoint.Y * 10, 1)]
    return row


@tool("drawing")
def add_sheet(s: Session, sheet_size: str = "A3", orientation: str = "landscape", name: str = "") -> dict:
    """Add a sheet to the drawing (with the template's border and title block) and make it active."""
    d = s.drawing()
    size = getattr(K, SIZES.get(sheet_size.upper(), "kA3DrawingSheetSize"))
    orient = K.kLandscapePageOrientation if orientation.startswith("l") else K.kPortraitPageOrientation
    sh = d.Sheets.Add(size, orient, name) if name else d.Sheets.Add(size, orient)
    first = d.Sheets.Item(1)
    if safe(lambda: first.TitleBlock) is not None and safe(lambda: sh.TitleBlock) is None:
        safe(lambda: sh.AddTitleBlock(first.TitleBlock.Definition))
    if safe(lambda: first.Border) is not None and safe(lambda: sh.Border) is None:
        safe(lambda: sh.AddBorder(first.Border.Definition))
    sh.Activate()
    return {"sheet": _sheet_row(sh), "sheets": d.Sheets.Count}


@tool("drawing")
def activate_sheet(s: Session, sheet: str) -> dict:
    """Activate a sheet by name or 1-based index."""
    d = s.drawing()
    target = None
    if str(sheet).isdigit():
        target = d.Sheets.Item(int(sheet))
    else:
        for sh in items(d.Sheets):
            if sh.Name.lower().startswith(str(sheet).lower()):
                target = sh
    if target is None:
        raise InventorError(f"no sheet {sheet!r}")
    target.Activate()
    return {"sheet": _sheet_row(target)}


@tool("drawing")
def add_base_view(s: Session, model: str = "", x: float = 120.0, y: float = 170.0, scale: float = 1.0,
                  orientation: str = "front", style: str = "visible", name: str = "") -> dict:
    """Base view of a part/assembly (file path or open document name) centred at (x, y) sheet mm.

    scale: e.g. 0.5 for 1:2, 2 for 2:1
    orientation: front | top | right | left | back | bottom | iso | iso_left
    style: visible (hidden lines removed) | hidden | shaded | shaded_hidden
    """
    sh = _sheet(s)
    md = _model_doc(s, model)
    v = sh.DrawingViews.AddBaseView(md, s.tg.CreatePoint2d(x * MM, y * MM), float(scale),
                                    getattr(K, ORIENT[orientation.lower()]), getattr(K, STYLES[style.lower()]))
    if name:
        v.Name = name
    return {"view": _view_row(v)}


@tool("drawing")
def add_projected_view(s: Session, parent: str, x: float, y: float, style: str = "visible", name: str = "") -> dict:
    """Projected view from a parent view, placed at (x, y) sheet mm. Above/below/left/right gives the
    orthographic views (per the drawing's projection angle); a diagonal position gives an isometric."""
    sh = _sheet(s)
    v = sh.DrawingViews.AddProjectedView(_view(s, parent), s.tg.CreatePoint2d(x * MM, y * MM),
                                         getattr(K, STYLES.get(style.lower(), "kFromBaseDrawingViewStyle")))
    if name:
        v.Name = name
    return {"view": _view_row(v)}


def _settle(s: Session, v) -> bool:
    """If a new (unaligned) view overlaps another view, a parts list or the title block, move it to the
    first free spot on the sheet, scanning from the top-right. Returns True if it moved."""
    sh = _sheet(s)
    W, H = sh.Width * 10, sh.Height * 10
    tb = safe(lambda: sh.TitleBlock.RangeBox)
    blocked = [_lbox(o) for o in items(sh.DrawingViews) if o.Name != v.Name]
    if tb is not None:
        blocked.append((tb.MinPoint.X * 10, tb.MinPoint.Y * 10, tb.MaxPoint.X * 10, tb.MaxPoint.Y * 10))
    for pl in items(sh.PartsLists):
        rb = pl.RangeBox
        blocked.append((rb.MinPoint.X * 10, rb.MinPoint.Y * 10, rb.MaxPoint.X * 10, rb.MaxPoint.Y * 10))
    px, py = v.Position.X * 10, v.Position.Y * 10
    lb = _lbox(v)            # view + label, relative to the view's position
    rel = (lb[0] - px - 4, lb[1] - py - 4, lb[2] - px + 4, lb[3] - py + 4)

    def free(cx, cy):
        box = (cx + rel[0], cy + rel[1], cx + rel[2], cy + rel[3])
        if box[0] < 14 or box[1] < 14 or box[2] > W - 14 or box[3] > H - 14:
            return False
        return not any(_overlap(box, o) for o in blocked)

    if free(px, py):
        return False

    def try_at(x, y) -> bool:
        if not free(x, y):
            return False
        v.Position = s.tg.CreatePoint2d(x * MM, y * MM)
        ax, ay = v.Position.X * 10, v.Position.Y * 10      # aligned views snap back on one axis
        return free(ax, ay)

    # aligned (section/projected) views can only slide along one axis: try those lines first
    ys = [y for y in [py + k * 5.0 for k in range(-80, 81)] if 14 < y + rel[1] and y + rel[3] < H - 14]
    for y in sorted(ys, key=lambda y: abs(y - py)):
        if try_at(px, y):
            return True
    xs = [x for x in [px + k * 5.0 for k in range(-120, 121)] if 14 < x + rel[0] and x + rel[2] < W - 14]
    for x in sorted(xs, key=lambda x: abs(x - px)):
        if try_at(x, py):
            return True
    y = H - 14 - rel[3]
    while y + rel[1] > 14:
        x = W - 14 - rel[2]
        while x + rel[0] > 14:
            if try_at(x, y):
                return True
            x -= 5.0
        y -= 5.0
    v.Position = s.tg.CreatePoint2d(px * MM, py * MM)       # nowhere free: leave it for QA to report
    return False


@tool("drawing")
def add_section_view(s: Session, parent: str, x: float, y: float, cut: str = "vertical", offset: float = 0.0,
                     label: str = "A") -> dict:
    """Full section through a parent view along a straight cutting line, placed at (x, y) sheet mm.

    cut: vertical | horizontal — the cutting line direction on the parent view
    offset: mm on the sheet from the parent view's centre to the cutting line
    """
    sh = _sheet(s)
    pv = _view(s, parent)
    l, b0, rr, t = _box(pv)
    cx, cy = (l + rr) / 2, (b0 + t) / 2
    if cut.startswith("v"):
        ends = [(cx + offset, b0 - 6), (cx + offset, t + 6)]
    else:
        ends = [(l - 6, cy + offset), (rr + 6, cy + offset)]
    sk = pv.Sketches.Add()
    sk.Edit()
    pts = [sk.SheetToSketchSpace(s.tg.CreatePoint2d(px * MM, py * MM)) for px, py in ends]
    sk.SketchLines.AddByTwoPoints(pts[0], pts[1])
    sk.ExitEdit()
    v = sh.DrawingViews.AddSectionView(pv, sk, s.tg.CreatePoint2d(x * MM, y * MM), K.kFromBaseDrawingViewStyle,
                                       ShowLabel=True, Name=label)
    moved = safe(lambda: _settle(s, v), False)
    return {"view": _view_row(v), "moved_to_free_space": moved}


@tool("drawing")
def add_detail_view(s: Session, parent: str, center_x: float, center_y: float, radius: float, x: float, y: float,
                    scale: float = 2.0, label: str = "B") -> dict:
    """Circular detail (enlarged) view of part of a parent view. center/radius are sheet mm on the parent."""
    sh = _sheet(s)
    pv = _view(s, parent)
    v = sh.DrawingViews.AddDetailView(pv, s.tg.CreatePoint2d(x * MM, y * MM), K.kFromBaseDrawingViewStyle, True,
                                      s.tg.CreatePoint2d(center_x * MM, center_y * MM), radius * MM,
                                      Scale=float(scale), ShowLabel=True, Name=label)
    if abs(v.Scale - float(scale)) > 1e-9:
        safe(lambda: setattr(v, "ScaleFromBase", False))
        safe(lambda: setattr(v, "Scale", float(scale)))
    moved = safe(lambda: _settle(s, v), False)
    return {"view": _view_row(v), "moved_to_free_space": moved}


@tool("drawing")
def move_view(s: Session, view: str, x: float, y: float) -> dict:
    """Move a view's centre to (x, y) sheet mm (projected children follow alignment)."""
    v = _view(s, view)
    v.Position = s.tg.CreatePoint2d(x * MM, y * MM)
    return {"view": _view_row(v)}


@tool("drawing")
def set_view_scale(s: Session, view: str, scale: float) -> dict:
    """Change a base view's scale (dependent views follow)."""
    v = _view(s, view)
    v.Scale = float(scale)
    return {"view": _view_row(v)}


@tool("drawing", mutates=False)
def list_views(s: Session) -> dict:
    """Views on the active sheet: name, scale, centre, box, size, type."""
    sh = _sheet(s)
    return {"sheet": _sheet_row(sh), "views": [_view_row(v) for v in items(sh.DrawingViews)]}


def _model_entity(s: Session, v, kind: str, index: int, occurrence: str = ""):
    """Model edge/face (by part index) as seen in a drawing view."""
    md = v.ReferencedDocumentDescriptor.ReferencedDocument
    if s.kind(md) == "part":
        body = md.ComponentDefinition.SurfaceBodies.Item(1)
    else:
        if not occurrence:
            raise InventorError("assembly view: give occurrence= the component name")
        occ = None
        for o in items(md.ComponentDefinition.Occurrences):
            if o.Name.lower().startswith(occurrence.lower()):
                occ = o
        if occ is None:
            raise InventorError(f"no occurrence {occurrence!r}")
        body = occ.SurfaceBodies.Item(1)
    coll = body.Edges if kind == "edge" else body.Faces
    if not 1 <= index <= coll.Count:
        raise InventorError(f"{kind} {index} out of range 1..{coll.Count}")
    return coll.Item(index)


def drawing_curves(v, entity=None):
    """DrawingView.DrawingCurves is an indexed property with an optional argument, which late
    binding can't call; invoke it with explicit types."""
    import pythoncom
    o = v._oleobj_
    memid = o.GetIDsOfNames("DrawingCurves")
    arg = entity._oleobj_ if entity is not None else pythoncom.Missing
    res = o.InvokeTypes(memid, 0, pythoncom.DISPATCH_PROPERTYGET, (pythoncom.VT_DISPATCH, 0),
                        ((pythoncom.VT_VARIANT, 17),), arg)
    return late(res)


def _curves(v, entity=None) -> list:
    try:
        return list(items(drawing_curves(v, entity)))
    except Exception:
        return []


def _pt(c):
    """A sheet point on a drawing curve (mid, else start, else centre)."""
    for attr in ("MidPoint", "StartPoint", "CenterPoint"):
        p = safe(lambda a=attr: getattr(c, a))
        if p is not None:
            return p
    return None


def _is_circle(c) -> bool:
    """A full circle as drawn in this view. A circular edge seen side-on is a straight line on the sheet."""
    if safe(lambda: c.CurveType, 0) not in (K.get("kCircleCurve"), K.get("kCircleCurve2d")):
        return False
    if safe(lambda: c.EdgeType) == K.kThreadEdge:     # cosmetic thread lines aren't features
        return False
    rb = safe(lambda: c.Evaluator2D.RangeBox)
    if rb is None:
        return True
    w, h = rb.MaxPoint.X - rb.MinPoint.X, rb.MaxPoint.Y - rb.MinPoint.Y
    return w > 1e-4 and abs(w - h) <= 0.02 * max(w, h)


@tool("drawing")
def add_linear_dimension(s: Session, view: str, edge_one: int, edge_two: int = 0, x: float = 0.0, y: float = 0.0,
                         direction: str = "auto", occurrence: str = "") -> dict:
    """Dimension a model edge's length (one edge) or the distance between two edges, in a view.
    Edge indices are the part's list_edges indices. (x, y) sheet mm for the text; 0,0 = auto.

    direction: auto | horizontal | vertical | aligned
    """
    sh = _sheet(s)
    v = _view(s, view)
    c1 = _curves(v, _model_entity(s, v, "edge", edge_one, occurrence))
    if not c1:
        raise InventorError(f"edge {edge_one} is not visible in view {v.Name}")
    i1 = sh.CreateGeometryIntent(c1[0])
    i2 = None
    if edge_two:
        c2 = _curves(v, _model_entity(s, v, "edge", edge_two, occurrence))
        if not c2:
            raise InventorError(f"edge {edge_two} is not visible in view {v.Name}")
        i2 = sh.CreateGeometryIntent(c2[0])
    if not (x or y):
        b = _box(v)
        mp = _pt(c1[0])
        x, y = mp.X * 10, b[3] + 8 if mp.Y * 10 > (b[1] + b[3]) / 2 else b[1] - 8
    dtype = {"horizontal": "kHorizontalDimensionType", "vertical": "kVerticalDimensionType",
             "aligned": "kAlignedDimensionType"}.get(direction)
    gd = sh.DrawingDimensions.GeneralDimensions
    tp = s.tg.CreatePoint2d(x * MM, y * MM)
    if i2 is not None:
        dim = gd.AddLinear(tp, i1, i2, getattr(K, dtype)) if dtype else gd.AddLinear(tp, i1, i2)
    else:
        dim = gd.AddLinear(tp, i1, None, getattr(K, dtype)) if dtype else gd.AddLinear(tp, i1)
    return {"dimension_mm": r(dim.ModelValue * 10, 3), "text": safe(lambda: dim.Text.Text, "")}


@tool("drawing")
def add_diameter_dimension(s: Session, view: str, edge: int, x: float = 0.0, y: float = 0.0, occurrence: str = "") -> dict:
    """Diameter dimension on a circular edge (list_edges type circle) in a view."""
    sh = _sheet(s)
    v = _view(s, view)
    c = _curves(v, _model_entity(s, v, "edge", edge, occurrence))
    if not c:
        raise InventorError(f"edge {edge} is not visible in view {v.Name}")
    if not (x or y):
        cp = c[0].CenterPoint
        x, y = cp.X * 10 + 15, cp.Y * 10 + 15
    dim = sh.DrawingDimensions.GeneralDimensions.AddDiameter(s.tg.CreatePoint2d(x * MM, y * MM),
                                                             sh.CreateGeometryIntent(c[0]))
    return {"dimension_mm": r(dim.ModelValue * 10, 3)}


@tool("drawing")
def auto_dimension_view(s: Session, view: str, arrange: bool = True) -> dict:
    """Retrieve the model's own (sketch + feature) dimensions into a view — the fastest way to a
    fully-dimensioned view — and optionally arrange them clear of the geometry."""
    sh = _sheet(s)
    v = _view(s, view)
    gd = sh.DrawingDimensions.GeneralDimensions
    try:
        got = gd.Retrieve(v)
        n = got.Count if got is not None else 0
    except Exception as e:
        return {"retrieved": 0, "note": f"no retrievable dimensions ({e.__class__.__name__}) — add them with add_linear_dimension"}
    if arrange and n:
        safe(lambda: sh.DrawingDimensions.Arrange(got))
    return {"retrieved": n, "view": v.Name}


@tool("drawing")
def add_centermarks(s: Session, view: str = "") -> dict:
    """Centre marks on every circular edge in a view (or all views on the sheet)."""
    sh = _sheet(s)
    views = [_view(s, view)] if view else list(items(sh.DrawingViews))
    n = 0
    for v in views:
        if "Iso" in K.name_of(safe(lambda v=v: v.Camera.ViewOrientationType, 0), "k"):
            continue
        seen = set()
        for c in _curves(v):
            if not _is_circle(c):
                continue
            cp = c.CenterPoint
            key = (round(cp.X, 3), round(cp.Y, 3))
            if key in seen:
                continue
            seen.add(key)
            if safe(lambda c=c: sh.Centermarks.Add(sh.CreateGeometryIntent(c)), None) is not None:
                n += 1
    return {"centermarks": n}


@tool("drawing")
def add_hole_note(s: Session, view: str, edge: int, x: float = 0.0, y: float = 0.0) -> dict:
    """Hole/thread note (Ø, depth, thread, counterbore...) on a hole's circular edge in a view."""
    sh = _sheet(s)
    v = _view(s, view)
    c = _curves(v, _model_entity(s, v, "edge", edge))
    if not c:
        raise InventorError(f"edge {edge} is not visible in view {v.Name}")
    if not (x or y):
        cp = c[0].CenterPoint
        x, y = cp.X * 10 + 18, cp.Y * 10 + 18
    note = sh.DrawingNotes.HoleThreadNotes.Add(s.tg.CreatePoint2d(x * MM, y * MM), c[0])
    return {"note": re.sub(r"\bn(?=\d)", "Ø", safe(lambda: note.Text.Text, "") or "")}


@tool("drawing")
def add_general_notes(s: Session, text: str, x: float = 20.0, y: float = 40.0) -> dict:
    """Free text note at (x, y) sheet mm (use \\n for new lines, e.g. "NOTES:\\n1. DEBURR ALL EDGES")."""
    sh = _sheet(s)
    note = sh.DrawingNotes.GeneralNotes.AddFitted(s.tg.CreatePoint2d(x * MM, y * MM),
                                                  text.replace("\\n", "\n").replace("\n", "<Br/>"))
    return {"note": safe(lambda: note.Text, text)}


@tool("drawing")
def add_parts_list(s: Session, view: str = "", x: float = 0.0, y: float = 0.0) -> dict:
    """Parts list (BOM table) for an assembly view; default position above the title block."""
    sh = _sheet(s)
    v = _view(s, view) if view else sh.DrawingViews.Item(1)
    if not (x or y):
        tb = safe(lambda: sh.TitleBlock.RangeBox)
        x = (tb.MaxPoint.X * 10) if tb is not None else sh.Width * 10 - 10
        y = (tb.MaxPoint.Y * 10 + 5) if tb is not None else 80
    pl = sh.PartsLists.Add(v, s.tg.CreatePoint2d(x * MM, y * MM))
    # Position is the table's top-right corner: lift it so its bottom clears the title block
    rb = pl.RangeBox
    height = (rb.MaxPoint.Y - rb.MinPoint.Y) * 10
    width = (rb.MaxPoint.X - rb.MinPoint.X) * 10
    if rb.MinPoint.Y * 10 < y:
        pl.Position = s.tg.CreatePoint2d(x * MM, (y + height) * MM)
    rb = pl.RangeBox
    return {"rows": pl.PartsListRows.Count, "box_mm": [r(rb.MinPoint.X * 10, 1), r(rb.MinPoint.Y * 10, 1),
                                                       r(rb.MaxPoint.X * 10, 1), r(rb.MaxPoint.Y * 10, 1)],
            "size_mm": [r(width, 1), r(height, 1)]}


@tool("drawing")
def auto_balloon(s: Session, view: str = "") -> dict:
    """Balloon every component visible in an assembly view, placed outside the view and linked to
    the parts list numbering."""
    sh = _sheet(s)
    if view:
        v = _view(s, view)
    else:       # an orthographic view shows outlines best
        vs = [w for w in items(sh.DrawingViews) if "Iso" not in K.name_of(safe(lambda w=w: w.Camera.ViewOrientationType, 0), "k")]
        v = vs[-1] if vs else sh.DrawingViews.Item(1)
    md = v.ReferencedDocumentDescriptor.ReferencedDocument
    if s.kind(md) != "assembly":
        raise InventorError("auto_balloon needs an assembly view")
    l, b, rgt, t = _box(v)
    cx, cy = (l + rgt) / 2, (b + t) / 2
    placed, done, taken = 0, set(), []
    for occ in items(md.ComponentDefinition.Occurrences):
        fname = safe(lambda o=occ: o.Definition.Document.FullFileName, occ.Name)
        if fname in done:
            continue
        curves = _curves(v, occ)
        if not curves:
            continue
        c = next((cc for cc in curves if safe(lambda cc=cc: cc.MidPoint) is not None), curves[0])
        mp = _pt(c)
        if mp is None:
            continue
        dx, dy = mp.X * 10 - cx, mp.Y * 10 - cy
        n = math.hypot(dx, dy) or 1
        rad = max(rgt - l, t - b) / 2 + 12
        ang = math.atan2(dy, dx)
        W, H = sh.Width * 10, sh.Height * 10
        tb = safe(lambda: sh.TitleBlock.RangeBox)
        blocked = [_lbox(o) for o in items(sh.DrawingViews) if o.Name != v.Name]
        blocked += [(p.RangeBox.MinPoint.X * 10, p.RangeBox.MinPoint.Y * 10, p.RangeBox.MaxPoint.X * 10, p.RangeBox.MaxPoint.Y * 10)
                    for p in items(sh.PartsLists)]
        if tb is not None:
            blocked.append((tb.MinPoint.X * 10, tb.MinPoint.Y * 10, tb.MaxPoint.X * 10, tb.MaxPoint.Y * 10))

        def spot_ok(px, py):
            box = (px - 8, py - 8, px + 8, py + 8)
            return (18 < px < W - 18 and 18 < py < H - 18 and not _overlap(box, (l, b, rgt, t))
                    and not any(_overlap(box, o) for o in blocked)
                    and all(math.hypot(px - qx, py - qy) >= BALLOON_GAP for qx, qy in taken))

        bx, by = None, None
        for ring in (0, 14, 28, 42):                  # nearest free spot around the view
            for step in range(36):
                sign = 1 if step % 2 else -1
                a2 = ang + sign * (step + 1) // 2 * math.radians(10)
                # an ellipse hugging the view's real shape, not a circle around its longest side
                px = cx + ((rgt - l) / 2 + 14 + ring) * math.cos(a2) * 1.15
                py = cy + ((t - b) / 2 + 14 + ring) * math.sin(a2) * 1.15
                if spot_ok(px, py):
                    bx, by = px, py
                    break
            if bx is not None:
                break
        if bx is None:
            bx, by = cx + dx / n * rad, cy + dy / n * rad
        pts = s.collection([s.tg.CreatePoint2d(bx * MM, by * MM), sh.CreateGeometryIntent(c)])
        if safe(lambda: sh.Balloons.Add(pts), None) is not None:
            placed += 1
            done.add(fname)
            taken.append((bx, by))
    return {"balloons": placed, "view": v.Name}


TB_FIELDS = {"title": ("Inventor Summary Information", "Title"), "drawing_number": ("Design Tracking Properties", "Part Number"),
             "part_number": ("Design Tracking Properties", "Part Number"), "drawn_by": ("Inventor Summary Information", "Author"),
             "author": ("Inventor Summary Information", "Author"), "designer": ("Design Tracking Properties", "Designer"),
             "checked_by": ("Design Tracking Properties", "Checked By"), "approved_by": ("Design Tracking Properties", "Engineer Approved By"),
             "revision": ("Inventor Summary Information", "Revision Number"), "description": ("Design Tracking Properties", "Description"),
             "company": ("Inventor Document Summary Information", "Company"), "material": ("Design Tracking Properties", "Material"),
             "project": ("Design Tracking Properties", "Project"), "subject": ("Inventor Summary Information", "Subject")}


@tool("drawing")
def set_title_block_field(s: Session, fields: dict) -> dict:
    """Fill title-block fields: title, drawing_number, drawn_by, checked_by, approved_by, revision,
    description, company, material, project. Writes the drawing's iProperties (which the title
    block shows) and any prompted entries with a matching name."""
    d = s.drawing()
    done = {}
    for k, val in fields.items():
        key = k.lower().replace(" ", "_")
        if key in TB_FIELDS:
            setname, prop = TB_FIELDS[key]
            ok = safe(lambda: setattr(d.PropertySets.Item(setname).Item(prop), "Value", str(val)) or True, False)
            if ok:
                done[key] = str(val)
        tb = safe(lambda: d.ActiveSheet.TitleBlock)
        if tb is not None:
            for tbx in items(tb.Definition.Sketch.TextBoxes):
                txt = safe(lambda t=tbx: t.Text, "")
                if "<" in txt and k.lower().replace("_", " ") in txt.lower():
                    if safe(lambda t=tbx: tb.SetPromptResultText(t, str(val)) or True, False):
                        done[key] = str(val)
    d.Update()
    return {"set": done, "ignored": [k for k in fields if k.lower().replace(" ", "_") not in done]}


@tool("drawing")
def place_view_labels(s: Session) -> dict:
    """Show a label (name + scale) under every view; names views FRONT/TOP/RIGHT/ISO... from their
    orientation when they still have default names."""
    sh = _sheet(s)
    names = {"kFrontViewOrientation": "FRONT", "kTopViewOrientation": "TOP", "kRightViewOrientation": "RIGHT",
             "kLeftViewOrientation": "LEFT", "kBottomViewOrientation": "BOTTOM", "kBackViewOrientation": "BACK",
             "kIsoTopRightViewOrientation": "ISOMETRIC", "kIsoTopLeftViewOrientation": "ISOMETRIC"}
    out = []
    for v in items(sh.DrawingViews):
        o = K.name_of(safe(lambda v=v: v.Camera.ViewOrientationType, 0), "k")
        if v.Name.upper().startswith("VIEW") and o in names:
            safe(lambda v=v: setattr(v, "Name", names[o] + ("" if names[o] not in [x["name"] for x in out] else " 2")))
        v.ShowLabel = True
        out.append({"name": v.Name, "scale": _scale_text(v.Scale)})
    return {"labels": out}


# --------------------------------------------------------------------------- one-shot sheets
def _region(sh) -> dict:
    """Free drawing area: inside a 10 mm border margin and above the title block band."""
    W, H = sh.Width * 10, sh.Height * 10
    tb = safe(lambda: sh.TitleBlock.RangeBox)
    tb_top = tb.MaxPoint.Y * 10 if tb is not None else 10.0
    tb_left = tb.MinPoint.X * 10 if tb is not None else W
    return {"x0": 22.0, "x1": W - 14.0, "y0": tb_top + 14.0, "y1": H - 14.0, "W": W, "H": H,
            "tb_left": tb_left, "tb_top": tb_top}


def _pick_scale(extent_w: float, extent_h: float, avail_w: float, avail_h: float) -> float:
    for sc in STANDARD_SCALES:
        if extent_w * sc <= avail_w and extent_h * sc <= avail_h:
            return sc
    return STANDARD_SCALES[-1]


def _extents(md) -> list[float]:
    rb = md.ComponentDefinition.RangeBox
    return [(rb.MaxPoint.X - rb.MinPoint.X) * 10, (rb.MaxPoint.Y - rb.MinPoint.Y) * 10,
            (rb.MaxPoint.Z - rb.MinPoint.Z) * 10]


def _best_base(md, L: list[float]) -> str:
    """The orientation for the main view of a part: the one that sees the most holes end-on, then the largest area."""
    from collections import Counter
    axes: Counter = Counter()
    if safe(lambda: md.ComponentDefinition.SurfaceBodies.Count, 0):
        for f in items(md.ComponentDefinition.SurfaceBodies.Item(1).Faces):
            if safe(lambda f=f: f.SurfaceType) == K.kCylinderSurface and not _is_boss_face(f):   # holes, not round outsides
                a = f.Geometry.AxisVector
                comp = [abs(a.X), abs(a.Y), abs(a.Z)]
                if max(comp) > 0.999:
                    axes["XYZ"[comp.index(max(comp))]] += 1
    area = {"Z": L[0] * L[1], "Y": L[0] * L[2], "X": L[2] * L[1]}      # Front looks along Z, Top along Y, Right along X
    best = max("ZYX", key=lambda ax: (axes[ax], area[ax] > 1.01 * max(area[o] for o in "ZYX" if o != ax), ax == "Z"))
    return {"Z": "front", "Y": "top", "X": "right"}[best]


def _base_sizes(L: list[float], base: str, rotate: bool) -> tuple:
    """(width, height, depth) of the main view on the sheet, before scaling."""
    fw, fh, dp = {"front": (L[0], L[1], L[2]), "top": (L[0], L[2], L[1]), "right": (L[2], L[1], L[0])}[base]
    return (fh, fw, dp) if rotate else (fw, fh, dp)


def _layout(s: Session, md, want: list[str], third: bool, scale: float, label_gap: float = 14.0,
            gap: float = 22.0, base: str = "front", rotate: bool = False) -> dict:
    """Place FRONT (+TOP/+RIGHT projected) in the left ~70 % of the free area and an ISOMETRIC in the
    top-right, all clear of the title block. Inventor's Front view of a part shows X across and Y up."""
    sh = _sheet(s)
    g = _region(sh)
    L = _extents(md)
    fw, fh, dp = _base_sizes(L, base, rotate)
    tw, th = fw, dp
    rw, rh = dp, fh
    has_top, has_right = "top" in want, "right" in want
    dm = 30.0          # room left of / below the views for dimensions
    g = dict(g, x0=g["x0"] + dm, y0=g["y0"] + dm * 0.6)
    block_w = (g["x1"] - g["x0"]) * 0.66
    block_h = g["y1"] - g["y0"]
    need_w = fw + (rw if has_right else 0)
    need_h = fh + (th if has_top else 0)
    avail_w = block_w - (gap if has_right else 0)
    avail_h = block_h - (gap if has_top else 0) - label_gap * (2 if has_top else 1)
    sc = scale or _pick_scale(need_w, need_h, avail_w, avail_h)
    used_w = need_w * sc + (gap if has_right else 0)
    used_h = need_h * sc + (gap + label_gap if has_top else 0)
    left = g["x0"] + (block_w - used_w) / 2
    bottom = g["y0"] + (block_h - used_h - label_gap) / 2
    if third:
        fx = left + fw * sc / 2
        fy = bottom + fh * sc / 2
        top_y = fy + fh * sc / 2 + gap + label_gap + th * sc / 2
        right_x = fx + fw * sc / 2 + gap + rw * sc / 2
    else:
        fx = left + (rw * sc + gap if has_right else 0) + fw * sc / 2
        fy = bottom + (th * sc + gap + label_gap if has_top else 0) + fh * sc / 2
        top_y = fy - fh * sc / 2 - gap - label_gap - th * sc / 2
        right_x = fx - fw * sc / 2 - gap - rw * sc / 2
    src = md.FullFileName or md.DisplayName
    made = [add_base_view(s, model=src, x=fx, y=fy, scale=sc, orientation=base, name="FRONT")["view"]]
    if rotate:                                       # long parts lie down; projected views follow the rotation
        _view(s, "FRONT").Rotation = math.pi / 2
    if has_top:
        made.append(add_projected_view(s, "FRONT", fx, top_y, name="TOP")["view"])
    if has_right:
        made.append(add_projected_view(s, "FRONT", right_x, fy, name="RIGHT")["view"])
    if "iso" in want:
        iso_x0 = g["x0"] + block_w + 10
        iso_w, iso_h = g["x1"] - iso_x0, (g["y1"] - g["y0"]) * 0.55
        diag = math.sqrt(L[0] ** 2 + L[1] ** 2 + L[2] ** 2) or 1
        iso_sc = _pick_scale(diag, diag * 0.85, iso_w - 10, iso_h - label_gap)
        iso_sc = min(iso_sc, sc)
        v = sh.DrawingViews.AddBaseView(md, s.tg.CreatePoint2d((iso_x0 + iso_w / 2) * MM,
                                                               (g["y1"] - iso_h / 2) * MM), iso_sc,
                                        K.kIsoTopRightViewOrientation, K.kShadedDrawingViewStyle)
        v.Name = "ISOMETRIC"
        made.append(_view_row(v))
    return {"scale": sc, "views": made}


def _hole_position_dims(s: Session, view_name: str) -> int:
    """Locate every circle in an orthographic view from the view's left and bottom outline edges
    (one dimension per distinct x and per distinct y)."""
    sh = _sheet(s)
    v = _view(s, view_name)
    curves = _curves(v)
    lines = [c for c in curves if safe(lambda c=c: c.CurveType, 0) in (K.get("kLineSegmentCurve"), K.get("kLineSegmentCurve2d"))]
    circles = [c for c in curves if _is_circle(c)]
    if not lines or not circles:
        return 0
    def xs(c):
        a, b = c.StartPoint, c.EndPoint
        return a, b
    verticals = [c for c in lines if abs(xs(c)[0].X - xs(c)[1].X) < 1e-6]
    horizontals = [c for c in lines if abs(xs(c)[0].Y - xs(c)[1].Y) < 1e-6]
    if not verticals or not horizontals:
        return 0
    left = min(verticals, key=lambda c: c.StartPoint.X)
    bottom = min(horizontals, key=lambda c: c.StartPoint.Y)
    b = _box(v)
    gd = sh.DrawingDimensions.GeneralDimensions
    n, done_x, done_y = 0, set(), set()
    centres = {}
    for c in circles:
        cp = c.CenterPoint
        centres.setdefault((round(cp.X, 3), round(cp.Y, 3)), c)
    for k, ((cx, cy), c) in enumerate(sorted(centres.items())):
        if round(cx, 2) not in done_x and abs(cx - left.StartPoint.X) > 1e-4:
            ty = b[1] - 8 - 8 * len(done_x)
            dim = safe(lambda: gd.AddLinear(s.tg.CreatePoint2d(cx, ty * MM), sh.CreateGeometryIntent(left),
                                            sh.CreateGeometryIntent(c, K.kCenterPointIntent),
                                            K.kHorizontalDimensionType))
            if dim is not None:
                n += 1
                done_x.add(round(cx, 2))
        if round(cy, 2) not in done_y and abs(cy - bottom.StartPoint.Y) > 1e-4:
            tx = b[0] - 8 - 8 * len(done_y)
            dim = safe(lambda: gd.AddLinear(s.tg.CreatePoint2d(tx * MM, cy), sh.CreateGeometryIntent(bottom),
                                            sh.CreateGeometryIntent(c, K.kCenterPointIntent),
                                            K.kVerticalDimensionType))
            if dim is not None:
                n += 1
                done_y.add(round(cy, 2))
    return n


def _curve_box(c):
    """(xmin, ymin, xmax, ymax) of a drawing curve on the sheet, cm. Works for silhouette lines too, whose
    start and end points Inventor leaves empty."""
    rb = safe(lambda: c.Evaluator2D.RangeBox)
    if rb is None:
        return None
    return rb.MinPoint.X, rb.MinPoint.Y, rb.MaxPoint.X, rb.MaxPoint.Y


def _straight_outline(v) -> tuple[list, list]:
    """Vertical and horizontal straight curves of a view as (position in cm, curve)."""
    vert, horz = [], []
    for c in _curves(v):
        if _is_circle(c):
            continue
        box = _curve_box(c)
        if box is None:
            continue
        w, h = box[2] - box[0], box[3] - box[1]
        if w < 1e-5 and h > 1e-4:
            vert.append((box[0], c))
        elif h < 1e-5 and w > 1e-4:
            horz.append((box[1], c))
    return vert, horz


def _existing_values(sh, linear_only: bool = False) -> list[float]:
    return [safe(lambda d=d: d.ModelValue * 10, -1.0) for d in items(sh.DrawingDimensions)
            if not linear_only or safe(lambda d=d: d.Type, 0) == K.kLinearGeneralDimensionObject]


def _dimension_view(dim, names) -> str | None:
    """Name of the view a dimension is attached to (retrieved ones attach through curve segments)."""
    for path in ("IntentOne.Geometry.Parent", "Intent.Geometry.Parent",
                 "IntentOne.Geometry.Parent.Parent", "Intent.Geometry.Parent.Parent"):
        obj = dim
        for attr in path.split("."):
            obj = safe(lambda o=obj, a=attr: getattr(o, a))
            if obj is None:
                break
        name = safe(lambda o=obj: o.Name) if obj is not None else None
        if name in names:
            return name
    return None


def _have(values: list[float], v: float, tol: float = 0.02) -> bool:
    return any(abs(x - v) <= tol for x in values)


def _circles_by_centre(v) -> dict:
    """{(cx, cy) in cm: [(model diameter mm, curve), ...]} for the full circles in a view."""
    out: dict = {}
    for c in _curves(v):
        if not _is_circle(c):
            continue
        cp = c.CenterPoint
        box = _curve_box(c)
        if box is None:
            continue
        out.setdefault((round(cp.X, 3), round(cp.Y, 3)), []).append((round((box[2] - box[0]) * 10 / v.Scale, 3), c))
    return out


def _is_boss(c) -> bool:
    """True when a circle in a view is the outside of a boss, not a hole: its cylindrical face looks away from the axis."""
    for f in items(safe(lambda: c.ModelGeometry.Faces) or []):
        if safe(lambda f=f: f.SurfaceType) not in (K.kCylinderSurface, K.kConeSurface):
            continue
        p, g = f.PointOnFace, f.Geometry
        try:
            base, axis = g.BasePoint, g.AxisVector
            n = face_normal(f)
        except Exception:
            continue
        v = [p.X - base.X, p.Y - base.Y, p.Z - base.Z]
        along = v[0] * axis.X + v[1] * axis.Y + v[2] * axis.Z
        radial = [v[0] - along * axis.X, v[1] - along * axis.Y, v[2] - along * axis.Z]
        return sum(radial[i] * n[i] for i in range(3)) > 0
    return False


def _thread_map(md) -> dict:
    """{face key: "M6x1-6H"} for the model's thread features (cosmetic threads drawn as extra edges)."""
    out = {}
    for tf in items(safe(lambda: md.ComponentDefinition.Features.ThreadFeatures) or []):
        info = safe(lambda tf=tf: tf.ThreadInfo)
        desig = safe(lambda: info.ThreadDesignation, "") or ""
        cls = safe(lambda: info.Class, "") or ""
        text = f"{desig}-{cls}" if cls else desig
        for f in items(safe(lambda tf=tf: tf.ThreadedFace) or []):
            out[safe(lambda f=f: f.TransientKey)] = text
    return out


def _curve_faces(c) -> list:
    faces = safe(lambda: c.ModelGeometry.Faces)
    return list(items(faces)) if faces is not None else []


def _spline_holes(v) -> list:
    """Holes drilled into curved faces show as splines, not circles: [(centre (x, y) cm, diameter mm, curve)]."""
    out = []
    seen: list = []
    for c in _curves(v):
        if _is_circle(c) or safe(lambda c=c: c.ProjectedCurveType) == K.get("kLineSegmentCurve2d"):
            continue
        box = _curve_box(c)
        if box is None:
            continue
        w, h = box[2] - box[0], box[3] - box[1]
        if w < 1e-4 or abs(w - h) > 0.06 * max(w, h):
            continue
        size = w * 10 / v.Scale                       # the hole's own face, not the round surface it was drilled into
        cyl = next((f for f in _curve_faces(c) if safe(lambda f=f: f.SurfaceType) == K.kCylinderSurface
                    and not _is_boss_face(f) and abs(f.Geometry.Radius * 20 - size) <= 0.1 * f.Geometry.Radius * 20), None)
        if cyl is None:
            continue
        dia = cyl.Geometry.Radius * 20
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        if any(abs(centre[0] - x) < 0.02 and abs(centre[1] - y) < 0.02 for x, y in seen):   # entry and exit project alike
            continue
        seen.append(centre)
        out.append((centre, round(dia, 3), c))
    return out


def _note_score(sh, box, own=None) -> float:
    """How bad a spot is for a note: overlaps with views, dimension text, other notes, the title block, the sheet edge."""
    score = 0.0
    W, H = sh.Width * 10, sh.Height * 10
    if box[0] < 12 or box[1] < 12 or box[2] > W - 12 or box[3] > H - 12:
        score += 100
    for v in items(sh.DrawingViews):
        if _overlap(box, _box(v), 0.0):
            score += 30
    for d in items(sh.DrawingDimensions):
        tb = _text_box(d)
        if tb and _overlap(box, tb, -1.5):
            score += 20
    for n_ in list(items(sh.DrawingNotes.HoleThreadNotes)) + list(items(sh.DrawingNotes.LeaderNotes)):
        if own is not None and safe(lambda n_=n_: n_.Text.Origin.X) == safe(lambda: own.Text.Origin.X):
            continue
        rb = safe(lambda n_=n_: n_.Text.RangeBox)
        if rb is not None and _overlap(box, _mm_box(rb), -1.5):
            score += 20
    tb = safe(lambda: sh.TitleBlock.RangeBox)
    if tb is not None and _overlap(box, _mm_box(tb), 0):
        score += 50
    return score


def _place_note(s: Session, sh, make, anchor_cm, view_box) -> object:
    """Create a note with `make(point)` at the best of a ring of candidate spots around its anchor; keeps the first spot
    if it is already clear. Leader notes ignore later moves, so each candidate is a fresh note."""
    best = None
    ax, ay = anchor_cm[0] * 10, anchor_cm[1] * 10
    cand = [(view_box[2] + 14, ay)]
    for dist in (14, 24, 36):
        for k in range(16):
            ang = math.radians(k * 22.5)
            cand.append((ax + math.cos(ang) * (dist + (view_box[2] - view_box[0]) / 2 * abs(math.cos(ang))),
                         ay + math.sin(ang) * (dist + (view_box[3] - view_box[1]) / 2 * abs(math.sin(ang)))))
    for i, (x, y) in enumerate(cand):
        note = safe(lambda: make(s.tg.CreatePoint2d(x * MM, y * MM)))
        if note is None:
            continue
        rb = safe(lambda: note.Text.RangeBox)
        if rb is None:
            return note
        sc = _note_score(sh, _mm_box(rb), own=note) + math.hypot(x - ax, y - ay) * 0.06
        if best is None or sc < best[0]:
            if best is not None:
                safe(lambda b=best[1]: b.Delete())
            best = (sc, note, (x, y))
        else:
            safe(lambda: note.Delete())
        if i == 0 and sc < 5:                        # the usual spot is clear
            break
    return best[1] if best else None


def _is_boss_face(f) -> bool:
    p, g = f.PointOnFace, f.Geometry
    try:
        base, axis = g.BasePoint, g.AxisVector
        n = face_normal(f)
    except Exception:
        return False
    v = [p.X - base.X, p.Y - base.Y, p.Z - base.Z]
    along = v[0] * axis.X + v[1] * axis.Y + v[2] * axis.Z
    radial = [v[0] - along * axis.X, v[1] - along * axis.Y, v[2] - along * axis.Z]
    return sum(radial[i] * n[i] for i in range(3)) > 0


def _round_callouts(s: Session, view_name: str) -> dict:
    """Replace ad-hoc diameter dimensions with proper callouts: one hole note per group of equal holes
    ("6× Ø14 ... ON Ø110 PCD") and a diameter dimension for each size concentric with the view."""
    sh = _sheet(s)
    v = _view(s, view_name)
    centres = _circles_by_centre(v)
    if not centres and not _spline_holes(v):
        return {"hole_groups": 0, "diameters": 0}
    covered = {dia for rows in centres.values() for dia, _ in rows}
    for d in list(items(sh.DrawingDimensions)):       # retrieved Ø dimensions are superseded below
        if d.Type == K.kDiameterGeneralDimensionObject and any(abs(d.ModelValue * 10 - x) < 0.01 for x in covered):
            safe(lambda d=d: d.Delete())
    b = _box(v)
    vc = ((b[0] + b[2]) / 2 * MM, (b[1] + b[3]) / 2 * MM)
    gd = sh.DrawingDimensions.GeneralDimensions
    concentric, holes = [], {}
    for (cx, cy), rows in centres.items():
        rows.sort(key=lambda t_: t_[0])
        if math.hypot(cx - vc[0], cy - vc[1]) < 0.02 * max(v.Width, v.Height) + 0.02:
            concentric += rows
        elif not _is_boss(rows[0][1]):
            holes.setdefault(rows[0][0], []).append(((cx, cy), rows[0][1]))
    placed = 0
    done: set = set()
    half = max(b[2] - b[0], b[3] - b[1]) / 2
    for i, (dia, c) in enumerate(sorted(concentric, key=lambda t_: -t_[0])):
        if dia in done:
            continue
        done.add(dia)
        ang = math.radians(200 + 24 * (len(done) - 1))      # lower-left: clear of the label and the view above
        rr = half + 14 + 6 * (len(done) - 1)
        tp = s.tg.CreatePoint2d(vc[0] + rr * MM * math.cos(ang), vc[1] + rr * MM * math.sin(ang))
        if safe(lambda: gd.AddDiameter(tp, sh.CreateGeometryIntent(c)), None) is not None:
            placed += 1
    groups = 0
    used_rows: list[float] = []
    for k, (dia, members) in enumerate(sorted(holes.items(), key=lambda kv: kv[0])):
        # hole-note text follows its leader, so the leader runs level, out of the rightmost hole of the group
        free = [m for m in members if all(abs(m[0][1] - y_) > 0.8 for y_ in used_rows)] or members
        (cx0, cy0), c = max(free, key=lambda m: m[0][0])
        used_rows.append(cy0)
        note = _place_note(s, sh, lambda tp, c=c: sh.DrawingNotes.HoleThreadNotes.Add(tp, c), (cx0, cy0), b)
        if note is None:
            continue
        groups += 1
        n = len(members)
        md_ = safe(lambda: v.ReferencedDocumentDescriptor.ReferencedDocument)
        tmap = _thread_map(md_) if md_ is not None else {}
        thr = next((tmap[k] for k in (safe(lambda f=f: (safe(lambda: f.NativeObject) or f).TransientKey) for f in _curve_faces(c))
                    if k in tmap), None)
        if thr:                                        # a cosmetic thread: call out the thread, not the drill size
            text = note.FormattedHoleThreadNote
            size = (re.search(r"FontSize='([0-9.]+)'", text) or [None, "0.35"])[1]
            tail = " THRU" if "THRU" in text.upper() else ""
            safe(lambda: setattr(note, "FormattedHoleThreadNote",
                                 f"<StyleOverride Font='Tahoma' FontSize='{size}'>{(str(n) + '× ') if n > 1 else ''}{thr}{tail}</StyleOverride>"))
            continue
        radii = [math.hypot(cx - vc[0], cy - vc[1]) for (cx, cy), _ in members]
        pcd = ""
        if n >= 3 and max(radii) - min(radii) < 0.01:
            pcd = f" ON Ø{round(radii[0] * 20 / v.Scale, 2):g} PCD"
        if n > 1 or pcd:
            text = note.FormattedHoleThreadNote
            size = (re.search(r"FontSize='([0-9.]+)'", text) or [None, "0.35"])[1]
            wrap = lambda x: f"<StyleOverride Font='Tahoma' FontSize='{size}'>{x}</StyleOverride>"
            head, sep, rest = text.partition("<Br/>")          # the PCD belongs with the hole size, on line 1
            prefix = wrap(f"{n}× ") if n > 1 else ""
            safe(lambda: setattr(note, "FormattedHoleThreadNote", prefix + head + (wrap(pcd) if pcd else "") + sep + rest))
        tb = safe(lambda: note.Text.RangeBox)              # the text is centred on the leader end: move it clear
        if tb is not None and tb.MinPoint.X * 10 < b[2] + 5:
            dx = (b[2] + 5) * MM - tb.MinPoint.X
            o = note.Text.Origin
            safe(lambda: setattr(note.Text, "Origin", s.tg.CreatePoint2d(o.X + dx, o.Y)))
    # cosmetic threads that aren't hole features, and holes that show as splines: leader notes
    md = safe(lambda: v.ReferencedDocumentDescriptor.ReferencedDocument)
    threads = _thread_map(md) if md is not None else {}
    leaders = 0
    for (cx, cy), rows in centres.items():
        for dia, c in rows:
            key = next((safe(lambda f=f: (f.NativeObject or f).TransientKey) for f in _curve_faces(c)
                        if safe(lambda f=f: (f.NativeObject or f).TransientKey) in threads), None)
            if key is None or any(abs(safe(lambda n_=n_: n_.Text.Origin.Y, -1) - cy) < 0.4 for n_ in items(sh.DrawingNotes.HoleThreadNotes)):
                continue
            n = len([1 for (x2, y2), rr in centres.items() if any(abs(d2 - dia) < 0.01 for d2, _ in rr)])
            text = (f"{n}× " if n > 1 else "") + threads[key]
            if _leader_note(s, sh, c, text, (cx, cy), b):
                leaders += 1
            break
    for (cx, cy), dia, c in _spline_holes(v):
        safe(lambda c=c: sh.Centermarks.Add(sh.CreateGeometryIntent(c)))
        if _leader_note(s, sh, c, f"Ø{dia:g}", (cx, cy), b):
            leaders += 1
    return {"hole_groups": groups, "diameters": placed, "leader_notes": leaders}


def _leader_note(s: Session, sh, curve, text: str, anchor_cm, view_box) -> bool:
    def make(tp):
        pts = s.to.CreateObjectCollection()
        pts.Add(tp)
        pts.Add(sh.CreateGeometryIntent(curve))
        return sh.DrawingNotes.LeaderNotes.Add(pts, text)
    return _place_note(s, sh, make, anchor_cm, view_box) is not None


def _overall_dims(s: Session, view_name: str) -> int:
    """Overall width and height from the view's outline edges, when not already dimensioned."""
    sh = _sheet(s)
    v = _view(s, view_name)
    vert, horz = _straight_outline(v)
    b = _box(v)
    gd = sh.DrawingDimensions.GeneralDimensions
    added = 0
    have = _existing_values(sh)
    have_linear = _existing_values(sh, linear_only=True)
    if len(vert) >= 2:
        lo, hi = min(vert, key=lambda t_: t_[0]), max(vert, key=lambda t_: t_[0])
        val = (hi[0] - lo[0]) * 10 / v.Scale
        if val > 0.01 and not _have(have, val):
            tp = s.tg.CreatePoint2d((b[0] + b[2]) / 2 * MM, (b[1] - 24) * MM)
            if safe(lambda: gd.AddLinear(tp, sh.CreateGeometryIntent(lo[1]), sh.CreateGeometryIntent(hi[1]),
                                         K.kHorizontalDimensionType)) is not None:
                added += 1
    if len(horz) >= 2:
        lo, hi = min(horz, key=lambda t_: t_[0]), max(horz, key=lambda t_: t_[0])
        val = (hi[0] - lo[0]) * 10 / v.Scale
        if val > 0.01 and not _have(have_linear, val):     # a bore of the same size is not a height
            tp = s.tg.CreatePoint2d((b[0] - 24) * MM, (b[1] + b[3]) / 2 * MM)
            if safe(lambda: gd.AddLinear(tp, sh.CreateGeometryIntent(lo[1]), sh.CreateGeometryIntent(hi[1]),
                                         K.kVerticalDimensionType)) is not None:
                added += 1
    return added


def _make_room_for_notes(s: Session) -> int:
    """If a hole note's text lands on a neighbouring view to its right, slide that view further right
    (it stays aligned with its parent), as long as it stays on the sheet and clear of other views."""
    sh = _sheet(s)
    moved = 0
    W = sh.Width * 10
    for n_ in items(sh.DrawingNotes.HoleThreadNotes):
        rb = safe(lambda n_=n_: n_.Text.RangeBox)
        if rb is None:
            continue
        nb = (rb.MinPoint.X * 10, rb.MinPoint.Y * 10, rb.MaxPoint.X * 10, rb.MaxPoint.Y * 10)
        views = list(items(sh.DrawingViews))
        for v in views:
            b = _box(v)
            if not _overlap(nb, b, 0.5) or b[0] < nb[0]:
                continue
            dx = nb[2] + 10 - b[0]
            new_box = (b[0] + dx, b[1], b[2] + dx, b[3])
            others = [_lbox(o) for o in views if o.Name != v.Name]
            if new_box[2] <= W - 14 and not any(_overlap(new_box, o, 1) for o in others):
                v.Position = s.tg.CreatePoint2d(v.Position.X + dx * MM, v.Position.Y)
                moved += 1
    return moved


def _level_dims(s: Session, view_name: str, limit: int = 3) -> int:
    """Dimension the steps inside a view's height (a flange's thickness, a shoulder) from the bottom edge."""
    sh = _sheet(s)
    v = _view(s, view_name)
    _, horz = _straight_outline(v)
    levels: dict = {}
    for y, c in horz:
        levels.setdefault(round(y, 3), c)
    ys = sorted(levels)
    if len(ys) < 3:
        return 0
    b = _box(v)
    gd = sh.DrawingDimensions.GeneralDimensions
    have = _existing_values(sh, linear_only=True)
    added = 0
    for y in ys[1:-1]:
        val = round((y - ys[0]) * 10 / v.Scale, 3)
        if added >= limit or val < 0.01 or _have(have, val):
            continue
        tp = s.tg.CreatePoint2d((b[2] + 12 + 9 * added) * MM, (ys[0] + y) / 2)
        if safe(lambda: gd.AddLinear(tp, sh.CreateGeometryIntent(levels[ys[0]]), sh.CreateGeometryIntent(levels[y]),
                                     K.kVerticalDimensionType)) is not None:
            added += 1
    return added


def _mm_box(rb) -> tuple[float, float, float, float]:
    return rb.MinPoint.X * 10, rb.MinPoint.Y * 10, rb.MaxPoint.X * 10, rb.MaxPoint.Y * 10


def _clear_text_clashes(s: Session) -> int:
    """Lift view labels clear of dimensions that landed on them, and slide hole notes clear of dimension text."""
    sh = _sheet(s)
    texts = [b for b in (_text_box(d) for d in items(sh.DrawingDimensions)) if b]
    moved = 0
    for v in items(sh.DrawingViews):
        rb = safe(lambda v=v: v.Label.RangeBox)
        if rb is None:
            continue
        box = _mm_box(rb)
        hits = [t_ for t_ in texts if _overlap(box, t_, 0.5)]
        if hits:
            lift = max(t_[3] for t_ in hits) + 3.0 - box[1]
            pos = v.Label.Position
            if safe(lambda: setattr(v.Label, "Position", s.tg.CreatePoint2d(pos.X, pos.Y + lift * MM)) or True, False):
                moved += 1
    for n_ in items(sh.DrawingNotes.HoleThreadNotes):
        rb = safe(lambda n_=n_: n_.Text.RangeBox)
        if rb is None:
            continue
        box = _mm_box(rb)
        hits = [t_ for t_ in texts if _overlap(box, t_, 0.5)]
        if hits:
            dx = max(t_[2] for t_ in hits) + 3.0 - box[0]
            o = n_.Text.Origin
            if safe(lambda n_=n_: setattr(n_.Text, "Origin", s.tg.CreatePoint2d(o.X + dx * MM, o.Y)) or True, False):
                moved += 1
    return moved


def _tidy_dimension_text(s: Session) -> int:
    """Pull dimension text that landed outside the drawing area (or on the title block) back in."""
    sh = _sheet(s)
    g = _region(sh)
    moved = 0
    for dim in items(sh.DrawingDimensions):
        o = safe(lambda dim=dim: dim.Text.Origin)
        if o is None:
            continue
        x, y = o.X * 10, o.Y * 10
        nx = min(max(x, 16.0), g["W"] - 16.0)
        ny = min(max(y, 16.0), g["H"] - 16.0)
        if x > g["tb_left"] - 4 and ny < g["tb_top"] + 6:
            ny = g["tb_top"] + 8
        if (nx, ny) != (x, y):
            if safe(lambda dim=dim: setattr(dim.Text, "Origin", s.tg.CreatePoint2d(nx * MM, ny * MM)) or True, False):
                moved += 1
    return moved


@tool("drawing")
def add_projection_symbol(s: Session, x: float = 0.0, y: float = 0.0, third_angle: bool = True) -> dict:
    """Put the projection-angle symbol on the sheet (a truncated cone and two circles, as AS 1100 and ISO call for).

    x: sheet position in mm (0 = just left of the title block)
    y: sheet position in mm
    third_angle: false draws the first-angle symbol
    """
    d = s.drawing()
    sh = _sheet(s)
    name = "Third angle projection" if third_angle else "First angle projection"
    defs = d.SketchedSymbolDefinitions
    defn = next((o for o in items(defs) if o.Name == name), None)
    if defn is None:
        defn = defs.Add(name)
        sk = defn.Edit()
        pt = s.tg.CreatePoint2d
        lines, circles = sk.SketchLines, sk.SketchCircles
        cone_x, ring_x = (0.0, 2.1) if third_angle else (1.5, 0.5)     # the cone's narrow end is on the right in both
        corners = [(cone_x, -0.5), (cone_x + 1.0, -0.25), (cone_x + 1.0, 0.25), (cone_x, 0.5)]
        for (a, b), (c, e) in zip(corners, corners[1:] + corners[:1]):
            lines.AddByTwoPoints(pt(a, b), pt(c, e))
        circles.AddByCenterRadius(pt(ring_x, 0.0), 0.5)
        circles.AddByCenterRadius(pt(ring_x, 0.0), 0.25)
        defn.ExitEdit(True)
    for old in list(items(sh.SketchedSymbols)):
        if safe(lambda o=old: o.Definition.Name) == name:
            safe(lambda o=old: o.Delete())
    g = _region(sh)
    px = x or (g["tb_left"] - 42.0)
    py = y or (g["tb_top"] - 16.0 if g["tb_top"] > 30 else 20.0)
    sym = sh.SketchedSymbols.Add(defn, s.tg.CreatePoint2d(px * MM, py * MM), 0.0, 1.0)
    return {"symbol": name, "position_mm": [r(sym.Position.X * 10, 1), r(sym.Position.Y * 10, 1)]}


@tool("drawing")
def set_view_component_visibility(s: Session, view: str, occurrence: str, visible: bool = False) -> dict:
    """Show or hide one component in a view, e.g. a large reference part that forces a small scale.

    view: view name
    occurrence: top-level component name
    visible: false hides it
    """
    v = _view(s, view)
    occs = v.ReferencedDocumentDescriptor.ReferencedDocument.ComponentDefinition.Occurrences
    match = next((o for o in items(occs) if o.Name == occurrence), None)
    if match is None:
        raise InventorError(f"no component {occurrence!r}; there are {[o.Name for o in items(occs)][:30]}")
    v.SetVisibility(match, bool(visible))
    return {"view": view, "occurrence": occurrence, "visible": bool(visible)}


@tool("drawing")
def make_part_sheet(s: Session, model: str = "", views: str = "front,top,right,iso", scale: float = 0.0,
                    sheet_size: str = "A3", standard: str = "", dimensions: bool = True,
                    title: str = "", drawing_number: str = "", drawn_by: str = "", material: str = "") -> dict:
    """One-shot detail drawing of a part: new drawing, FRONT + projected TOP/RIGHT laid out for the
    projection angle and clear of the title block, a shaded ISOMETRIC, retrieved model dimensions,
    hole position dimensions, centre marks, view labels, title block — then QA. Always render_sheet
    and look, and fix what check_drawing reports.

    views: comma list from front, top, right, iso
    scale: 0 = largest standard scale that fits
    """
    md = _model_doc(s, model)
    if s.kind(md) != "part":
        raise InventorError("make_part_sheet needs a part (use make_assembly_sheet for assemblies)")
    standard = s.standard_key(standard)
    third = _third(standard)
    create_drawing(s, standard=standard, sheet_size=sheet_size)
    want = [w.strip().lower() for w in views.split(",") if w.strip()]
    L = _extents(md)
    base = _best_base(md, L)
    fw, fh, _dp = _base_sizes(L, base, False)
    rotate = fh > 1.4 * fw                          # long parts lie down (shafts and pins horizontal)
    lay = _layout(s, md, want, third, scale, base=base, rotate=rotate)
    steps: dict = {"base_orientation": base, "rotated": rotate}
    if safe(lambda: _tilt_base(s, md), False):
        steps["tilted_part"] = "main view turned square to the largest flat face"
    cross = _cross_hole(s, md, base)
    if os.environ.get("INVENTOR_FORGE_DEBUG"):
        steps["cylinders"] = [(K.name_of(safe(lambda f=f: f.SurfaceType) or 0, "k"), safe(lambda f=f: [round(v, 3) for v in (f.Geometry.AxisVector.X, f.Geometry.AxisVector.Y, f.Geometry.AxisVector.Z)]),
                               safe(lambda f=f: _is_boss_face(f))) for f in items(md.ComponentDefinition.SurfaceBodies.Item(1).Faces)
                              if safe(lambda f=f: f.SurfaceType) != K.kPlaneSurface]
    if cross is not None and "top" in want and dimensions:
        try:
            steps["section"] = _section_for(s, cross)
        except Exception as e:  # noqa: BLE001 - the sheet is still usable without it
            steps["section"] = None
            steps["section_error"] = str(e)[:200]
        if steps["section"]:
            for m in lay["views"]:
                if m["name"] == "TOP":
                    m["name"] = "A"
    ortho = [m["name"] for m in lay["views"] if m["name"] != "ISOMETRIC"]
    if dimensions:
        steps["retrieved"] = sum(auto_dimension_view(s, vn, arrange=True).get("retrieved", 0) for vn in ortho)
        steps["hole_positions"] = safe(lambda: _hole_position_dims(s, "FRONT"), 0)
        steps["callouts"] = safe(lambda: _round_callouts(s, "FRONT"), {})
        steps["views_moved"] = safe(lambda: _make_room_for_notes(s), 0)
        steps["overall"] = sum(safe(lambda vn=vn: _overall_dims(s, vn), 0) for vn in ortho)
        steps["steps"] = sum(safe(lambda vn=vn: _level_dims(s, vn), 0) for vn in ortho)
        if steps.get("section"):
            steps["section_dims"] = safe(lambda: _level_dims(s, "A", limit=6), 0)
        steps["centermarks"] = safe(lambda: add_centermarks(s)["centermarks"], 0)
        sh = _sheet(s)
        safe(lambda: sh.DrawingDimensions.Arrange(s.collection(list(items(sh.DrawingDimensions.GeneralDimensions)))))
        steps["text_moved"] = safe(lambda: _tidy_dimension_text(s), 0)
    place_view_labels(s)
    if dimensions:
        steps["text_clashes_cleared"] = safe(lambda: _clear_text_clashes(s), 0)
        safe(lambda: _make_room_for_notes(s))
    fields = {k: v for k, v in (("title", title or md.DisplayName.split(".")[0].upper()),
                                ("drawing_number", drawing_number or safe(lambda: str(md.PropertySets.Item(
                                    "Design Tracking Properties").Item("Part Number").Value), "")),
                                ("drawn_by", drawn_by), ("material", material or safe(
                                    lambda: md.ActiveMaterial.DisplayName, ""))) if v}
    set_title_block_field(s, fields)
    if third:
        safe(lambda: add_projection_symbol(s))
    return {"scale": _scale_text(lay["scale"]), "views": [m["name"] for m in lay["views"]], **steps,
            "qa": check_drawing(s)}


def _cross_hole(s: Session, md, base: str):
    """A hole lying across the main view (its axis square to the line of sight), which the views can't show: the
    first such hole's (centre point, axis), else None."""
    look = {"front": 2, "top": 1, "right": 0}[base]
    for f in items(safe(lambda: md.ComponentDefinition.SurfaceBodies.Item(1).Faces) or []):
        if safe(lambda f=f: f.SurfaceType) != K.kCylinderSurface or _is_boss_face(f):
            continue
        a = f.Geometry.AxisVector
        comp = [abs(a.X), abs(a.Y), abs(a.Z)]
        if comp[look] < 0.01:
            bp = f.Geometry.BasePoint
            return (bp.X, bp.Y, bp.Z), (a.X, a.Y, a.Z)
    return None


def _section_for(s: Session, cross) -> str:
    """Replace the TOP view by section A-A cut along the cross hole's axis, so the hole shows in full."""
    sh = _sheet(s)
    front = _view(s, "FRONT")
    (px, py, pz), axis = cross
    p = front.ModelToSheetSpace(s.tg.CreatePoint(px, py, pz))
    q = front.ModelToSheetSpace(s.tg.CreatePoint(px + axis[0], py + axis[1], pz + axis[2]))
    horizontal = abs(q.X - p.X) >= abs(q.Y - p.Y)
    l, b0, rr, t = _box(front)
    top = next((v for v in items(sh.DrawingViews) if v.Name == "TOP"), None)
    if horizontal:
        offset = p.Y * 10 - (b0 + t) / 2
        tx, ty = (top.Position.X * 10, top.Position.Y * 10) if top is not None else ((l + rr) / 2, t + 60)
    else:
        offset = p.X * 10 - (l + rr) / 2
        tx, ty = (top.Position.X * 10, top.Position.Y * 10) if top is not None else (rr + 60, (b0 + t) / 2)
    if top is not None:
        top.Delete()
    add_section_view(s, "FRONT", tx, ty, cut="horizontal" if horizontal else "vertical", offset=offset, label="A")
    return "A-A"


def _tilt_base(s: Session, md) -> bool:
    """A part whose largest flat face isn't square to the axes (often an imported STEP part) gets its main view looking
    straight at that face, with its longest straight edge level. True if the view was turned."""
    faces = [f for f in items(safe(lambda: md.ComponentDefinition.SurfaceBodies.Item(1).Faces) or [])
             if safe(lambda f=f: f.SurfaceType) == K.kPlaneSurface]
    if not faces:
        return False
    big = max(faces, key=lambda f: f.Evaluator.Area)
    n = face_normal(big)
    if max(abs(v) for v in n) > 0.999:
        return False
    edges = [e for e in items(big.Edges) if safe(lambda e=e: e.GeometryType) == K.kLineSegmentCurve]
    if edges:
        e = max(edges, key=lambda e: e.StartVertex.Point.DistanceTo(e.StopVertex.Point))
        a, b = e.StartVertex.Point, e.StopVertex.Point
        along = [b.X - a.X, b.Y - a.Y, b.Z - a.Z]
    else:
        along = [1, 0, 0]
    up = [n[1] * along[2] - n[2] * along[1], n[2] * along[0] - n[0] * along[2], n[0] * along[1] - n[1] * along[0]]
    ln = math.sqrt(sum(v * v for v in up)) or 1
    up = [v / ln for v in up]
    v = _view(s, "FRONT")
    cam = v.Camera
    tgt = cam.Target
    dist = max(tgt.DistanceTo(cam.Eye), 100.0)
    cam.Eye = s.tg.CreatePoint(tgt.X + n[0] * dist, tgt.Y + n[1] * dist, tgt.Z + n[2] * dist)
    cam.UpVector = s.tg.CreateUnitVector(*up)
    cam.ApplyWithoutTransition()
    return True


def _ga_sheet_size(s: Session, md, rows: int) -> str:
    """Smallest sheet (A3, A2, A1) that reaches the largest standard scale for the GA views."""
    L = _extents(md)
    need_w, need_h = L[0] + L[2], L[1] + L[2]
    sizes = {"A3": (420, 297), "A2": (594, 420), "A1": (841, 594)}
    best = {}
    for name, (W, H) in sizes.items():
        if name == "A1" and rows <= 20 and best:
            pass
        avail_w, avail_h = (W - 60) * 0.66 - 22, H - 80 - 22 - 28
        best[name] = _pick_scale(need_w, need_h, avail_w, avail_h)
    top = max(best.values())
    return next(n for n in ("A3", "A2", "A1") if best[n] == top)


@tool("drawing")
def make_assembly_sheet(s: Session, model: str = "", scale: float = 0.0, sheet_size: str = "A3",
                        standard: str = "", title: str = "", drawing_number: str = "",
                        drawn_by: str = "") -> dict:
    """One-shot general-arrangement (GA) drawing of an assembly: FRONT + TOP, shaded ISOMETRIC, parts
    list above the title block, balloons, labels, title block, QA.

    sheet_size: A3, A2, A1 or auto (the smallest sheet that reaches the largest standard scale)
    """
    md = _model_doc(s, model)
    if s.kind(md) != "assembly":
        raise InventorError("make_assembly_sheet needs an assembly")
    standard = s.standard_key(standard)
    third = _third(standard)
    if sheet_size.lower() == "auto":
        sheet_size = _ga_sheet_size(s, md, safe(lambda: md.ComponentDefinition.Occurrences.Count, 0))
    create_drawing(s, standard=standard, sheet_size=sheet_size)
    lay = _layout(s, md, ["front", "top", "iso"], third, scale)
    pl = add_parts_list(s, "FRONT")
    # if the iso view now collides with the parts list, shrink it toward the top-right
    sh = _sheet(s)
    iso = next((v for v in items(sh.DrawingViews) if v.Name == "ISOMETRIC"), None)
    if iso is not None:
        for _ in range(4):
            if not _overlap(_box(iso), pl["box_mm"], 1):
                break
            iso.Scale = iso.Scale * 0.75
            b = _box(iso)
            g = _region(sh)
            iso.Position = s.tg.CreatePoint2d((g["x1"] - (b[2] - b[0]) / 2 - 2) * MM,
                                              (g["y1"] - (b[3] - b[1]) / 2 - 2) * MM)
    bl = auto_balloon(s)
    place_view_labels(s)
    set_title_block_field(s, {k: v for k, v in (("title", title or md.DisplayName.split(".")[0].upper()),
                                                ("drawing_number", drawing_number), ("drawn_by", drawn_by)) if v})
    if third:
        safe(lambda: add_projection_symbol(s))
    return {"scale": _scale_text(lay["scale"]), "sheet": sheet_size, "parts_list_rows": pl["rows"], "balloons": bl["balloons"],
            "qa": check_drawing(s)}


@tool("drawing", mutates=False)
def list_drawing_dimensions(s: Session) -> dict:
    """Dimensions on the active sheet: index (for the tools below), value, text, tolerance and text position."""
    sh = _sheet(s)
    rows = []
    for i, d in enumerate(items(sh.DrawingDimensions), 1):
        o = safe(lambda d=d: d.Text.Origin)
        rows.append({"index": i, "value_mm": safe(lambda d=d: r(d.ModelValue * 10, 3)),
                     "text": safe(lambda d=d: d.Text.Text, ""),
                     "tolerance": K.name_of(safe(lambda d=d: d.Tolerance.ToleranceType, 0), "k"),
                     "at_mm": [r(o.X * 10, 1), r(o.Y * 10, 1)] if o is not None else None})
    return {"dimensions": rows, "count": len(rows)}


def _dimension(s: Session, index: int):
    dims = _sheet(s).DrawingDimensions
    if not 1 <= int(index) <= dims.Count:
        raise InventorError(f"dimension {index} out of range 1..{dims.Count} (see list_drawing_dimensions)")
    return dims.Item(int(index))


@tool("drawing")
def set_dimension_tolerance(s: Session, index: int, upper: float | None = None, lower: float | None = None,
                            symmetric: float | None = None, hole_fit: str = "", shaft_fit: str = "") -> dict:
    """Tolerance a drawing dimension: symmetric (±), deviation (upper/lower, mm) or an ISO fit such as hole_fit="H7"
    (a hole), shaft_fit="g6" (a shaft), or both for a fit."""
    d = _dimension(s, index)
    tol = d.Tolerance
    if symmetric is not None:
        tol.SetToSymmetric(float(symmetric) * MM)
    elif upper is not None and lower is not None:
        tol.SetToDeviation(float(upper) * MM, float(lower) * MM)
    elif hole_fit or shaft_fit:
        tol.SetToFits(K.kLimitsFitsStackedTolerance, hole_fit, shaft_fit)
    else:
        raise InventorError("give symmetric, upper + lower, or hole_fit / shaft_fit")
    return {"text": safe(lambda: d.Text.Text, ""), "tolerance": K.name_of(tol.ToleranceType, "k")}


@tool("drawing")
def move_dimension_text(s: Session, index: int, x: float, y: float) -> dict:
    """Move a dimension's text to (x, y) sheet mm."""
    d = _dimension(s, index)
    d.Text.Origin = s.tg.CreatePoint2d(x * MM, y * MM)
    return {"moved": index}


@tool("drawing")
def delete_drawing_dimension(s: Session, index: int) -> dict:
    """Delete a drawing dimension by index (see list_drawing_dimensions)."""
    d = _dimension(s, index)
    text = safe(lambda: d.Text.Text, "")
    d.Delete()
    return {"deleted": text}


@tool("drawing")
def add_surface_finish(s: Session, view: str, edge: int, roughness: str = "3.2", x: float = 0.0, y: float = 0.0,
                       occurrence: str = "") -> dict:
    """Surface texture symbol (machining required) on a model edge in a view; roughness is Ra in µm."""
    sh = _sheet(s)
    v = _view(s, view)
    c = _curves(v, _model_entity(s, v, "edge", edge, occurrence))
    if not c:
        raise InventorError(f"edge {edge} is not visible in view {v.Name}")
    mp = _pt(c[0])
    if not (x or y):
        x, y = mp.X * 10 + 12, mp.Y * 10 + 12
    pts = s.collection([s.tg.CreatePoint2d(x * MM, y * MM), sh.CreateGeometryIntent(c[0])])
    sym = sh.SurfaceTextureSymbols.Add(pts, K.kMaterialRemovalRequiredSurfaceType, MaximumRoughness=str(roughness))
    return {"symbol": True, "ra": str(roughness), "count": sh.SurfaceTextureSymbols.Count, "name": safe(lambda: sym.Type)}


@tool("drawing")
def add_hole_table(s: Session, view: str, x: float = 0.0, y: float = 0.0) -> dict:
    """Hole table for every hole in a view, positioned with its top-left at (x, y) sheet mm (default above the
    title block). Needs a datum in the view; the table is returned as text rows."""
    sh = _sheet(s)
    v = _view(s, view)
    if not (x or y):
        g = _region(sh)
        x, y = g["tb_left"], g["tb_top"] + 60
    if not safe(lambda: v.HasOriginIndicator, False):      # the table measures hole positions from this datum
        vert, horz = _straight_outline(v)
        if not vert:
            raise InventorError("the view has no straight edge to place the hole table's origin on")
        c = min(vert, key=lambda t_: t_[0])[1]
        pt = c.StartPoint
        end = safe(lambda: c.EndPoint)
        intent = K.kStartPointIntent if pt is not None and (end is None or pt.Y <= end.Y) else K.kEndPointIntent
        v.CreateOriginIndicator(sh.CreateGeometryIntent(c, intent))
    ht = sh.HoleTables.Add(v, s.tg.CreatePoint2d(x * MM, y * MM))
    return {"rows": ht.HoleTableRows.Count}


@tool("drawing")
def add_revision_table(s: Session, x: float = 0.0, y: float = 0.0) -> dict:
    """Revision table on the active sheet; default placement is above the title block."""
    sh = _sheet(s)
    if not (x or y):
        g = _region(sh)
        x, y = g["W"] - 14, g["tb_top"] + 40
    rt = sh.RevisionTables.Add(s.tg.CreatePoint2d(x * MM, y * MM))
    rb = rt.RangeBox                                     # keep the whole table on the sheet
    over = rb.MaxPoint.X * 10 - (sh.Width * 10 - 12)
    if over > 0:
        rt.Position = s.tg.CreatePoint2d(rt.Position.X - over * MM, rt.Position.Y)
    rb = rt.RangeBox
    return {"rows": rt.RevisionTableRows.Count,
            "box_mm": [r(rb.MinPoint.X * 10, 1), r(rb.MinPoint.Y * 10, 1), r(rb.MaxPoint.X * 10, 1), r(rb.MaxPoint.Y * 10, 1)]}


# --------------------------------------------------------------------------- QA, render, PDF
def _text_box(dim):
    """Approximate box of a dimension's text (3.5 mm text; vertical dimensions read rotated)."""
    o = safe(lambda: dim.Text.Origin)
    txt = safe(lambda: dim.Text.Text, "") or ""
    if o is None or not txt:
        return None
    w, h = max(len(txt), 1) * 2.6, 4.0
    if safe(lambda: dim.DimensionType, 0) == K.get("kVerticalDimensionType"):
        w, h = h, w
    x, y = o.X * 10, o.Y * 10
    return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)


BALLOON_GAP = 15.0          # mm between balloon centres


def _overlap(a, b, pad=0.0) -> bool:
    return a[0] < b[2] - pad and b[0] < a[2] - pad and a[1] < b[3] - pad and b[1] < a[3] - pad


@tool("drawing", mutates=False)
def check_drawing(s: Session) -> dict:
    """QA of every sheet: views overlapping each other / the title block / the sheet edge, views
    with no dimensions, empty title-block fields, missing parts list or balloons on assemblies.
    Ends with TOTAL ISSUES: N — aim for 0, then render_sheet and look."""
    d = s.drawing()
    issues: list[str] = []
    for sh in items(d.Sheets):
        W, H = sh.Width * 10, sh.Height * 10
        tb = safe(lambda: sh.TitleBlock.RangeBox)
        tbox = (tb.MinPoint.X * 10, tb.MinPoint.Y * 10, tb.MaxPoint.X * 10, tb.MaxPoint.Y * 10) if tb is not None else None
        views = list(items(sh.DrawingViews))
        boxes = [(v.Name, _lbox(v)) for v in views]
        if not views:
            issues.append(f"[{sh.Name}] sheet has no views")
        for i, (n1, b1) in enumerate(boxes):
            if b1[0] < 10 or b1[1] < 10 or b1[2] > W - 10 or b1[3] > H - 10:
                issues.append(f"[{sh.Name}] view {n1} runs outside the border area")
            if tbox and _overlap(b1, tbox, 1):
                issues.append(f"[{sh.Name}] view {n1} overlaps the title block")
            for n2, b2 in boxes[i + 1:]:
                if _overlap(b1, b2, 2):
                    issues.append(f"[{sh.Name}] views {n1} and {n2} overlap")
        for pl in items(sh.PartsLists):
            rb = pl.RangeBox
            pb = (rb.MinPoint.X * 10, rb.MinPoint.Y * 10, rb.MaxPoint.X * 10, rb.MaxPoint.Y * 10)
            if tbox and _overlap(pb, tbox, 1):
                issues.append(f"[{sh.Name}] parts list overlaps the title block")
            for n1, b1 in boxes:
                if _overlap(pb, b1, 1):
                    issues.append(f"[{sh.Name}] parts list overlaps view {n1}")
        tables = [("revision table", t_) for t_ in items(sh.RevisionTables)] + [("hole table", t_) for t_ in items(sh.HoleTables)]
        for tname, t_ in tables:
            rb = t_.RangeBox
            if rb.MaxPoint.X * 10 > W - 5 or rb.MinPoint.X * 10 < 5 or rb.MinPoint.Y * 10 < 5 or rb.MaxPoint.Y * 10 > H - 5:
                issues.append(f"[{sh.Name}] {tname} runs off the sheet")
            tb2 = (rb.MinPoint.X * 10, rb.MinPoint.Y * 10, rb.MaxPoint.X * 10, rb.MaxPoint.Y * 10)
            for n1, b1 in boxes:
                if _overlap(tb2, b1, 1):
                    issues.append(f"[{sh.Name}] {tname} overlaps view {n1}")
        spots = [(safe(lambda b_=b_: b_.Position.X * 10), safe(lambda b_=b_: b_.Position.Y * 10)) for b_ in items(sh.Balloons)]
        spots = [p for p in spots if p[0] is not None]
        crowded = sum(1 for i, a_ in enumerate(spots) for b_ in spots[i + 1:] if math.hypot(a_[0] - b_[0], a_[1] - b_[1]) < 11)
        if crowded:
            issues.append(f"[{sh.Name}] {crowded} pair(s) of balloons overlap")
        for n_ in items(sh.DrawingNotes.HoleThreadNotes):
            rb = safe(lambda n_=n_: n_.Text.RangeBox)
            if rb is None:
                continue
            nb = (rb.MinPoint.X * 10, rb.MinPoint.Y * 10, rb.MaxPoint.X * 10, rb.MaxPoint.Y * 10)
            for vn, vb in [(v.Name, _box(v)) for v in views]:
                if _overlap(nb, vb, 0.5):
                    issues.append(f"[{sh.Name}] hole note sits on view {vn}")
            if nb[2] > W - 12:
                issues.append(f"[{sh.Name}] hole note runs off the sheet")
        off = 0
        on_tb = 0
        for dim in items(sh.DrawingDimensions):
            o = safe(lambda dim=dim: dim.Text.Origin)
            if o is None:
                continue
            x, y = o.X * 10, o.Y * 10
            if x < 12 or y < 12 or x > W - 12 or y > H - 12:
                off += 1
            elif tbox and tbox[0] < x < tbox[2] and tbox[1] < y < tbox[3]:
                on_tb += 1
        if off:
            issues.append(f"[{sh.Name}] {off} dimension text(s) outside the border")
        texts = []
        for dim in items(sh.DrawingDimensions):
            box = _text_box(dim)
            if box:
                texts.append((safe(lambda dim=dim: dim.Text.Text, "?"), box))
        clash = [(a[0], b[0]) for i, a in enumerate(texts) for b in texts[i + 1:] if _overlap(a[1], b[1], 0.3)]
        if clash:
            issues.append(f"[{sh.Name}] overlapping dimension texts: " + ", ".join(f"{a} / {b}" for a, b in clash[:6]))
        for v in views:
            lb = safe(lambda v=v: v.Label.RangeBox)
            if lb is not None and any(_overlap(_mm_box(lb), t_[1], 0.3) for t_ in texts):
                issues.append(f"[{sh.Name}] the label of view {v.Name} sits on a dimension")
        for n_ in items(sh.DrawingNotes.HoleThreadNotes):
            nb_ = safe(lambda n_=n_: n_.Text.RangeBox)
            if nb_ is not None and any(_overlap(_mm_box(nb_), t_[1], 0.3) for t_ in texts):
                issues.append(f"[{sh.Name}] a hole note sits on a dimension")
        if on_tb:
            issues.append(f"[{sh.Name}] {on_tb} dimension text(s) on the title block")
        # dimensions per view (by attachment)
        per_view = {v.Name: 0 for v in views}
        for dim in items(sh.DrawingDimensions):
            vn = None
            for path in ("IntentOne.Geometry.Parent", "Intent.Geometry.Parent",
                         "IntentOne.Geometry.Parent.Parent", "Intent.Geometry.Parent.Parent"):
                obj = dim
                for attr in path.split("."):
                    obj = safe(lambda o=obj, a=attr: getattr(o, a))
                    if obj is None:
                        break
                name = safe(lambda o=obj: o.Name) if obj is not None else None
                if name in per_view:
                    vn = name
                    break
            if vn is None:      # retrieved model dimensions: attribute by where the text sits
                o = safe(lambda dim=dim: dim.Text.Origin)
                if o is not None:
                    x, y = o.X * 10, o.Y * 10
                    near = [(n, b) for n, b in boxes if b[0] - 30 <= x <= b[2] + 30 and b[1] - 30 <= y <= b[3] + 30]
                    if near:
                        vn = min(near, key=lambda nb: abs(x - (nb[1][0] + nb[1][2]) / 2) + abs(y - (nb[1][1] + nb[1][3]) / 2))[0]
            if vn in per_view:
                per_view[vn] += 1
        ortho = [v for v in views if "Iso" not in K.name_of(safe(lambda v=v: v.Camera.ViewOrientationType, 0), "k")]
        is_asm = any(s.kind(safe(lambda v=v: v.ReferencedDocumentDescriptor.ReferencedDocument)) == "assembly"
                     for v in views if safe(lambda v=v: v.ReferencedDocumentDescriptor.ReferencedDocument) is not None)
        if not is_asm and ortho and sum(per_view.values()) == 0:
            issues.append(f"[{sh.Name}] no dimensions on any view")
        if is_asm:
            if sh.PartsLists.Count == 0:
                issues.append(f"[{sh.Name}] assembly sheet without a parts list")
            elif sh.Balloons.Count == 0:
                issues.append(f"[{sh.Name}] parts list but no balloons")
            elif sh.Balloons.Count < sh.PartsLists.Item(1).PartsListRows.Count:
                issues.append(f"[{sh.Name}] {sh.PartsLists.Item(1).PartsListRows.Count - sh.Balloons.Count} "
                              f"parts-list items have no balloon")
        for v in views:
            if not safe(lambda v=v: v.ShowLabel, False):
                issues.append(f"[{sh.Name}] view {v.Name} has no label")
    for sh in items(d.Sheets):
        views = list(items(sh.DrawingViews))
        model = safe(lambda: views[0].ReferencedDocumentDescriptor.ReferencedDocument) if views else None
        if model is None or s.kind(model) != "part" or sh.DrawingDimensions.Count == 0:
            continue
        rb = safe(lambda: model.ComponentDefinition.RangeBox)
        if rb is None:
            continue
        all_values = _existing_values(sh)
        linear = _existing_values(sh, linear_only=True)
        sizes = {"X": (rb.MaxPoint.X - rb.MinPoint.X) * 10, "Y": (rb.MaxPoint.Y - rb.MinPoint.Y) * 10,
                 "Z": (rb.MaxPoint.Z - rb.MinPoint.Z) * 10}
        for axis, mm in sizes.items():
            round_outline = any(a != axis and abs(v - mm) < 0.05 for a, v in sizes.items())
            if not _have(all_values if round_outline else linear, mm, 0.05):
                issues.append(f"[{sh.Name}] overall size {mm:g} mm ({axis}) has no dimension")
    for key in ("title", "drawing_number", "drawn_by"):
        setname, prop = TB_FIELDS[key]
        if not safe(lambda: str(d.PropertySets.Item(setname).Item(prop).Value).strip(), ""):
            issues.append(f"title block field '{key}' is empty")
    report = "\n".join(f"- {i}" for i in issues) + f"\nTOTAL ISSUES: {len(issues)}"
    return {"issues": issues, "total": len(issues), "report": report}


@tool("drawing", mutates=False)
def render_sheet(s: Session, path: str = "", width: int = 2400) -> dict:
    """PNG of the active sheet (look at it before exporting)."""
    d = s.drawing()
    sh = d.ActiveSheet
    if not path:
        path = os.path.join(s.workdir, "renders", f"sheet-{int(time.time() * 1000)}.png")
    elif not os.path.isabs(path):
        path = os.path.join(s.workdir, "renders", path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    d.Activate()
    v = s.view()
    safe(lambda: v.Fit())
    h = int(width * sh.Height / sh.Width)
    v.SaveAsBitmap(path, int(width), h)
    return {"path": path, "sheet": sh.Name}


@tool("drawing", mutates=False)
def export_pdf(s: Session, path: str, all_sheets: bool = True) -> dict:
    """Export the drawing (all sheets by default) to PDF, vector, black lines."""
    d = s.drawing()
    if not os.path.isabs(path):
        path = os.path.join(s.workdir, "exports", path)
    if not path.lower().endswith(".pdf"):
        path += ".pdf"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    app = s.app
    tr = app.ApplicationAddIns.ItemById(PDF_TRANSLATOR)
    ctx = app.TransientObjects.CreateTranslationContext()
    ctx.Type = K.kFileBrowseIOMechanism
    opts = app.TransientObjects.CreateNameValueMap()
    medium = app.TransientObjects.CreateDataMedium()
    medium.FileName = path
    for key, val in (("All_Color_AS_Black", 1), ("Remove_Line_Weights", 0), ("Vector_Resolution", 400),
                     ("Sheet_Range", K.kPrintAllSheets if all_sheets else K.kPrintCurrentSheet)):
        safe(lambda: opts.Add(key, val))
    if os.path.exists(path):
        os.remove(path)
    tr.SaveCopyAs(d, ctx, opts, medium)
    if not os.path.exists(path):
        raise InventorError("PDF translator did not write the file")
    return {"path": path, "bytes": os.path.getsize(path), "sheets": d.Sheets.Count if all_sheets else 1}
