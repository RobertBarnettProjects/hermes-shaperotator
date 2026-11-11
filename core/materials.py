"""Linear-elastic material data for FEA. Yield values are specified MINIMUMS and depend on
thickness for plate grades — pass thickness_mm to get the right band.

Sources: AS/NZS 3678:2016 (plate), AS/NZS 3679.1:2016 (sections), AS/NZS 1163:2016 (hollow
sections), AS 4100 (E = 200 GPa for steel), AS/NZS 1664.1 (6061-T6), ASTM A240 (304).
This is a property table, not a code check.
"""
from __future__ import annotations

from typing import Any

STEEL = {"E_MPa": 200000.0, "nu": 0.3, "density_kg_m3": 7850.0}

# name -> (base props, [(max_thickness_mm, yield_MPa), ...] ascending; None = any thickness)
MATERIALS: dict[str, tuple[dict[str, float], list[tuple[float | None, float]]]] = {
    "AS/NZS 3678-250": (STEEL, [(8, 280), (12, 260), (50, 250), (80, 240), (150, 230)]),
    "AS/NZS 3678-350": (STEEL, [(12, 360), (20, 350), (80, 340), (150, 330)]),
    "AS/NZS 3679.1-300": (STEEL, [(11, 320), (17, 300), (None, 280)]),
    "AS/NZS 1163-C350L0": (STEEL, [(None, 350)]),
    "AS/NZS 1163-C450L0": (STEEL, [(None, 450)]),
    "Aluminium 6061-T6": ({"E_MPa": 68900.0, "nu": 0.33, "density_kg_m3": 2700.0}, [(None, 240)]),
    "Stainless 304": ({"E_MPa": 193000.0, "nu": 0.29, "density_kg_m3": 8000.0}, [(None, 205)]),
}

ALIASES = {
    "250": "AS/NZS 3678-250", "grade 250": "AS/NZS 3678-250", "3678-250": "AS/NZS 3678-250",
    "350": "AS/NZS 3678-350", "grade 350": "AS/NZS 3678-350", "3678-350": "AS/NZS 3678-350",
    "300": "AS/NZS 3679.1-300", "grade 300": "AS/NZS 3679.1-300",
    "c350": "AS/NZS 1163-C350L0", "c350l0": "AS/NZS 1163-C350L0", "c450": "AS/NZS 1163-C450L0",
    "6061": "Aluminium 6061-T6", "6061-t6": "Aluminium 6061-T6", "aluminium": "Aluminium 6061-T6",
    "aluminum": "Aluminium 6061-T6", "304": "Stainless 304", "stainless": "Stainless 304", "steel": "AS/NZS 3678-250",
}


def resolve(material: Any, thickness_mm: float | None = None) -> dict[str, Any]:
    """Material name / alias / explicit dict -> {name, E_MPa, nu, density_kg_m3, yield_MPa, note}."""
    if isinstance(material, dict):
        need = {"E_MPa", "nu", "density_kg_m3"}
        missing = need - set(material)
        if missing:
            raise ValueError(f"custom material needs {sorted(missing)}")
        out = {"name": material.get("name", "custom"), **{k: float(material[k]) for k in need}}
        out["yield_MPa"] = float(material["yield_MPa"]) if material.get("yield_MPa") else None
        out["note"] = "user-supplied properties"
        return out
    key = str(material or "AS/NZS 3678-250").strip()
    name = key if key in MATERIALS else ALIASES.get(key.lower())
    if name is None:
        for n in MATERIALS:
            if key.lower() in n.lower():
                name = n
                break
    if name is None:
        raise ValueError(f"unknown material {key!r}; known: {', '.join(MATERIALS)} (or pass E_MPa/nu/density_kg_m3)")
    props, bands = MATERIALS[name]
    fy, note = None, ""
    if thickness_mm is None:
        fy = min(y for _, y in bands)
        if len(bands) > 1:
            note = f"thickness not given: using the lowest yield band ({fy:g} MPa, conservative)"
    else:
        for tmax, y in bands:
            if tmax is None or thickness_mm <= tmax:
                fy = y
                break
        if fy is None:
            fy = bands[-1][1]
            note = f"thickness {thickness_mm:g} mm beyond the table; using {fy:g} MPa"
        else:
            note = f"yield for t = {thickness_mm:g} mm"
    return {"name": name, **props, "yield_MPa": float(fy), "note": note or "specified minimum yield"}
