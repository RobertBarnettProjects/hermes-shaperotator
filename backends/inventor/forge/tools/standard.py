"""Standard metric fasteners, modelled from the published ISO dimensions (nominal values, coarse thread)."""
from __future__ import annotations

import math
import os

from ..com import InventorError, r
from ..registry import tool
from ..session import Session
from .docs import create_part, save_document
from .features import chamfer, extrude, hole, thread
from .inspect import find_edge, list_faces, set_iproperties, set_material
from .sketch import create_sketch, draw_circle, draw_polygon

#         pitch, hex across flats s, hex head k, nut m, washer d1, d2, h, socket head dk, k, hex key, key depth
SIZES = {
    "M3": (0.5, 5.5, 2.0, 2.4, 3.2, 7.0, 0.5, 5.5, 3.0, 2.5, 1.3),
    "M4": (0.7, 7.0, 2.8, 3.2, 4.3, 9.0, 0.8, 7.0, 4.0, 3.0, 2.0),
    "M5": (0.8, 8.0, 3.5, 4.7, 5.3, 10.0, 1.0, 8.5, 5.0, 4.0, 2.5),
    "M6": (1.0, 10.0, 4.0, 5.2, 6.4, 12.0, 1.6, 10.0, 6.0, 5.0, 3.0),
    "M8": (1.25, 13.0, 5.3, 6.8, 8.4, 16.0, 1.6, 13.0, 8.0, 6.0, 4.0),
    "M10": (1.5, 16.0, 6.4, 8.4, 10.5, 20.0, 2.0, 16.0, 10.0, 8.0, 5.0),
    "M12": (1.75, 18.0, 7.5, 10.8, 13.0, 24.0, 2.5, 18.0, 12.0, 10.0, 6.0),
    "M16": (2.0, 24.0, 10.0, 14.8, 17.0, 30.0, 3.0, 24.0, 16.0, 14.0, 8.0),
    "M20": (2.5, 30.0, 12.5, 18.0, 21.0, 37.0, 3.0, 30.0, 20.0, 17.0, 10.0),
    "M24": (3.0, 36.0, 15.0, 21.5, 25.0, 44.0, 4.0, 36.0, 24.0, 19.0, 12.0),
}
KINDS = ("hex_bolt", "socket_screw", "hex_nut", "washer", "dowel")


def _hex_area(af: float) -> float:
    return math.sqrt(3) / 2 * af * af


def _thread_shank(s: Session, d: float, designation: str) -> None:
    cyl = [f for f in list_faces(s, type="cylinder")["faces"] if abs(f["diameter_mm"] - d) < 0.01 and not f.get("hole")]
    if cyl:
        thread(s, face_index=cyl[0]["index"], designation=designation)


@tool("parts")
def create_standard_part(s: Session, kind: str, size: str, length: float = 0.0, material: str = "Steel",
                         save_path: str = "") -> dict:
    """Model a standard metric part in a new document, with part number and description filled in: hex_bolt (ISO 4017),
    socket_screw (ISO 4762), hex_nut (ISO 4032), washer (ISO 7089, form A) or dowel (ISO 2338, nominal diameter).
    Threads are cosmetic. The head's underside (or the part's base) is on the XY plane, shanks point down -Z.

    size: M3 M4 M5 M6 M8 M10 M12 M16 M20 M24 (for a dowel, the nominal diameter such as 8 is also accepted)
    length: mm, shank length (bolts, screws, dowels)
    material: Inventor material name
    save_path: optional .ipt path; relative paths go under ~/Documents/inventor-forge
    """
    k = kind.lower()
    if k not in KINDS:
        raise InventorError(f"kind must be one of {KINDS}")
    key = size.upper() if size.upper().startswith("M") else f"M{float(size):g}"
    if k != "dowel" and key not in SIZES:
        raise InventorError(f"size must be one of {sorted(SIZES, key=lambda x: int(x[1:]))}")
    d = float(key[1:])
    if k in ("hex_bolt", "socket_screw", "dowel") and length <= 0:
        raise InventorError(f"{k} needs a length in mm")
    data = SIZES.get(key)
    label = f"{key}x{length:g}" if length else key
    create_part(s, name=f"{k.replace('_', ' ').title().replace(' ', '')}_{label}")
    if k == "hex_nut":
        p, af, _, m, *_ = data
        create_sketch(s, plane="XY")
        draw_polygon(s, sides=6, radius=af / 2, inscribed=False)
        extrude(s, distance=m)
        hole(s, x=0, y=0, tap=f"{key}x{p:g}")
        expected = None
        std, desc = "ISO 4032", f"Hex nut {key}"
    elif k == "washer":
        d1, d2, h = data[4], data[5], data[6]
        create_sketch(s, plane="XY")
        draw_circle(s, diameter=d2)
        draw_circle(s, diameter=d1)
        extrude(s, distance=h)
        expected = math.pi / 4 * (d2 ** 2 - d1 ** 2) * h
        std, desc = "ISO 7089", f"Plain washer {key}"
    elif k == "dowel":
        create_sketch(s, plane="XY")
        draw_circle(s, diameter=d)
        extrude(s, distance=length, direction="negative")
        c = max(0.2, round(0.12 * d, 2))
        for z in (0.0, -length):
            chamfer(s, distance=c, edges=[find_edge(s, z=z, type="circle")["edge"]])
        expected = None
        std, desc = "ISO 2338", f"Parallel pin Ø{d:g} x {length:g}"
    else:
        p = data[0]
        create_sketch(s, plane="XY")
        if k == "hex_bolt":
            af, head = data[1], data[2]
            draw_polygon(s, sides=6, radius=af / 2, inscribed=False)
            extrude(s, distance=head)
            head_v = _hex_area(af) * head
            std, desc = "ISO 4017", f"Hex head screw {label}"
        else:
            dk, head, key_af, key_depth = data[7], data[8], data[9], data[10]
            draw_circle(s, diameter=dk)
            extrude(s, distance=head)
            create_sketch(s, plane="XY", offset=head)
            draw_polygon(s, sides=6, radius=key_af / 2, inscribed=False)
            extrude(s, distance=key_depth, operation="cut")
            head_v = math.pi / 4 * dk ** 2 * head - _hex_area(key_af) * key_depth
            std, desc = "ISO 4762", f"Socket head cap screw {label}"
        create_sketch(s, plane="XY")
        draw_circle(s, diameter=d)
        extrude(s, distance=length, direction="negative")
        c = round(p, 2)
        chamfer(s, distance=c, edges=[find_edge(s, z=-length, type="circle")["edge"]])
        expected = head_v + math.pi / 4 * d ** 2 * length
        _thread_shank(s, d, f"{key}x{p:g}")
    mat = set_material(s, name=material)
    part_number = f"{std.replace(' ', '')}-{label}"
    set_iproperties(s, properties={"part_number": part_number, "description": desc, "stock_number": std})
    snap = s.snapshot()
    out = {"standard": std, "description": desc, "part_number": part_number, "volume_mm3": r(snap.volume, 2),
           "mass_kg": mat.get("mass_kg"), "material": mat.get("material")}
    if expected:
        out["expected_volume_mm3"] = r(expected, 1)
        if abs(snap.volume - expected) > 0.04 * expected:
            out["warning"] = "volume differs from the hand calculation by more than 4 %"
    if save_path:
        if not os.path.splitext(save_path)[1]:
            save_path += ".ipt"
        out["saved"] = save_document(s, path=save_path)["saved"]
    return out
