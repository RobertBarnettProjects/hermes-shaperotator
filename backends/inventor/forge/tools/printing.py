"""3D-print deliverables and multi-body parts: translator capabilities, an atomic print package (native file, STL,
3MF, STEP) with mesh checks, bodies with stable names, body-to-body clearance, and display poses that never change the
saved build pose."""
from __future__ import annotations

import math
import os
import shutil
import tempfile
import time

from .. import meshio
from ..com import MM, InventorError, K, items, r, safe
from ..registry import tool
from ..session import Session, body_volume

STL_TRANSLATOR = "{533E9A98-FC3B-11D4-8E7E-0010B541CD80}"
RESOLUTION = {"fine": 0, "medium": 1, "coarse": 2}


# --------------------------------------------------------------------------- translators
def _translators(app) -> list:
    out = []
    for a in items(app.ApplicationAddIns):
        if safe(lambda a=a: a.AddInType) != K.get("kTranslationApplicationAddIn", -1) and \
                safe(lambda a=a: a.FileExtensions) is None:
            continue
        out.append(a)
    return out


def _translator_for(app, ext: str):
    ext = ext.lower().lstrip(".")
    for a in _translators(app):
        exts = str(safe(lambda a=a: a.FileExtensions, "") or "").lower()
        if f"*.{ext}" in exts or f".{ext}" in exts.split(";") or f".{ext};" in exts or exts.endswith(f".{ext}"):
            if safe(lambda a=a: a.SupportsSaveCopyAs, False):
                return a
    return None


@tool("documents", mutates=False)
def export_capabilities(s: Session, extension: str = "") -> dict:
    """The file formats this Inventor can export to: each translator's name, extensions, whether it is loaded and
    licensed, and which document types it saves from. With extension, just that one, with its default options.

    extension: e.g. "3mf", "stl", "stp"
    """
    app = s.app
    rows = []
    for a in _translators(app):
        if not safe(lambda a=a: a.SupportsSaveCopyAs, False):
            continue
        rows.append({"name": safe(lambda a=a: a.DisplayName, "?"), "extensions": safe(lambda a=a: a.FileExtensions, ""),
                     "loaded": bool(safe(lambda a=a: a.Activated, False)),
                     "licence": K.name_of(safe(lambda a=a: a.LicenseStatus, 0), "k"),
                     "saves_from": safe(lambda a=a: a.SupportsSaveCopyAsFrom, ""),
                     "id": safe(lambda a=a: a.ClassIdString, "")})
    if not extension:
        return {"translators": rows, "count": len(rows)}
    tr = _translator_for(app, extension)
    if tr is None:
        return {"extension": extension, "available": False,
                "note": f"no translator in this Inventor saves .{extension.lstrip('.')}"}
    return {"extension": extension, "available": True, "name": safe(lambda: tr.DisplayName, "?"),
            "loaded": bool(safe(lambda: tr.Activated, False)),
            "saves_from": safe(lambda: tr.SupportsSaveCopyAsFrom, ""),
            "default_options": _default_options(s, tr, s.doc())}


def _default_options(s: Session, tr, d) -> dict:
    """The options a translator would use for this document (its own defaults), as plain values."""
    try:
        if not safe(lambda: tr.Activated, True):
            tr.Activate()
        ctx = s.to.CreateTranslationContext()
        ctx.Type = K.kFileBrowseIOMechanism
        opts = s.to.CreateNameValueMap()
        has = tr.HasSaveCopyAsOptions(d, ctx, opts)
        if not has:
            return {}
        return {opts.Name(i): safe(lambda i=i: opts.Value(opts.Name(i)), None) for i in range(1, opts.Count + 1)}
    except Exception:  # noqa: BLE001
        return {}


