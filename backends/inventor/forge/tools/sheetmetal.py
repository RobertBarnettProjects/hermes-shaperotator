"""Sheet metal: thickness, face, flange, cut, flat pattern and DXF of the flat pattern."""
from __future__ import annotations

import math
import os

from ..com import MM, InventorError, K, items, r, safe
from ..registry import tool
from ..session import Session


def _sm(s: Session):
    if not s.is_sheet_metal():
        raise InventorError("the active part is not sheet metal — create_part(sheet_metal=true)")
    return s.cdef()


def _fold(cd) -> None:
    """Make sure we're editing the folded model, not the flat pattern."""
    fp = safe(lambda: cd.FlatPattern)
    if fp is not None and safe(lambda: fp.Parent.Document.ActiveEditObject, None) is not None:
        safe(lambda: fp.ExitEdit())


@tool("sheet_metal")
def set_sheet_metal_thickness(s: Session, thickness: float, bend_radius: float = 0.0) -> dict:
    """Sheet thickness (mm) and optionally the default bend radius (mm) for this part."""
    cd = _sm(s)
    cd.UseSheetMetalStyleThickness = False
    cd.Thickness.Expression = f"{thickness} mm"
    if bend_radius:
        ps = cd.Parameters
        for name in ("BendRadius", "Bend Radius"):
            try:
                ps.Item(name).Expression = f"{bend_radius} mm"
                break
            except Exception:
                continue
    s.part().Update()
    return {"thickness_mm": r(cd.Thickness.Value * 10, 4),
            "bend_radius_mm": safe(lambda: r(cd.Parameters.Item("BendRadius").Value * 10, 4))}


@tool("sheet_metal")
def sheet_metal_face(s: Session, sketch_index: int = 0) -> dict:
    """Sheet-metal face (a plate of the sheet thickness) from the active sketch's closed profile."""
    cd = _sm(s)
    _fold(cd)
    sk = s.sketch(sketch_index)
    prof = sk.Profiles.AddForSolid()
    ff = cd.Features.FaceFeatures
    before = s.snapshot()
    feat = ff.Add(ff.CreateFaceFeatureDefinition(prof))
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("sheet_metal")
def flange(s: Session, edges: list[int], height: float = 20.0, angle: float = 90.0, flip: bool = False) -> dict:
    """Bend a flange up from one or more model edges (indices from list_edges / find_edge, pick the
    outer edges of the face).

    height: mm
    angle: bend angle in degrees
    flip: bend to the other side
    """
    cd = _sm(s)
    _fold(cd)
    fl = cd.Features.FlangeFeatures
    ec = s.edge_collection([s.edge(i) for i in edges])
    d = fl.CreateFlangeDefinition(ec, f"{angle} deg", f"{height} mm")
    if flip:
        d.SetDistanceHeightExtent(f"{height} mm", K.kNegativeExtentDirection, K.kHeightDatumOuter)
    before = s.snapshot()
    feat = fl.Add(d)
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("sheet_metal")
def sheet_metal_cut(s: Session, sketch_index: int = 0) -> dict:
    """Cut the active sketch's profile through the sheet (normal to the face it is sketched on)."""
    cd = _sm(s)
    _fold(cd)
    sk = s.sketch(sketch_index)
    prof = sk.Profiles.AddForSolid()
    cf = cd.Features.CutFeatures
    before = s.snapshot()
    feat = cf.Add(cf.CreateCutDefinition(prof))
    out = s.delta(feat.Name, before)
    out["feature"] = feat.Name
    return out


@tool("sheet_metal")
def flat_pattern(s: Session, dxf_path: str = "") -> dict:
    """Unfold to the flat pattern, report its blank size and bend count, optionally write a DXF of
    the flat pattern for laser/plasma cutting, then return to the folded model."""
    cd = _sm(s)
    if not cd.HasFlatPattern:
        cd.Unfold()
    else:
        cd.FlatPattern.Edit()
    fp = cd.FlatPattern
    out = {"length_mm": r(fp.Length * 10, 3), "width_mm": r(fp.Width * 10, 3),
           "bend_results": safe(lambda: fp.FlatBendResults.Count, None),
           "thickness_mm": r(cd.Thickness.Value * 10, 4)}
    if dxf_path:
        if not os.path.isabs(dxf_path):
            dxf_path = os.path.join(s.workdir, "exports", dxf_path)
        if not dxf_path.lower().endswith(".dxf"):
            dxf_path += ".dxf"
        os.makedirs(os.path.dirname(dxf_path), exist_ok=True)
        opts = "FLAT PATTERN DXF?AcadVersion=2010&OuterProfileLayer=OUTER&InteriorProfilesLayer=INNER" \
               "&BendUpLayer=BEND_UP&BendDownLayer=BEND_DOWN&InvisibleLayers=IV_TANGENT;IV_ARC_CENTERS"
        fp.DataIO.WriteDataToFile(opts, dxf_path)
        out["dxf"] = dxf_path
        out["dxf_bytes"] = os.path.getsize(dxf_path) if os.path.exists(dxf_path) else 0
    safe(lambda: fp.ExitEdit())
    return out
