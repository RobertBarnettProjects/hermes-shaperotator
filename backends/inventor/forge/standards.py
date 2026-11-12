"""Drafting/engineering standards a chat can work to. One is chosen per session (the agent asks the user once at the
start) and every drawing, standard part note and material suggestion follows it from then on.

Modelling is always in millimetres. Standard parts are built from ISO metric data; for standards with their own
fastener series (AS, DIN, JIS, GB) those are the ISO-equivalent sizes, and inch fasteners (ASME B18) are not modelled.
"""
from __future__ import annotations


class StandardError(ValueError):
    pass


STANDARDS: dict[str, dict] = {
    "AS1100": {
        "name": "Australian Standards (AS 1100)", "region": "Australia / New Zealand",
        "drawing": "AS 1100.101 / AS 1100.201", "projection": "third", "template": "ISO.idw",
        "decimal": "point", "trailing_zeros": False, "sheets": ["A0", "A1", "A2", "A3", "A4"],
        "general_tolerance": "AS 1100.201 (state on the sheet)",
        "threads": "ISO metric coarse (AS 1275)",
        "fasteners": "AS 1110/1111 bolts and screws, AS 1112 nuts, AS 1237 washers (built to the ISO-equivalent sizes)",
        "materials": "AS/NZS 3678 Grade 250/350 plate, AS/NZS 3679.1 Grade 300 sections, AS/NZS 1163 C350L0 tube",
    },
    "ISO": {
        "name": "ISO (ISO 128 / ISO 5456)", "region": "International",
        "drawing": "ISO 128, ISO 5456-2, ISO 129-1", "projection": "first", "template": "ISO.idw",
        "decimal": "comma or point", "trailing_zeros": False, "sheets": ["A0", "A1", "A2", "A3", "A4"],
        "general_tolerance": "ISO 2768-m", "threads": "ISO metric coarse (ISO 261/262)",
        "fasteners": "ISO 4017 bolts, ISO 4762 socket screws, ISO 4032 nuts, ISO 7089 washers, ISO 2338 dowels",
        "materials": "EN 10025 S235/S275/S355, EN 10219 tube",
    },
    "ASME": {
        "name": "American (ASME Y14)", "region": "United States",
        "drawing": "ASME Y14.5-2018, Y14.3, Y14.100", "projection": "third", "template": "ANSI (mm).idw",
        "template_in": "ANSI (in).idw",
        "decimal": "point", "trailing_zeros": False, "sheets": ["A", "B", "C", "D", "E"],
        "general_tolerance": "title-block tolerances (ASME Y14.5)",
        "threads": "UNC/UNF (ASME B1.1) for inch work; ISO metric (ASME B1.13M) for metric work",
        "fasteners": "ASME B18.2.1/B18.2.2 (inch fasteners are not modelled: the ISO metric equivalents are built)",
        "materials": "ASTM A36 plate, ASTM A500 Gr B/C tube, ASTM A572 Gr 50",
    },
    "DIN": {
        "name": "German (DIN / DIN EN ISO)", "region": "Germany",
        "drawing": "DIN EN ISO 128, DIN ISO 5456", "projection": "first", "template": "DIN.idw",
        "decimal": "comma", "trailing_zeros": False, "sheets": ["A0", "A1", "A2", "A3", "A4"],
        "general_tolerance": "DIN ISO 2768-mK", "threads": "ISO metric coarse (DIN 13)",
        "fasteners": "DIN 933/931 (ISO 4017/4014) bolts, DIN 912 (ISO 4762), DIN 934 (ISO 4032) nuts, DIN 125 washers",
        "materials": "S235JR / S355J2 (EN 10025), C45, 42CrMo4",
    },
    "BS8888": {
        "name": "British (BS 8888)", "region": "United Kingdom",
        "drawing": "BS 8888 (ISO GPS)", "projection": "first", "template": "BSI.idw",
        "decimal": "point", "trailing_zeros": False, "sheets": ["A0", "A1", "A2", "A3", "A4"],
        "general_tolerance": "BS EN ISO 22081 / ISO 2768-m", "threads": "ISO metric coarse",
        "fasteners": "BS EN ISO 4017/4032/7089",
        "materials": "EN 10025 S275/S355, EN 10219 tube",
    },
    "JIS": {
        "name": "Japanese (JIS B 0001)", "region": "Japan",
        "drawing": "JIS B 0001, JIS Z 8315", "projection": "third", "template": "JIS.idw",
        "decimal": "point", "trailing_zeros": False, "sheets": ["A0", "A1", "A2", "A3", "A4"],
        "general_tolerance": "JIS B 0405-m", "threads": "ISO metric coarse (JIS B 0205)",
        "fasteners": "JIS B 1180/1181 (built to the ISO-equivalent sizes)",
        "materials": "SS400, S45C, STKR400 tube",
    },
    "GB": {
        "name": "Chinese (GB/T)", "region": "China",
        "drawing": "GB/T 4457, GB/T 14689, GB/T 14692", "projection": "first", "template": "GB.idw",
        "decimal": "point", "trailing_zeros": False, "sheets": ["A0", "A1", "A2", "A3", "A4"],
        "general_tolerance": "GB/T 1804-m", "threads": "ISO metric coarse (GB/T 193)",
        "fasteners": "GB/T 5783 bolts, GB/T 6170 nuts (built to the ISO-equivalent sizes)",
        "materials": "Q235B, Q355B, 45 steel",
    },
}
DEFAULT = "AS1100"

ALIASES = {"AS": "AS1100", "AUSTRALIAN": "AS1100", "ANSI": "ASME", "US": "ASME", "Y14": "ASME", "Y14.5": "ASME",
           "BS": "BS8888", "BSI": "BS8888", "UK": "BS8888", "EN": "ISO", "ISO128": "ISO", "DINISO": "DIN",
           "CHINESE": "GB", "GBT": "GB", "JAPANESE": "JIS", "GERMAN": "DIN"}


def key(standard: str) -> str:
    """Canonical key for a standard name ("AS 1100", "asme", "bs 8888"...)."""
    k = str(standard or "").upper().replace(" ", "").replace("-", "").replace("/", "").replace("_", "")
    k = ALIASES.get(k, k)
    if k not in STANDARDS:
        raise StandardError(f"unknown standard {standard!r}; choose one of {', '.join(STANDARDS)}")
    return k


def third_angle(standard: str) -> bool:
    return STANDARDS[key(standard)]["projection"] == "third"


def summary(k: str, units: str = "mm") -> dict:
    d = STANDARDS[k]
    return {"standard": k, **{f: d[f] for f in ("name", "region", "drawing", "projection", "general_tolerance",
                                                 "threads", "fasteners", "materials", "sheets")},
            "projection": f"{d['projection']}-angle", "drawing_units": units}