def _stl_export(s: Session, d, path: str, resolution: str) -> dict:
    """STL through the translator with an explicit resolution; returns the options used."""
    from .docs import _dialog_watchdog, _silent
    app = s.app
    tr = safe(lambda: app.ApplicationAddIns.ItemById(STL_TRANSLATOR)) or _translator_for(app, "stl")
    if tr is None:
        raise InventorError("this Inventor has no STL translator (export_capabilities lists what it has)")
    if not safe(lambda: tr.Activated, True):
        tr.Activate()
    ctx = s.to.CreateTranslationContext()
    ctx.Type = K.kFileBrowseIOMechanism
    opts = s.to.CreateNameValueMap()
    safe(lambda: tr.HasSaveCopyAsOptions(d, ctx, opts))
    for k, v in (("Resolution", RESOLUTION[resolution]), ("OutputFileType", 0)):
        safe(lambda k=k: opts.Remove(k))          # a NameValueMap value is replaced by removing and adding it
        safe(lambda k=k, v=v: opts.Add(k, v))
    medium = s.to.CreateDataMedium()
    medium.FileName = path
    with _silent(s), _dialog_watchdog(s) as dog:
        tr.SaveCopyAs(d, ctx, opts, medium)
    if dog.cancelled:
        raise InventorError(f"the STL translator opened a dialog ({dog.cancelled}); it was cancelled")
    used = {opts.Name(i): safe(lambda i=i: opts.Value(opts.Name(i)), None) for i in range(1, opts.Count + 1)}
    return {k: v for k, v in used.items() if k in ("Resolution", "SurfaceDeviation", "NormalDeviation",
                                                   "MaxEdgeLength", "AspectRatio", "ExportUnits", "OutputFileType")}


def _model_size_mm(d) -> list[float]:
    rb = safe(lambda: d.ComponentDefinition.RangeBox)
    if rb is None:
        return [0.0, 0.0, 0.0]
    return [(getattr(rb.MaxPoint, a) - getattr(rb.MinPoint, a)) * 10 for a in "XYZ"]


def check_mesh(tris, model_size: list[float], solids: int) -> tuple[dict, list[str], float]:
    """Stats for a mesh plus plain-language problems, and the factor applied to bring it to millimetres."""
    st = meshio.stats(tris)
    problems = []
    k = meshio.scale_hint(st.get("size_mm", []), model_size) if st.get("triangles") else None
    if k and k != 1.0:
        tris[:] = meshio.scaled(tris, k)
        st = meshio.stats(tris)
    if not st.get("triangles"):
        problems.append("the mesh has no triangles")
        return st, problems, 1.0
    if k is None:
        problems.append(f"mesh size {st['size_mm']} doesn't match the model's {[round(v, 3) for v in model_size]}")
    if st["open_edges"]:
        problems.append(f"{st['open_edges']} open edges: the mesh isn't closed, slicers may repair or reject it")
    if solids and st["shells"] != solids:
        problems.append(f"{st['shells']} separate shells for {solids} solid bodies: bodies touch and merged, or a "
                        "body came apart")
    if st["bed_contact_mm2"] <= 0:
        problems.append("nothing lies flat at the lowest Z: the part needs supports or a flat face down")
    return st, problems, k or 1.0


