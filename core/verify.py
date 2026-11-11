"""Claims checker: find engineering figures in a reply and check them against the evidence log.

A figure is VERIFIED when an analysis this session produced the same value (allowing for the
rounding the writer used), MISMATCHED when analyses of that quantity exist but none agrees, and
UNSUPPORTED when nothing was ever computed for it. Inputs the user gave (loads in N, dimensions)
are not claims and are ignored.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .evidence import EVIDENCE, Record

NUM = r"(\d+(?:[.,]\d+)?)"

# (quantity, regex). Group "v" is the value. Context words keep e.g. "M16" or "250 mm wide" out.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("safety_factor", re.compile(
        r"(?:safety\s+factor|factor\s+of\s+safety|\bFoS\b|\bFOS\b|\bSF\b)(?:\s+(?:on|against|vs\.?|to|for)\s+\w+)?"
        r"\s*(?:\(\w+\))?[\s|*:=≈~]*(?:of|is|was|about|around|approximately)?[\s|*:=≈~]*"
        r"(?:about|around|approximately)?\s*(?P<v>\d+(?:\.\d+)?)", re.I)),
    ("safety_factor", re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*(?:×|x)?\s*(?:safety\s+factor|factor\s+of\s+safety)", re.I)),
    ("stress_MPa", re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*(?:MPa|N/mm²|N/mm2)\b", re.I)),
    ("disp_mm", re.compile(
        r"(?:deflect\w*|displac\w*|deform\w*|sag\w*|moves?|movement)[^.\n]{0,40}?(?P<v>\d+(?:\.\d+)?)\s*mm\b", re.I)),
    ("disp_mm", re.compile(
        r"(?P<v>\d+(?:\.\d+)?)\s*mm\s+(?:of\s+)?(?:max(?:imum)?\s+)?(?:deflection|displacement|deformation|sag)", re.I)),
    ("mass_kg", re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*kg\b(?!\s*/)", re.I)),
]

# which evidence values can back each quantity
BACKING = {
    "stress_MPa": ("vm_max_MPa",),
    "safety_factor": ("safety_factor_yield",),
    "disp_mm": ("disp_max_mm",),
    "mass_kg": ("mass_kg",),
}

# words that make a figure a requirement/target/range rather than a result
REQUIREMENT_RX = re.compile(
    r"(?i)\b(?:requir\w*|target\w*|typical\w*|minimum|min\.|at least|no less than|needs?|should|must|"
    r"allowable|criteri\w*)\b|[≥>]=?|\d\s*(?:–|-|to)\s*\d")

YIELD_NEAR = re.compile(r"(?i)\byield|\bf_?y\b")
OPERATOR_AFTER = re.compile(r"\s*[/×*÷·]\s*\d")
OPERATOR_BEFORE = re.compile(r"\d\s*[/×*÷·]\s*$")
MODULUS_RX = re.compile(r"(?i)(?:\bE\s*[=:≈]\s*$|modulus[^.\n]{0,20}$|young[^.\n]{0,20}$)")

LABEL = {"stress_MPa": "stress", "safety_factor": "safety factor", "disp_mm": "deflection", "mass_kg": "mass"}


@dataclass
class Claim:
    quantity: str
    text: str
    value: float
    decimals: int
    status: str = "unsupported"   # verified | mismatch | unsupported
    record: Record | None = None
    closest: float | None = None
    key: str | None = None
    ctx: str = ""


def _tol(value: float, decimals: int, ref: float) -> float:
    return max(0.02 * abs(ref), 0.5 * 10 ** (-decimals) + 1e-12)


def _in_code(text: str, pos: int) -> bool:
    return text.count("```", 0, pos) % 2 == 1


def extract(text: str) -> list[Claim]:
    claims: list[Claim] = []
    seen: set[tuple[int, int]] = set()
    for qty, rx in PATTERNS:
        for m in rx.finditer(text):
            span = m.span("v")
            if span in seen or _in_code(text, span[0]):
                continue
            raw = m.group("v").replace(",", ".")
            seen.add(span)
            # operands of a hand calculation ("SF = 207/90", "2 × 45 MPa") are inputs, not results
            if OPERATOR_AFTER.match(text, span[1]) or OPERATOR_BEFORE.search(text[max(0, span[0] - 3):span[0]]):
                continue
            # a modulus quoted in MPa is a material property, not a computed stress
            if qty == "stress_MPa" and MODULUS_RX.search(text[max(0, span[0] - 30):span[0]]):
                continue
            dec = len(raw.split(".")[1]) if "." in raw else 0
            snippet = text[max(0, m.start() - 0):m.end()].strip()
            before = re.split(r"[.;:!?]\s|\n", text[max(0, m.start() - 40):m.start()])[-1]
            after = re.split(r"[.;!?]\s|\n", text[m.end():m.end() + 12])[0]
            ctx = before + text[m.start():m.end()] + after      # same sentence only
            claims.append(Claim(qty, snippet[:80], float(raw), dec, ctx=ctx))
    claims.sort(key=lambda c: text.find(c.text))
    return claims


def check(text: str, records: list[Record] | None = None) -> list[Claim]:
    records = EVIDENCE.items() if records is None else records
    claims = extract(text)
    for c in claims:
        keys = BACKING[c.quantity]
        if c.quantity == "stress_MPa" and YIELD_NEAR.search(c.ctx):
            keys = ("yield_MPa",)          # "yield strength 250 MPa" is the material, not a computed stress
        cands = [(r, k, r.values[k]) for r in records for k in keys if k in r.values]
        if not cands:
            continue
        best = min(cands, key=lambda t: abs(t[2] - c.value))
        r, k, ref = best
        if abs(ref - c.value) <= _tol(c.value, c.decimals, ref):
            c.status, c.record, c.closest, c.key = "verified", r, ref, k
        elif REQUIREMENT_RX.search(c.ctx):
            c.status = "requirement"     # "a minimum SF of 2", "typically 4–5": a target, not a result
        else:
            # only call it a mismatch against the quantity itself (not yield) — the writer may be
            # quoting an input such as an allowable stress
            own = [t for t in cands if t[1] != "yield_MPa"]
            if own:
                r2, _k2, ref2 = min(own, key=lambda t: abs(t[2] - c.value))
                c.status, c.record, c.closest, c.key = "mismatch", r2, ref2, _k2
    return claims


def _dedupe(claims: list[Claim]) -> list[Claim]:
    out, seen = [], set()
    for c in claims:
        k = (c.quantity, c.status, c.record.id if c.record else None, c.key,
             c.closest if c.status != "unsupported" else round(c.value, 6))
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def footer(claims: list[Claim]) -> str | None:
    # masses are often inputs (payloads); only report them when an analysis computed one
    claims = [c for c in claims if not (c.quantity == "mass_kg" and c.status == "unsupported")
              and c.status != "requirement"]
    claims = _dedupe(claims)
    if not claims:
        return None
    lines = ["", "---", "**hermes-shaperotator verification** — engineering figures in this reply, checked against "
             "this session's analysis log:"]
    warned: dict[str, Record] = {}
    for c in claims:
        name = "yield strength" if c.key == "yield_MPa" else LABEL[c.quantity]
        if c.status == "verified":
            has_warn = (bool(c.record.warnings) or not c.record.verified) and c.key != "yield_MPa"
            if has_warn:
                warned[c.record.id] = c.record
            mark = "⚠️" if has_warn else "✅"
            note = (f" — see {c.record.id} warnings" if c.record.warnings else f" — {c.record.id}'s checks did not pass") if has_warn else ""
            lines.append(f"- {mark} {name} **{c.value:g}** — matches {c.record.id} ({c.closest:g}){note}")
        elif c.status == "mismatch":
            lines.append(f"- ❌ {name} **{c.value:g}** — does not match the analysis: {c.record.id} gives {c.closest:g}")
        else:
            lines.append(f"- ❓ {name} **{c.value:g}** — not backed by any analysis this session")
    for rid, rec in warned.items():
        lines.append(f"{rid} warnings:")
        lines.extend(f"  - {w}" for w in rec.warnings)
    if any(c.status != "verified" for c in claims):
        lines.append("Treat ❌/❓ figures as unverified until an analysis backs them (cad_fea).")
    return "\n".join(lines)
