"""Triangle meshes for 3D-print exports, standard library only: read STL (binary or ASCII) and 3MF, write a
millimetre 3MF, and measure what a slicer will see (connected shells, open edges, extents, volume, bed contact)."""
from __future__ import annotations

import math
import re
import struct
import zipfile
from xml.sax.saxutils import escape

Tri = tuple  # ((x, y, z), (x, y, z), (x, y, z))


def read_stl(path: str) -> list[Tri]:
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * n == len(data):
            tris = []
            for i in range(n):
                v = struct.unpack_from("<12f", data, 84 + 50 * i)
                tris.append((tuple(v[3:6]), tuple(v[6:9]), tuple(v[9:12])))
            return tris
    text = data.decode("latin-1")
    nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    pts = [(float(a), float(b), float(c)) for a, b, c in nums]
    return [tuple(pts[i:i + 3]) for i in range(0, len(pts) - 2, 3)]


def write_3mf(path: str, tris: list[Tri], name: str = "part", unit: str = "millimeter") -> dict:
    """A plain 3MF (one object, one mesh) in the given unit. Shared vertices are merged, so shells stay closed."""
    verts: dict = {}
    faces = []
    for t in tris:
        ids = []
        for p in t:
            key = tuple(round(c, 6) for c in p)
            if key not in verts:
                verts[key] = len(verts)
            ids.append(verts[key])
        if len(set(ids)) == 3:
            faces.append(ids)
    vtxt = "".join(f'<vertex x="{x:.6f}" y="{y:.6f}" z="{z:.6f}"/>' for x, y, z in verts)
    ttxt = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             f'<model unit="{unit}" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<metadata name="Title">{escape(name)}</metadata>'
             f'<resources><object id="1" type="model" name="{escape(name)}"><mesh><vertices>{vtxt}</vertices>'
             f'<triangles>{ttxt}</triangles></mesh></object></resources>'
             '<build><item objectid="1"/></build></model>')
    types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
             '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
    return {"vertices": len(verts), "triangles": len(faces)}


UNIT_MM = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}


def read_3mf(path: str) -> tuple[list[Tri], str]:
    """Triangles (in millimetres) of every mesh in a 3MF, and the unit the file declares."""
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".model")), None)
        if name is None:
            raise ValueError("no 3D model part in the 3MF")
        xml = z.read(name).decode("utf-8", "replace")
    m = re.search(r'<model[^>]*\bunit="([^"]+)"', xml)
    unit = m.group(1) if m else "millimeter"
    k = UNIT_MM.get(unit, 1.0)
    tris = []
    for mesh in re.findall(r"<mesh>(.*?)</mesh>", xml, re.S):
        vs = [(float(a) * k, float(b) * k, float(c) * k) for a, b, c in
              re.findall(r'<vertex[^>]*\bx="([^"]+)"[^>]*\by="([^"]+)"[^>]*\bz="([^"]+)"', mesh)]
        for a, b, c in re.findall(r'<triangle[^>]*\bv1="(\d+)"[^>]*\bv2="(\d+)"[^>]*\bv3="(\d+)"', mesh):
            tris.append((vs[int(a)], vs[int(b)], vs[int(c)]))
    return tris, unit


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def stats(tris: list[Tri], bed_tol: float = 0.01) -> dict:
    """What a slicer sees: triangle count, connected shells, open (unpaired) edges, extents, volume, and the area
    lying flat on the bed (facing down at the lowest Z)."""
    if not tris:
        return {"triangles": 0, "shells": 0}
    index: dict = {}
    parent: list[int] = []

    def vid(p):
        key = (round(p[0], 4), round(p[1], 4), round(p[2], 4))
        if key not in index:
            index[key] = len(parent)
            parent.append(len(parent))
        return index[key]

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    edges: dict = {}
    vol = 0.0
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for t in tris:
        ids = [vid(p) for p in t]
        for a, b in ((ids[0], ids[1]), (ids[1], ids[2]), (ids[2], ids[0])):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
            key = (a, b) if a < b else (b, a)
            edges[key] = edges.get(key, 0) + 1
        for p in t:
            for i in range(3):
                lo[i] = min(lo[i], p[i])
                hi[i] = max(hi[i], p[i])
        a, b, c = t
        vol += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
                + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
    roots = {find(i) for i in range(len(parent))}
    zmin = lo[2]
    bed = 0.0
    for t in tris:
        if all(abs(p[2] - zmin) <= bed_tol for p in t):
            n = _cross(_sub(t[1], t[0]), _sub(t[2], t[0]))
            if n[2] < 0:
                bed += 0.5 * math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
    open_edges = sum(1 for v in edges.values() if v != 2)
    return {"triangles": len(tris), "vertices": len(parent), "shells": len(roots), "open_edges": open_edges,
            "watertight": open_edges == 0, "min_mm": [round(v, 4) for v in lo], "max_mm": [round(v, 4) for v in hi],
            "size_mm": [round(hi[i] - lo[i], 4) for i in range(3)], "volume_mm3": round(abs(vol), 3),
            "bed_contact_mm2": round(bed, 3)}


