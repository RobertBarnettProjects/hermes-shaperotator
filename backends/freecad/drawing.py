"""Headless engineering drawings for the FreeCAD backend (no GUI, no TechDraw page objects).

Views are made by rotating the solid so the view looks along +Z, then TechDraw.projectEx (hidden-line
removal), so the 2-D coordinates of every view are known exactly. The sheet is drawn with matplotlib:
border, title block, projection symbol, visible/hidden/centre lines, overall dimensions (each once)
and hole callouts with position dimensions. A QA check reports what is missing before PDF export.

AS 1100 = third-angle (top view above the front view, right view to its right);
ISO = first-angle (top view below, right view to the left).
"""
from __future__ import annotations

import datetime as _dt
import math
import os

import FreeCAD as App
import Part

V = App.Vector
SHEETS = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841)}
SCALES = [(5, 1), (2, 1), (1, 1), (1, 2), (1, 5), (1, 10), (1, 20), (1, 50)]
TB_W, TB_H, BORDER = 180.0, 46.0, 10.0
DIM_GAP = 9.0          # first dimension line offset from the view (mm on paper)
DIM_STEP = 7.0         # spacing between stacked dimension lines
TEXT = 3.5             # text height mm


class DrawingError(Exception):
    pass


# --------------------------------------------------------------------------- view geometry
def _basis(name: str):
    """(u, v, w): paper right, paper up, towards the viewer — in world coordinates."""
    if name == "front":
        return V(1, 0, 0), V(0, 0, 1), V(0, -1, 0)
    if name == "top":
        return V(1, 0, 0), V(0, 1, 0), V(0, 0, 1)
    if name == "right":
        return V(0, 1, 0), V(0, 0, 1), V(1, 0, 0)
    if name == "left":
        return V(0, -1, 0), V(0, 0, 1), V(-1, 0, 0)
    if name == "iso":
        w = V(1, -1, 1)
        w.normalize()
        u = V(0, 0, 1).cross(w)
        u = V(-u.x, -u.y, -u.z) if u.x < 0 else u
        u.normalize()
        v = w.cross(u)
        return u, v, w
    raise DrawingError(f"unknown view {name!r}")


def _to_view(shape, name):
    u, v, w = _basis(name)
    m = App.Matrix(u.x, u.y, u.z, 0, v.x, v.y, v.z, 0, w.x, w.y, w.z, 0, 0, 0, 0, 1)
    return shape.transformGeometry(m)


def _polylines(compound, defl=0.05):
    out = []
    if compound is None:
        return out
    for e in compound.Edges:
        try:
            n = 2 if isinstance(e.Curve, Part.Line) else max(8, int(e.Length / 0.8))
            pts = e.discretize(Number=min(n, 400))
        except Exception:
            continue
        out.append([(p.x, p.y) for p in pts])
    return out


def project(shape, name):
    import TechDraw
    s = _to_view(shape, name)
    r = TechDraw.projectEx(s, V(0, 0, 1))
    visible = []
    for i in (0, 1, 3):              # sharp, smooth, outline
        visible += _polylines(r[i])
    hidden = []
    if name != "iso":
        for i in (5, 6, 8):
            hidden += _polylines(r[i])
    bb = s.BoundBox
    return {"visible": visible, "hidden": hidden, "bbox": (bb.XMin, bb.YMin, bb.XMax, bb.YMax), "shape": s}