@tool("documents")
def export_print_package(s: Session, folder: str = "", name: str = "", formats: str = "native,stl,3mf,step",
                         resolution: str = "fine", save_first: bool = True) -> dict:
    """Export a part or assembly for 3D printing in one go: the native file, STL, 3MF and STEP, all in millimetres.
    Everything is written to temporary files first and checked; only when every requested file is good are they moved
    into place, so a failing translator never removes the last good file. Each mesh is checked: triangles, separate
    shells (should equal the number of solid bodies), open edges, exact extents, volume and the area lying flat on
    the bed. When Inventor's 3MF translator is missing or refuses, a millimetre 3MF is written from the STL mesh
    instead, and the result says so.

    folder: where the files go (default: the workdir's exports folder)
    name: file name without extension (default: the document's name)
    formats: any of native, stl, 3mf, step
    resolution: STL/3MF tessellation: fine, medium or coarse (the deviations used are reported)
    save_first: save the document's own file first when it has changes
    """
    from ..writes import guard
    from .docs import _silent, save_document
    d = s.doc()
    kind = s.kind(d)
    if kind not in ("part", "assembly"):
        raise InventorError("export_print_package needs a part or an assembly")
    resolution = (resolution or "fine").lower()
    if resolution not in RESOLUTION:
        raise InventorError(f"resolution must be one of {sorted(RESOLUTION)}")
    want = [f.strip().lower() for f in formats.split(",") if f.strip()]
    bad = [f for f in want if f not in ("native", "stl", "3mf", "step", "stp")]
    if bad:
        raise InventorError(f"unknown formats {bad}; use native, stl, 3mf, step")
    from ..com import plain_name
    stem = plain_name(name or os.path.splitext(safe(lambda: d.DisplayName, "part"))[0], "part")
    folder = os.path.abspath(folder if folder and os.path.isabs(folder) else
                             os.path.join(s.workdir, "exports", folder or ""))
    os.makedirs(folder, exist_ok=True)
    native_ext = ".ipt" if kind == "part" else ".iam"
    final = {"native": os.path.join(folder, stem + native_ext), "stl": os.path.join(folder, stem + ".stl"),
             "3mf": os.path.join(folder, stem + ".3mf"), "step": os.path.join(folder, stem + ".step")}
    final["stp"] = final["step"]
    for f in want:
        guard(final[f], "export to")
    timings: dict = {}
    t0 = time.time()
    if save_first and safe(lambda: d.Dirty, False) and safe(lambda: d.FullFileName, ""):
        save_document(s)
    timings["save_s"] = round(time.time() - t0, 2)
    tmp = tempfile.mkdtemp(prefix=".forge-package-", dir=folder)
    out: dict = {"folder": folder, "files": {}, "units": "mm", "resolution": resolution}
    try:
        solids = sum(1 for b in items(safe(lambda: d.ComponentDefinition.SurfaceBodies) or [])
                     if safe(lambda b=b: b.IsSolid, True)) if kind == "part" else 0
        size = _model_size_mm(d)
        made: dict = {}
        tris = None
        if "stl" in want or "3mf" in want:
            t1 = time.time()
            stl_tmp = os.path.join(tmp, stem + ".stl")
            out["tessellation"] = _stl_export(s, d, stl_tmp, resolution)
            tris = meshio.read_stl(stl_tmp)
            st, problems, k = check_mesh(tris, size, solids)
            if k != 1.0:
                meshio.write_stl(stl_tmp, tris)
                out["rescaled_to_mm"] = k
            out["mesh"] = st
            if problems:
                out["mesh_problems"] = problems
            made["stl"] = stl_tmp
            timings["stl_s"] = round(time.time() - t1, 2)
        if "3mf" in want:
            t1 = time.time()
            p3 = os.path.join(tmp, stem + ".3mf")
            how = "inventor"
            try:
                tr = _translator_for(s.app, "3mf")
                if tr is None:
                    raise InventorError("no 3MF translator in this Inventor")
                with _silent(s):
                    d.SaveAs(p3, True)
                t3, unit = meshio.read_3mf(p3)
                if not t3:
                    raise InventorError("Inventor's 3MF has no triangles")
                out["3mf_unit_declared"] = unit
            except Exception as e:  # noqa: BLE001
                how = f"from the STL mesh (Inventor's 3MF export failed: {str(e)[:160]})"
                safe(lambda: os.remove(p3))
                meshio.write_3mf(p3, tris or [], name=stem)
                t3, _unit = meshio.read_3mf(p3)
            st3 = meshio.stats(t3)
            out["3mf"] = {"written": how, "triangles": st3["triangles"], "shells": st3["shells"],
                          "size_mm": st3["size_mm"], "matches_stl": bool(tris) and st3["size_mm"] == meshio.stats(
                              tris)["size_mm"]}
            made["3mf"] = p3
            timings["3mf_s"] = round(time.time() - t1, 2)
        if "step" in want or "stp" in want:
            t1 = time.time()
            ps = os.path.join(tmp, stem + ".step")
            with _silent(s):
                d.SaveAs(ps, True)
            if not os.path.exists(ps) or os.path.getsize(ps) < 100:
                raise InventorError("Inventor did not write the STEP file")
            made["step"] = ps
            timings["step_s"] = round(time.time() - t1, 2)
        if "native" in want:
            t1 = time.time()
            pn = os.path.join(tmp, stem + native_ext)
            own = safe(lambda: d.FullFileName, "")
            if own and os.path.normcase(own) == os.path.normcase(final["native"]):
                made["native"] = None                  # it is the document's own file, saved above
            else:
                with _silent(s):
                    d.SaveAs(pn, True)                 # a copy: the open document keeps its own file
                made["native"] = pn
            timings["native_s"] = round(time.time() - t1, 2)
        for key, src in made.items():                  # everything is good: move into place
            if src is None:
                out["files"][key] = final["native"]
                continue
            os.replace(src, final[key])
            out["files"][key] = final[key]
        out["bytes"] = {k: os.path.getsize(v) for k, v in out["files"].items() if os.path.exists(v)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out["timings"] = timings
    return out


# --------------------------------------------------------------------------- bodies
def _box(b) -> list[float] | None:
    rb = safe(lambda: b.RangeBox)
    if rb is None:
        return None
    return [r(rb.MinPoint.X * 10), r(rb.MinPoint.Y * 10), r(rb.MinPoint.Z * 10),
            r(rb.MaxPoint.X * 10), r(rb.MaxPoint.Y * 10), r(rb.MaxPoint.Z * 10)]


@tool("inspect", mutates=False)
def list_bodies(s: Session) -> dict:
    """The bodies of a part with their names (stable as features are added; give meaningful ones with rename), index,
    solid or surface, volume, range box and face/edge counts. Name a body in body= arguments."""
    rows = []
    for i, b in enumerate(items(s.cdef().SurfaceBodies), 1):
        rows.append({"index": i, "name": safe(lambda b=b: b.Name, ""), "solid": bool(safe(lambda b=b: b.IsSolid, True)),
                     "volume_mm3": body_volume(b), "box_mm": _box(b), "faces": safe(lambda b=b: b.Faces.Count, 0),
                     "edges": safe(lambda b=b: b.Edges.Count, 0),
                     "made_by": safe(lambda b=b: b.CreatedByFeature.Name, ""),
                     "visible": bool(safe(lambda b=b: b.Visible, True))})
    return {"bodies": rows, "count": len(rows)}


def _overlap_mm3(s: Session, a, b) -> float:
    """Volume two (transient or real) bodies share, mm³."""
    tb = s.app.TransientBRep
    try:
        blank = tb.Copy(a)
        tb.DoBoolean(blank, tb.Copy(b), K.kBooleanTypeIntersect)
        return body_volume(blank)
    except Exception:  # noqa: BLE001
        return 0.0


def body_triangles(body, tol_cm: float = 0.002) -> list:
    """A body's tessellation as triangles in mm."""
    from ..com import typed_call
    out = typed_call(body, "CalculateFacets", tol_cm, 0, 0, [], [], [])
    arrays = [v for v in out if isinstance(v, (list, tuple))]
    if len(arrays) < 3:
        return []
    coords, idx = arrays[0], arrays[2]
    base = 1 if idx and min(idx) >= 1 else 0
    pts = [(coords[3 * i] * 10, coords[3 * i + 1] * 10, coords[3 * i + 2] * 10) for i in range(len(coords) // 3)]
    return [(pts[idx[k] - base], pts[idx[k + 1] - base], pts[idx[k + 2] - base]) for k in range(0, len(idx) - 2, 3)]


def _gap_mm(s: Session, a, b) -> float | None:
    """Smallest distance between two bodies, mm, from fine meshes of both (the measuring tools don't take bodies of
    one part): exact between flat faces, within the mesh's chord error on curved ones."""
    ta, tb = safe(lambda: body_triangles(a), []), safe(lambda: body_triangles(b), [])
    if not ta or not tb:
        return None
    return meshio.mesh_gap(ta, tb)


@tool("inspect", mutates=False)
def body_gap(s: Session, body_one: str, body_two: str) -> dict:
    """Clearance between two bodies of one part (print-in-place parts, snap fits): the smallest gap in mm, or the
    volume they overlap by when they clash."""
    a, b = s.body(body_one), s.body(body_two)
    ov = _overlap_mm3(s, a, b)
    gap = None if ov > 1e-6 else _gap_mm(s, a, b)
    return {"bodies": [safe(lambda: a.Name, ""), safe(lambda: b.Name, "")], "overlap_mm3": r(ov, 4),
            "gap_mm": None if gap is None else r(gap, 4), "clear": ov <= 1e-6 and (gap or 0) > 0}


def pose_matrix(s: Session, move: dict):
    """Matrix for {"rotate": {"axis": [x, y, z], "point": [x, y, z] mm, "angle": deg}, "translate": [x, y, z] mm};
    rotation first, then translation."""
    tg = s.tg
    m = tg.CreateMatrix()
    rot = move.get("rotate")
    if rot:
        ax = rot.get("axis") or [0, 0, 1]
        pt = rot.get("point") or [0, 0, 0]
        m.SetToRotation(math.radians(float(rot.get("angle", 0))), tg.CreateVector(*[float(v) for v in ax]),
                        tg.CreatePoint(*[float(v) * MM for v in pt]))
    tr = move.get("translate")
    if tr:
        cur = m.Translation
        m.SetTranslation(tg.CreateVector(cur.X + float(tr[0]) * MM, cur.Y + float(tr[1]) * MM,
                                         cur.Z + float(tr[2]) * MM))
    return m


@tool("inspect")
def pose_bodies(s: Session, moves: list[dict], path: str = "", view: str = "iso", up: str = "z",
                reference_box: list[float] | None = None) -> dict:
    """Try the bodies of a part in another arrangement (a print-in-place mechanism opened, a hinge turned) without
    changing the part: copies are moved, every pair is checked for overlap and clearance, and with path a picture of
    the posed copies is saved. The saved build pose is never touched.

    moves: [{"body": name or index, "rotate": {"axis": [0, 0, 1], "point": [x, y, z], "angle": 59},
      "translate": [x, y, z]}]; mm and degrees; several moves on one body apply in order
    path: PNG to save of the posed bodies (optional)
    view: orientation for the picture (iso, front, top...)
    up: "z" for parts built on a printer bed, "y" for Inventor's convention
    reference_box: [x0, y0, z0, x1, y1, z1] mm: a block shown with the bodies (a phone, a shelf) and checked for clashes
    """
    tb = s.app.TransientBRep
    bodies = list(items(s.cdef().SurfaceBodies))
    copies = {safe(lambda b=b: b.Name, f"body {i}"): tb.Copy(b) for i, b in enumerate(bodies, 1)}
    for mv in moves:
        b = s.body(mv.get("body"))
        name = safe(lambda: b.Name, "")
        tb.Transform(copies[name], pose_matrix(s, mv))
    if reference_box:
        x0, y0, z0, x1, y1, z1 = [float(v) * MM for v in reference_box]
        box = s.tg.CreateBox()
        box.MinPoint = s.tg.CreatePoint(min(x0, x1), min(y0, y1), min(z0, z1))
        box.MaxPoint = s.tg.CreatePoint(max(x0, x1), max(y0, y1), max(z0, z1))
        copies["reference"] = tb.CreateSolidBlock(box)
    names = list(copies)
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = copies[names[i]], copies[names[j]]
            ov = _overlap_mm3(s, a, b)
            gap = None if ov > 1e-6 else _gap_mm(s, a, b)
            pairs.append({"between": [names[i], names[j]], "overlap_mm3": r(ov, 4),
                          "gap_mm": None if gap is None else r(gap, 4)})
    out = {"pairs": pairs, "clashes": [p for p in pairs if p["overlap_mm3"] > 1e-6],
           "tightest": min((p for p in pairs if p["gap_mm"] is not None), key=lambda p: p["gap_mm"], default=None)}
    if path:
        from ..writes import output_path
        out["picture"] = _render_copies(s, copies, output_path(s.workdir, path, "renders", ".png"), view, up)
    return out


def _render_copies(s: Session, copies: dict, path: str, view: str, up: str) -> dict:
    """Show transient bodies in a scratch part, take the picture, close it unsaved, and go back."""
    from .docs import screenshot
    app = s.app
    work = s._working()
    keep = getattr(s, "_current", None)
    tpl = s.stock_template("Standard (mm).ipt")
    scratch = app.Documents.Add(K.kPartDocumentObject, tpl or "", True)
    try:
        nb = scratch.ComponentDefinition.Features.NonParametricBaseFeatures
        for name, body in copies.items():
            safe(lambda body=body: nb.Add(body))
        scratch.Activate()
        s.set_current(scratch)
        shot = screenshot(s, path=path, view=view, up=up, clean=True)
    finally:
        safe(lambda: scratch.Close(True))
        if work is not None:
            safe(lambda: work.Activate())
        s._current = keep
        if keep is not None:
            s.set_current(keep)
    return shot