def scale_hint(mesh_size: list[float], model_size_mm: list[float]) -> float | None:
    """The factor that turns the mesh's numbers into millimetres, judged against the model's own size (1 when the
    file is already in mm, 10 for cm, 25.4 for inches). None if it can't be told."""
    pairs = [(m, s) for m, s in zip(mesh_size, model_size_mm) if m > 1e-9 and s > 1e-6]
    if not pairs:
        return None
    ratio = sum(s / m for m, s in pairs) / len(pairs)
    for k in (1.0, 10.0, 25.4, 0.1, 1000.0, 0.001):
        if abs(ratio - k) / k < 0.02:
            return k
    return None


def scaled(tris: list[Tri], k: float) -> list[Tri]:
    return [tuple(tuple(c * k for c in p) for p in t) for t in tris]


def write_stl(path: str, tris: list[Tri], header: str = "inventor-forge") -> None:
    """Binary STL."""
    with open(path, "wb") as f:
        f.write(header.encode("ascii", "replace")[:80].ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for t in tris:
            n = _cross(_sub(t[1], t[0]), _sub(t[2], t[0]))
            ln = math.sqrt(sum(c * c for c in n)) or 1.0
            f.write(struct.pack("<12fH", n[0] / ln, n[1] / ln, n[2] / ln, *t[0], *t[1], *t[2], 0))


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def point_triangle_distance(p, tri) -> float:
    """Distance from a point to a triangle (closest point on the triangle, edges and corners included)."""
    a, b, c = tri
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return math.dist(p, a)
    bp = _sub(p, b)
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return math.dist(p, b)
    vc = d1 * d4 - d3 * d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        t = d1 / (d1 - d3)
        return math.dist(p, (a[0] + t * ab[0], a[1] + t * ab[1], a[2] + t * ab[2]))
    cp = _sub(p, c)
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return math.dist(p, c)
    vb = d5 * d2 - d1 * d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        t = d2 / (d2 - d6)
        return math.dist(p, (a[0] + t * ac[0], a[1] + t * ac[1], a[2] + t * ac[2]))
    va = d3 * d6 - d5 * d4
    if va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
        t = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return math.dist(p, (b[0] + t * (c[0] - b[0]), b[1] + t * (c[1] - b[1]), b[2] + t * (c[2] - b[2])))
    den = 1.0 / (va + vb + vc)
    v, w = vb * den, vc * den
    return math.dist(p, (a[0] + ab[0] * v + ac[0] * w, a[1] + ab[1] * v + ac[1] * w, a[2] + ab[2] * v + ac[2] * w))


def mesh_gap(tris_a: list, tris_b: list) -> float:
    """Smallest distance between two triangle meshes: every vertex of each measured to the other's triangles
    (exact between flat faces; curved faces carry the meshes' chord error)."""
    best = math.inf
    for src, dst in ((tris_a, tris_b), (tris_b, tris_a)):
        boxes = [([min(q[i] for q in t) for i in range(3)], [max(q[i] for q in t) for i in range(3)], t) for t in dst]
        for p in {p for t in src for p in t}:
            for lo, hi, t in boxes:
                if math.sqrt(sum(max(lo[i] - p[i], 0, p[i] - hi[i]) ** 2 for i in range(3))) >= best:
                    continue
                best = min(best, point_triangle_distance(p, t))
    return best