def holes(shape):
    """Cylindrical holes (concave cylinders): axis, centre, diameter, depth, through?"""
    out = []
    bb = shape.BoundBox
    for f in shape.Faces:
        srf = f.Surface
        if type(srf).__name__ != "Cylinder":
            continue
        # concave = material outside the cylinder → a hole (normal points towards the axis)
        u0, u1, v0, v1 = f.ParameterRange
        p = f.valueAt((u0 + u1) / 2, (v0 + v1) / 2)
        n = f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
        axis = srf.Axis
        centre = srf.Center
        radial = p - centre
        radial = radial - axis * radial.dot(axis)
        if radial.Length < 1e-9 or n.dot(radial) > 0:
            continue                    # convex (a boss or a fillet), not a hole
        ax = V(axis)
        ax.normalize()
        ext = [v.Point.dot(ax) for v in f.Vertexes] or [0]
        depth = (max(ext) - min(ext)) if len(ext) > 1 else f.Area / (2 * math.pi * srf.Radius)
        # through = both ends of the bore open to air (probe on the axis just past each end)
        mid_t = (max(ext) + min(ext)) / 2 if len(ext) > 1 else centre.dot(ax)
        mid = centre + ax * (mid_t - centre.dot(ax))
        ends = [mid + ax * (s * (depth / 2 + 0.3)) for s in (-1, 1)]
        try:
            through = not any(shape.isInside(p, 1e-6, True) for p in ends)
        except Exception:
            through = False
        out.append({"diameter": round(2 * srf.Radius, 3), "axis": (ax.x, ax.y, ax.z),
                    "centre": (mid.x, mid.y, mid.z), "depth": round(depth, 2), "through": through})
    # merge duplicate faces of one hole (split cylinders)
    merged = []
    for h in out:
        if any(abs(h["diameter"] - m["diameter"]) < 1e-3 and _same_axis_line(h, m) for m in merged):
            continue
        merged.append(h)
    return merged


def _same_axis_line(a, b):
    ax = V(*a["axis"])
    d = V(*a["centre"]) - V(*b["centre"])
    return abs(abs(ax.dot(V(*b["axis"]))) - 1) < 1e-6 and (d - ax * d.dot(ax)).Length < 1e-3


# --------------------------------------------------------------------------- sheet layout
def _choose_scale(sizes, area_w, area_h):
    for num, den in SCALES:
        s = num / den
        w = sizes["w"] * s + sizes["w_pad"]
        h = sizes["h"] * s + sizes["h_pad"]
        if w <= area_w and h <= area_h:
            return num, den
    return SCALES[-1]


class Sheet:
    def __init__(self, standard="AS1100", sheet_size="A3", orientation="landscape", properties=None):
        if sheet_size not in SHEETS:
            raise DrawingError(f"sheet_size must be one of {list(SHEETS)}")
        w, h = SHEETS[sheet_size]
        if orientation == "portrait":
            w, h = h, w
        self.W, self.H = w, h
        self.size = sheet_size
        self.standard = "ISO" if str(standard).upper().startswith("ISO") else "AS1100"
        self.props = dict(properties or {})
        self.views = {}
        self.dims = []          # dicts: kind, view, p1, p2, offset_dir, text
        self.notes = []         # hole callouts
        self.centre_marks = []
        self.scale = (1, 1)
        self.part = None

    # ------------------------------------------------------------------ build
    def part_sheet(self, shape, views=("front", "top", "right", "iso"), scale=0, title=""):
        if shape is None or shape.isNull() or not shape.Solids:
            raise DrawingError("no solid to draw — build or open a part first")
        self.part = shape
        names = [v.strip().lower() for v in views if v.strip()]
        proj = {n: project(shape, n) for n in names}
        bb = shape.BoundBox
        L, Wd, Hh = bb.XLength, bb.YLength, bb.ZLength
        side = "right" if "right" in proj else ("left" if "left" in proj else None)
        area_w = self.W - 2 * BORDER - 10
        area_h = self.H - 2 * BORDER - TB_H - 10
        sizes = {"w": L + (Wd if side else 0) + (0 if not proj.get("iso") else 0),
                 "h": Hh + (Wd if "top" in proj else 0),
                 "w_pad": 2 * (DIM_GAP + 3 * DIM_STEP) + 25, "h_pad": 2 * (DIM_GAP + 3 * DIM_STEP) + 25}
        if scale:
            s = float(scale)
            num, den = (s, 1) if s >= 1 else (1, round(1 / s))
        else:
            num, den = _choose_scale(sizes, area_w, area_h)
        self.scale = (num, den)
        k = num / den
        gap = DIM_GAP + 3 * DIM_STEP + 8
        # front view origin (paper mm, lower-left of the view's bbox)
        x0 = BORDER + 12 + DIM_GAP + 2 * DIM_STEP
        y0 = BORDER + TB_H + 12 + DIM_GAP + 2 * DIM_STEP
        third = self.standard == "AS1100"
        pos = {}
        if third:
            pos["front"] = (x0, y0)
            if "top" in proj:
                pos["top"] = (x0, y0 + Hh * k + gap)
            if side:
                pos[side] = (x0 + L * k + gap, y0)
        else:   # first angle: top view below the front view, right view on the left
            top_h = Wd * k + gap if "top" in proj else 0
            side_w = Wd * k + gap if side else 0
            pos["front"] = (x0 + side_w, y0 + top_h)
            if "top" in proj:
                pos["top"] = (x0 + side_w, y0)
            if side:
                pos[side] = (x0, y0 + top_h)
        if "iso" in proj:
            ib = proj["iso"]["bbox"]
            iw, ih = (ib[2] - ib[0]) * k, (ib[3] - ib[1]) * k
            pos["iso"] = (self.W - BORDER - 12 - iw, self.H - BORDER - 12 - ih)
        for n, p in proj.items():
            if n not in pos:
                continue
            self.views[n] = {**p, "origin": pos[n]}
        self._dimension(L, Wd, Hh, side)
        self.props.setdefault("title", title or "PART")
        return self.summary()

    def _paper(self, view, u, v):
        vw = self.views[view]
        k = self.scale[0] / self.scale[1]
        bx, by = vw["bbox"][0], vw["bbox"][1]
        return vw["origin"][0] + (u - bx) * k, vw["origin"][1] + (v - by) * k

    def _view_box(self, view):
        vw = self.views[view]
        a = self._paper(view, vw["bbox"][0], vw["bbox"][1])
        b = self._paper(view, vw["bbox"][2], vw["bbox"][3])
        return a[0], a[1], b[0], b[1]

    def _dimension(self, L, Wd, Hh, side):
        self.dims, self.notes, self.centre_marks = [], [], []
        if "front" in self.views:
            x1, y1, x2, y2 = self._view_box("front")
            self.dims.append({"kind": "h", "view": "front", "a": (x1, y1), "b": (x2, y1), "level": 0,
                              "side": -1, "value": L, "what": "overall length"})
            self.dims.append({"kind": "v", "view": "front", "a": (x1, y1), "b": (x1, y2), "level": 0,
                              "side": -1, "value": Hh, "what": "overall height"})
        width_view = "top" if "top" in self.views else side
        if width_view:
            x1, y1, x2, y2 = self._view_box(width_view)
            if width_view == "top":
                self.dims.append({"kind": "v", "view": "top", "a": (x1, y1), "b": (x1, y2), "level": 0,
                                  "side": -1, "value": Wd, "what": "overall width"})
            else:
                self.dims.append({"kind": "h", "view": side, "a": (x1, y1), "b": (x2, y1), "level": 0,
                                  "side": -1, "value": Wd, "what": "overall width"})
        # holes: show where they appear as circles
        groups = {}
        for h in holes(self.part):
            for n in ("top", "front", side):
                if not n or n not in self.views:
                    continue
                u, v, w = _basis(n)
                ax = V(*h["axis"])
                if abs(abs(ax.dot(w)) - 1) > 1e-6:
                    continue
                c = V(*h["centre"])
                cu, cv = c.dot(u), c.dot(v)
                key = (n, h["diameter"], h["through"], h["depth"])
                groups.setdefault(key, []).append((cu, cv))
                break
        level_h, level_v = {}, {}
        for (n, dia, thru, depth), centres in groups.items():
            pc = [self._paper(n, cu, cv) for cu, cv in centres]
            r_paper = dia / 2 * self.scale[0] / self.scale[1]
            for (px, py) in pc:
                self.centre_marks.append({"view": n, "x": px, "y": py, "r": r_paper})
            count = f"{len(centres)}× " if len(centres) > 1 else ""
            label = f"{count}Ø{dia:g} {'THRU' if thru else f'× {depth:g} DEEP'}"
            self.notes.append({"view": n, "x": pc[0][0], "y": pc[0][1], "r": r_paper, "text": label,
                               "diameter": dia, "count": len(centres)})
            # position dimensions from the view's lower-left corner (datum edges)
            x1, y1, x2, y2 = self._view_box(n)
            for (px, py), (cu, cv) in zip(pc, centres):
                bx, by = self.views[n]["bbox"][0], self.views[n]["bbox"][1]
                for kind, a, b, val, lev in (("h", (x1, y2), (px, y2), cu - bx, level_h),
                                             ("v", (x2, y1), (x2, py), cv - by, level_v)):
                    # holes on a common centre line share one position dimension
                    if any(d["view"] == n and d["kind"] == kind and d["what"].startswith("hole")
                           and abs(d["value"] - val) < 1e-6 for d in self.dims):
                        continue
                    lv_ = lev.get(n, 1)
                    self.dims.append({"kind": kind, "view": n, "a": a, "b": b, "level": lv_, "side": 1,
                                      "value": val, "what": f"hole Ø{dia:g} position"})
                    lev[n] = lv_ + 1
        self._feature_dims(level_h, level_v)

    def _feature_dims(self, level_h, level_v):
        """Baseline dimensions for every intermediate edge level (steps, plate thicknesses) that no
        other dimension covers yet — each size once, in the first view that shows it as an edge."""
        def world_axis(vec):
            return "xyz"[max(range(3), key=lambda i: abs((vec.x, vec.y, vec.z)[i]))]

        done = {"x": set(), "y": set(), "z": set()}
        bb = self.part.BoundBox
        lo = {"x": bb.XMin, "y": bb.YMin, "z": bb.ZMin}
        hi = {"x": bb.XMax, "y": bb.YMax, "z": bb.ZMax}
        for ax_ in "xyz":
            done[ax_].update({0.0, round(hi[ax_] - lo[ax_], 3)})
        self.levels = {"x": set(), "y": set(), "z": set()}
        for n in ("front", "top", "right", "left"):
            if n not in self.views:
                continue
            u, v, _w = _basis(n)
            au, av = world_axis(u), world_axis(v)
            vw = self.views[n]
            bx0, by0, bx1, by1 = vw["bbox"]
            us, vs = set(), set()
            for seg in vw["visible"]:
                if len(seg) < 2:
                    continue
                (u1, v1), (u2, v2) = seg[0], seg[-1]
                if any(abs(p[0] - u1) > 1e-6 for p in seg) and any(abs(p[1] - v1) > 1e-6 for p in seg):
                    continue        # not an axis-aligned straight edge
                if abs(u1 - u2) < 1e-6 and abs(v1 - v2) > 1e-3:
                    us.add(round(u1 - bx0, 3))
                elif abs(v1 - v2) < 1e-6 and abs(u1 - u2) > 1e-3:
                    vs.add(round(v1 - by0, 3))
            x1, y1, x2, y2 = self._view_box(n)
            k = self.scale[0] / self.scale[1]
            for val in sorted(us):
                self.levels[au].add(val)
                if any(abs(val - d) < 1e-3 for d in done[au]):
                    continue
                lv_ = level_h.get(n, 1)
                self.dims.append({"kind": "h", "view": n, "a": (x1, y2), "b": (x1 + val * k, y2), "level": lv_,
                                  "side": 1, "value": val, "what": f"feature {au}"})
                level_h[n] = lv_ + 1
                done[au].add(val)
            for val in sorted(vs):
                self.levels[av].add(val)
                if any(abs(val - d) < 1e-3 for d in done[av]):
                    continue
                lv_ = level_v.get(n, 1)
                self.dims.append({"kind": "v", "view": n, "a": (x2, y1), "b": (x2, y1 + val * k), "level": lv_,
                                  "side": 1, "value": val, "what": f"feature {av}"})
                level_v[n] = lv_ + 1
                done[av].add(val)
        self.dimensioned = done

    def extent(self, n):
        """Paper box of a view including its dimension lines, texts, label and hole callouts."""
        x1, y1, x2, y2 = self._view_box(n)
        for d in self.dims:
            if d["view"] != n:
                continue
            off = DIM_GAP + d["level"] * DIM_STEP
            if d["kind"] == "h":
                y = d["a"][1] + d["side"] * off
                y1, y2 = min(y1, y - 1), max(y2, y + TEXT + 2)
            else:
                x = d["a"][0] + d["side"] * off
                x1, x2 = min(x1, x - TEXT - 2), max(x2, x + 1)
        for note in self.notes:
            if note["view"] == n:
                tx = note["x"] + note["r"] * 0.71 + 10
                ty = note["y"] + note["r"] * 0.71 + 10
                x2, y2 = max(x2, tx + 0.62 * TEXT * len(note["text"])), max(y2, ty + TEXT + 1)
        y1 -= TEXT + 4          # view label
        return x1, y1, x2, y2

    def summary(self):
        return {"standard": self.standard, "projection": "third angle" if self.standard == "AS1100" else "first angle",
                "sheet": f"{self.size} {self.W:g}x{self.H:g}", "scale": f"{self.scale[0]:g}:{self.scale[1]:g}",
                "views": list(self.views), "dimensions": [f"{d['what']}: {d['value']:.1f}" for d in self.dims],
                "hole_callouts": [n["text"] for n in self.notes]}

    # ------------------------------------------------------------------ QA
    def check(self):
        issues = []
        if not self.views:
            issues.append("no views on the sheet")
        boxes = {n: self.extent(n) for n in self.views}
        for n, (x1, y1, x2, y2) in boxes.items():
            if x1 < BORDER or y1 < BORDER or x2 > self.W - BORDER or y2 > self.H - BORDER:
                issues.append(f"{n} view (with dimensions) runs off the drawing border — use a smaller scale")
            tb = (self.W - BORDER - TB_W, BORDER, self.W - BORDER, BORDER + TB_H)
            if x2 > tb[0] and x1 < tb[2] and y2 > tb[1] and y1 < tb[3]:
                issues.append(f"{n} view overlaps the title block")
        names = list(boxes)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                A, B = boxes[a], boxes[b]
                if A[2] > B[0] and A[0] < B[2] and A[3] > B[1] and A[1] < B[3]:
                    issues.append(f"{a} and {b} views (with their dimensions) overlap")
        whats = {d["what"] for d in self.dims}
        for need in ("overall length", "overall height", "overall width"):
            if need not in whats:
                issues.append(f"missing {need} dimension")
        if self.part is not None:
            n_holes = len(holes(self.part))
            called = sum(n["count"] for n in self.notes)
            if called < n_holes:
                issues.append(f"{n_holes - called} hole(s) have no callout (their axis is not normal to a drawn "
                              "view — add the view that shows them as circles)")
        for f in ("title", "drawing_number", "material", "drawn_by"):
            if not str(self.props.get(f) or "").strip():
                issues.append(f"title block field '{f}' is empty (set it via properties)")
        report = "\n".join(f"- {i}" for i in issues) or "- no issues"
        return {"issues": len(issues), "items": issues,
                "report": f"Drawing QA ({self.standard}, scale {self.scale[0]:g}:{self.scale[1]:g})\n{report}\n"
                          f"TOTAL ISSUES: {len(issues)}"}

    # ------------------------------------------------------------------ render
    def render(self, path):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Circle, Polygon, Rectangle

        mm = 1 / 25.4
        fig = plt.figure(figsize=(self.W * mm, self.H * mm))
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(0, self.W)
        ax.set_ylim(0, self.H)
        ax.set_aspect("equal")
        ax.axis("off")
        pt = 72 / 25.4            # points per mm
        thick, thin = 0.5 * pt, 0.25 * pt
        fs = TEXT * pt / 1.0 * 0.72
        ax.add_patch(Rectangle((BORDER, BORDER), self.W - 2 * BORDER, self.H - 2 * BORDER, fill=False, lw=0.7 * pt))
        k = self.scale[0] / self.scale[1]
        for n, vw in self.views.items():
            for seg in vw["hidden"]:
                xs, ys = zip(*[self._paper(n, u, v) for u, v in seg])
                ax.plot(xs, ys, color="black", lw=thin, dashes=(3, 1.5))
            for seg in vw["visible"]:
                xs, ys = zip(*[self._paper(n, u, v) for u, v in seg])
                ax.plot(xs, ys, color="black", lw=thick, solid_capstyle="round")
            x1, y1, x2, y2 = self._view_box(n)
            label = {"front": "FRONT VIEW", "top": "PLAN", "right": "RIGHT VIEW", "left": "LEFT VIEW",
                     "iso": "ISOMETRIC"}[n]
            ey = self.extent(n)[1]
            ax.text((x1 + x2) / 2, ey + TEXT + 3, label, ha="center", va="top", fontsize=fs, fontweight="bold")
        for c in self.centre_marks:
            e = c["r"] + 2.5
            for (xa, ya, xb, yb) in ((c["x"] - e, c["y"], c["x"] + e, c["y"]), (c["x"], c["y"] - e, c["x"], c["y"] + e)):
                ax.plot([xa, xb], [ya, yb], color="black", lw=thin * 0.8, dashes=(6, 1.5, 1, 1.5))
        for d in self.dims:
            self._draw_dim(ax, d, thin, fs)
        for note in self.notes:
            ang = math.radians(45)
            sx = note["x"] + note["r"] * math.cos(ang)
            sy = note["y"] + note["r"] * math.sin(ang)
            tx, ty = sx + 10, sy + 10
            ax.annotate(note["text"], xy=(sx, sy), xytext=(tx, ty), fontsize=fs, va="bottom",
                        arrowprops=dict(arrowstyle="-|>", lw=thin, color="black", shrinkA=0, shrinkB=0,
                                        mutation_scale=8))
        self._title_block(ax, fs, thin)
        ext = os.path.splitext(path)[1].lower()
        fig.savefig(path, dpi=200 if ext == ".png" else None, format=ext.lstrip(".") or "pdf")
        plt.close(fig)
        return path

    def _draw_dim(self, ax, d, lw, fs):
        off = DIM_GAP + d["level"] * DIM_STEP
        (ax1, ay1), (bx1, by1) = d["a"], d["b"]
        txt = f"{d['value']:.1f}".rstrip("0").rstrip(".")
        if d["kind"] == "h":
            y = ay1 + d["side"] * off
            for x, yy in ((ax1, ay1), (bx1, by1)):
                ax.plot([x, x], [yy + d["side"] * 1.5, y + d["side"] * 2], color="black", lw=lw * 0.8)
            ax.annotate("", xy=(ax1, y), xytext=(bx1, y),
                        arrowprops=dict(arrowstyle="<|-|>", lw=lw, color="black", shrinkA=0, shrinkB=0,
                                        mutation_scale=7))
            ax.text((ax1 + bx1) / 2, y + 0.8, txt, ha="center", va="bottom", fontsize=fs)
        else:
            x = ax1 + d["side"] * off
            for xx, yy in ((ax1, ay1), (bx1, by1)):
                ax.plot([xx + d["side"] * 1.5, x + d["side"] * 2], [yy, yy], color="black", lw=lw * 0.8)
            ax.annotate("", xy=(x, ay1), xytext=(x, by1),
                        arrowprops=dict(arrowstyle="<|-|>", lw=lw, color="black", shrinkA=0, shrinkB=0,
                                        mutation_scale=7))
            ax.text(x - 0.8, (ay1 + by1) / 2, txt, ha="right", va="center", rotation=90, fontsize=fs)

    def _title_block(self, ax, fs, lw):
        from matplotlib.patches import Circle, Polygon, Rectangle
        x0, y0 = self.W - BORDER - TB_W, BORDER
        ax.add_patch(Rectangle((x0, y0), TB_W, TB_H, fill=False, lw=lw * 2))
        p = self.props
        rows = [
            ("TITLE", str(p.get("title", "")).upper(), 2),
            ("DRAWING No.", p.get("drawing_number", ""), 1), ("SHEET", f"{p.get('sheet', '1 OF 1')}", 1),
            ("MATERIAL", p.get("material", ""), 1), ("SCALE", f"{self.scale[0]:g}:{self.scale[1]:g}", 1),
            ("DRAWN", p.get("drawn_by", ""), 1), ("DATE", p.get("date", _dt.date.today().isoformat()), 1),
            ("UNITS", "mm", 1), ("STANDARD", "AS 1100" if self.standard == "AS1100" else "ISO 128", 1),
        ]
        # layout: title across the top, then a 2-column grid
        h_title = 14
        ax.plot([x0, x0 + TB_W], [y0 + TB_H - h_title] * 2, color="black", lw=lw)
        ax.text(x0 + 2, y0 + TB_H - 3, "TITLE", fontsize=fs * 0.6, va="top")
        ax.text(x0 + 2, y0 + TB_H - h_title / 2 - 1, rows[0][1], fontsize=fs * 1.3, va="center", fontweight="bold")
        cell_w, cell_h = (TB_W - 30) / 2, (TB_H - h_title) / 4
        for i, (lab, val, _) in enumerate(rows[1:]):
            col, row = i % 2, i // 2
            cx, cy = x0 + col * cell_w, y0 + TB_H - h_title - (row + 1) * cell_h
            ax.add_patch(Rectangle((cx, cy), cell_w, cell_h, fill=False, lw=lw))
            ax.text(cx + 1.5, cy + cell_h - 1, lab, fontsize=fs * 0.55, va="top")
            ax.text(cx + 1.5, cy + 1.5, str(val), fontsize=fs * 0.85, va="bottom")
        # projection symbol box
        sx, sy = x0 + TB_W - 30, y0
        ax.add_patch(Rectangle((sx, sy), 30, TB_H - h_title, fill=False, lw=lw))
        cx, cy = sx + 15, sy + (TB_H - h_title) / 2
        third = self.standard == "AS1100"
        # the cone's narrow end always faces the end view (circles); third angle: cone left, circles right
        cone_x = cx - 9 if third else cx + 3
        circ_x = cx + 7 if third else cx - 7
        ax.add_patch(Polygon([(cone_x, cy - 6.5), (cone_x + 6, cy - 4), (cone_x + 6, cy + 4), (cone_x, cy + 6.5)]
                             if third else
                             [(cone_x, cy - 4), (cone_x + 6, cy - 6.5), (cone_x + 6, cy + 6.5), (cone_x, cy + 4)],
                             fill=False, lw=lw))
        ax.add_patch(Circle((circ_x, cy), 6.5, fill=False, lw=lw))
        ax.add_patch(Circle((circ_x, cy), 4, fill=False, lw=lw))
        ax.text(cx, sy + 1.5, "THIRD ANGLE" if third else "FIRST ANGLE", fontsize=fs * 0.5, ha="center")
        ax.text(BORDER + 2, BORDER + 2, "Generated by hermes-shaperotator — check before use. Do not scale.",
                fontsize=fs * 0.55, va="bottom")
