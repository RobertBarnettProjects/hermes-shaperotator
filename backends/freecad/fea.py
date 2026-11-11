"""Linear-static FEA inside the FreeCAD worker: Gmsh tet10 mesh + CalculiX (ccx).

Everything is scripted — no dialogs, no picking. Faces are chosen with geometric selectors
so the same request works on a FreeCAD body or an imported STEP (e.g. exported from Inventor):

  {"face": 3}                                  face index (as in list_faces)
  {"plane": "x", "value": 0}                   every planar face lying in the plane x = 0
  {"near": [x, y, z]}                          the face closest to a point
  {"normal": [0, 0, -1]}                       the outermost planar face(s) facing -Z (e.g. the underside)
  {"normal": [0, 0, 1], "at_min": true}        of the faces facing +Z, the lowest one (at_max: highest)

Units: mm, N, MPa (N/mm²), tonne/mm³ internally handled by FreeCAD's unit system.
"""
from __future__ import annotations

import math
import os
import re
import time

import FreeCAD as App
import Part
import ObjectsFem

V = App.Vector
G_STD = 9.80665


class FeaError(Exception):
    pass


# ----------------------------------------------------------------- face selection
def _planar(f):
    return type(f.Surface).__name__ == "Plane"


def _outward(f):
    """Outward normal at the face's parametric centre. FreeCAD's normalAt already accounts for the
    face's orientation in the solid (checked on Part boxes and fused Part Design bodies), so no flip."""
    u0, u1, v0, v1 = f.ParameterRange
    return f.normalAt((u0 + u1) / 2, (v0 + v1) / 2)


def resolve_faces(shape, sel, tol=0.05):
    faces = shape.Faces
    if isinstance(sel, int):
        sel = {"face": sel}
    if isinstance(sel, list):
        out = []
        for s in sel:
            out += [i for i in resolve_faces(shape, s, tol) if i not in out]
        return out
    if not isinstance(sel, dict):
        raise FeaError(f"bad face selector {sel!r}")
    if "face" in sel:
        i = int(sel["face"])
        if not 1 <= i <= len(faces):
            raise FeaError(f"face {i} out of range (1..{len(faces)})")
        return [i]
    if "plane" in sel:
        ax = "xyz".index(str(sel["plane"]).lower())
        val = float(sel["value"])
        hits = [i for i, f in enumerate(faces, 1)
                if _planar(f) and all(abs(v.Point[ax] - val) <= max(tol, 1e-6) for v in f.Vertexes)]
        if not hits:
            raise FeaError(f"no planar face lies in {sel['plane']} = {val}")
        return hits
    if "near" in sel:
        p = V(*[float(c) for c in sel["near"]])
        pt = Part.Vertex(p)
        d = [(f.distToShape(pt)[0], i) for i, f in enumerate(faces, 1)]
        return [min(d)[1]]
    if "normal" in sel:
        n = V(*[float(c) for c in sel["normal"]])
        n.normalize()
        cands = []
        for i, f in enumerate(faces, 1):
            if not _planar(f):
                continue
            if _outward(f).dot(n) > 0.999:
                cands.append((f.CenterOfMass, i))
        if not cands:
            raise FeaError(f"no planar face faces {list(n)}")
        # at_min / at_max: lowest / highest position along the normal's axis in world coordinates
        # ({"normal": [0,0,-1], "at_min": true} = the lowest downward face). Neither: the outermost
        # face in the direction the normal points.
        axis = V(abs(n.x), abs(n.y), abs(n.z))
        if sel.get("at_min"):
            key = [c.dot(axis) for c, _ in cands]
            m = min(key)
        elif sel.get("at_max"):
            key = [c.dot(axis) for c, _ in cands]
            m = max(key)
        else:
            key = [c.dot(n) for c, _ in cands]
            m = max(key)
        return [i for k, (_, i) in zip(key, cands) if abs(k - m) <= max(tol, 1e-6)]
    raise FeaError(f"face selector needs one of face / plane / near / normal: {sel!r}")


def support_faces_all(spec, shape):
    return [resolve_faces(shape, s["faces"]) for s in spec["supports"]]


def _face_info(shape, idx):
    f = shape.Faces[idx - 1]
    c = f.CenterOfMass
    return {"face": idx, "area_mm2": round(f.Area, 2), "centroid": [round(c.x, 2), round(c.y, 2), round(c.z, 2)],
            "type": type(f.Surface).__name__}


# ----------------------------------------------------------------- dat parsing
_TOTAL = re.compile(r"total force \(fx,fy,fz\) for set (\S+) and time\s+\S+\s*\n\s*(\S+)\s+(\S+)\s+(\S+)", re.I)


def _reactions(dat_path):
    out = {}
    if not os.path.isfile(dat_path):
        return out
    with open(dat_path, encoding="utf-8", errors="replace") as fh:
        txt = fh.read()
    for m in _TOTAL.finditer(txt):
        out[m.group(1)] = [float(m.group(2)), float(m.group(3)), float(m.group(4))]
    return out


# ----------------------------------------------------------------- one solve
def _solve(doc, geom, spec, mesh_size, workdir, tag):
    os.makedirs(workdir, exist_ok=True)  # ccxtools does not create it, and ccx runs with it as cwd
    an = ObjectsFem.makeAnalysis(doc, f"Analysis_{tag}")
    solver = ObjectsFem.makeSolverCalculiXCcxTools(doc, f"Ccx_{tag}")
    solver.WorkingDir = workdir
    solver.AnalysisType = "static"
    solver.GeometricalNonlinearity = "linear"
    an.addObject(solver)

    m = spec["material"]
    mat = ObjectsFem.makeMaterialSolid(doc, f"Mat_{tag}")
    d = mat.Material
    d["Name"] = m.get("name", "material")
    d["YoungsModulus"] = f"{float(m['E_MPa'])} MPa"
    d["PoissonRatio"] = str(float(m["nu"]))
    d["Density"] = f"{float(m['density_kg_m3'])} kg/m^3"
    mat.Material = d
    an.addObject(mat)

    shape = geom.Shape
    support_sets = {}
    for k, s in enumerate(spec["supports"], 1):
        if s.get("type", "fixed") != "fixed":
            raise FeaError("only fixed supports are supported in this version")
        faces = resolve_faces(shape, s["faces"])
        c = ObjectsFem.makeConstraintFixed(doc, f"Fix{k}_{tag}")
        c.References = [(geom, tuple(f"Face{i}" for i in faces))]
        an.addObject(c)
        support_sets[c.Name] = faces

    applied = V(0, 0, 0)
    load_info = []
    for k, ld in enumerate(spec["loads"], 1):
        t = ld.get("type", "force")
        if t == "force":
            faces = resolve_faces(shape, ld["faces"])
            dvec = V(*[float(c) for c in ld["direction"]])
            if dvec.Length < 1e-12:
                raise FeaError("force direction is zero")
            dvec.normalize()
            line = doc.addObject("Part::Feature", f"Dir{k}_{tag}")
            line.Shape = Part.makeLine(V(0, 0, 0), dvec)
            line.Visibility = False
            c = ObjectsFem.makeConstraintForce(doc, f"Force{k}_{tag}")
            c.References = [(geom, tuple(f"Face{i}" for i in faces))]
            c.Force = f"{float(ld['force_N'])} N"
            c.Direction = (line, ["Edge1"])
            c.Reversed = False
            an.addObject(c)
            applied += dvec * float(ld["force_N"])
            load_info.append({"type": "force", "N": float(ld["force_N"]), "direction": [round(x, 4) for x in dvec],
                              "faces": faces, "note": "total force shared over the faces"})
        elif t == "pressure":
            faces = resolve_faces(shape, ld["faces"])
            c = ObjectsFem.makeConstraintPressure(doc, f"Pressure{k}_{tag}")
            c.References = [(geom, tuple(f"Face{i}" for i in faces))]
            c.Pressure = f"{float(ld['pressure_MPa'])} MPa"
            c.Reversed = False
            an.addObject(c)
            for i in faces:  # pressure pushes into the face: -outward normal
                f = shape.Faces[i - 1]
                if _planar(f):
                    applied += _outward(f) * (-float(ld["pressure_MPa"]) * f.Area)
            load_info.append({"type": "pressure", "MPa": float(ld["pressure_MPa"]), "faces": faces})
        elif t == "gravity":
            g = V(*[float(c) for c in ld.get("direction", [0, 0, -1])])
            g.normalize()
            factor = float(ld.get("g_factor", 1.0))
            c = ObjectsFem.makeConstraintSelfWeight(doc, f"Gravity{k}_{tag}")
            c.GravityAcceleration = f"{G_STD * 1000 * factor} mm/s^2"
            c.GravityDirection = g
            an.addObject(c)
            mass_kg = shape.Volume * 1e-9 * float(m["density_kg_m3"])
            applied += g * (mass_kg * G_STD * factor)
            load_info.append({"type": "gravity", "g_factor": factor, "mass_kg": round(mass_kg, 3)})
        else:
            raise FeaError(f"unknown load type {t!r} (force, pressure, gravity)")
    if not spec["loads"]:
        raise FeaError("no loads given")

    mesh = ObjectsFem.makeMeshGmsh(doc, f"Mesh_{tag}")
    mesh.Shape = geom
    mesh.CharacteristicLengthMax = f"{mesh_size} mm"
    mesh.CharacteristicLengthMin = f"{mesh_size / 5} mm"
    mesh.ElementOrder = "2nd"
    if spec.get("_straight_2nd"):          # thin walls: curved mid-side nodes invert elements (negative Jacobian)
        for prop, val in (("SecondOrderLinear", True),):
            try:
                setattr(mesh, prop, val)
            except Exception:
                pass
    an.addObject(mesh)
    doc.recompute()

    from femmesh import gmshtools
    t0 = time.time()
    if not gmshtools.GmshTools(mesh).run(blocking=True) or mesh.FemMesh.NodeCount == 0:
        raise FeaError("meshing failed — try a larger mesh_size or check the geometry is a valid solid")
    t_mesh = time.time() - t0

    from femtools import ccxtools
    fea = ccxtools.FemToolsCcx(an, solver)
    fea.update_objects()
    fea.setup_working_dir(workdir)
    fea.setup_ccx()
    msg = fea.check_prerequisites()
    if msg:
        raise FeaError(f"analysis setup problem: {msg}")
    fea.purge_results()
    t0 = time.time()
    fea.write_inp_file()
    fea.ccx_run()
    fea.load_results()
    t_solve = time.time() - t0
    res = [o for o in an.Group if o.isDerivedFrom("Fem::FemResultObject")]
    if not res:
        raise FeaError("CalculiX produced no results — see the .dat/.sta files in " + workdir)
    r = res[0]

    vm = list(r.vonMises)
    disp = list(r.DisplacementLengths)
    nodes = list(r.NodeNumbers)
    i_vm = max(range(len(vm)), key=vm.__getitem__)
    i_d = max(range(len(disp)), key=disp.__getitem__)
    fm = mesh.FemMesh
    p_vm = fm.getNodeById(nodes[i_vm])
    p_d = fm.getNodeById(nodes[i_d])

    dat = os.path.splitext(fea.inp_file_name)[0] + ".dat"
    reac_by_set = _reactions(dat)
    total_r = V(0, 0, 0)
    reactions = {}
    for name, faces in support_sets.items():
        rv = None
        for k2, v in reac_by_set.items():
            if k2.upper().startswith(name.upper()):
                rv = v
        if rv is not None:
            total_r += V(*rv)
        reactions[name] = {"faces": faces, "RF_N": [round(x, 2) for x in rv] if rv else None}

    eq_err = None
    if applied.Length > 1e-9 and reac_by_set:
        eq_err = (total_r + applied).Length / applied.Length

    # does the peak stress sit on a support? (usually a singularity, not real stress)
    peak_on_support = False
    pv = Part.Vertex(p_vm)
    for faces in support_sets.values():
        for i in faces:
            if shape.Faces[i - 1].distToShape(pv)[0] < mesh_size * 0.6:
                peak_on_support = True

    return {
        "vm_max_MPa": round(vm[i_vm], 3), "vm_max_at": [round(p_vm.x, 2), round(p_vm.y, 2), round(p_vm.z, 2)],
        "disp_max_mm": round(disp[i_d], 5), "disp_max_at": [round(p_d.x, 2), round(p_d.y, 2), round(p_d.z, 2)],
        "nodes": fm.NodeCount, "elements": fm.VolumeCount, "mesh_size_mm": mesh_size,
        "applied_total_N": [round(x, 2) for x in applied], "reactions": reactions,
        "reaction_total_N": [round(x, 2) for x in total_r] if reac_by_set else None,
        "equilibrium_error": round(eq_err, 5) if eq_err is not None else None,
        "peak_on_support": peak_on_support, "loads": load_info,
        "t_mesh_s": round(t_mesh, 2), "t_solve_s": round(t_solve, 2),
        "_result": r, "_mesh": mesh, "_workdir": workdir, "_inp": fea.inp_file_name,
    }


# ----------------------------------------------------------------- viewer mesh
def viewer_mesh(shape, max_tris=30000):
    """Triangulate every face, tagging each triangle with its 1-based face index (for picking)."""
    diag = shape.BoundBox.DiagonalLength or 1.0
    tol = diag / 400.0
    verts, tris, face_of_tri, faces = [], [], [], []
    source = shape
    for attempt in range(6):
        verts, tris, face_of_tri, faces = [], [], [], []
        if attempt:             # OCC keeps the finest mesh a shape already has: coarsen on a mesh-free copy
            source = shape.copy()
        for i, f in enumerate(source.Faces, 1):
            pts, tri = f.tessellate(tol)
            base = len(verts) // 3
            for p in pts:
                verts += [round(p.x, 3), round(p.y, 3), round(p.z, 3)]
            for a, b, c in tri:
                tris += [base + a, base + b, base + c]
                face_of_tri.append(i)
            c = f.CenterOfMass
            n = _outward(f) if _planar(f) else None
            info = {"id": i, "type": type(f.Surface).__name__, "area": round(f.Area, 2),
                    "centroid": [round(c.x, 2), round(c.y, 2), round(c.z, 2)],
                    "normal": [round(n.x, 4), round(n.y, 4), round(n.z, 4)] if n is not None else None}
            if type(f.Surface).__name__ == "Cylinder":
                info["diameter"] = round(2 * f.Surface.Radius, 3)
            faces.append(info)
        if len(face_of_tri) <= max_tris or max_tris <= 1:      # 1 = only the face table is wanted
            break
        tol *= 2.0
    edges = []
    edge_points, max_edge_points = 0, max(20000, max_tris * 2)
    for e in sorted(shape.Edges, key=lambda x: -x.Length):      # longest first, so a cap drops the small detail
        if e.Length <= 0 or edge_points >= max_edge_points:
            continue
        pts = e.discretize(Deflection=tol)
        edge_points += len(pts)
        edges.append([round(c, 3) for p in pts for c in (p.x, p.y, p.z)])
    bb = shape.BoundBox
    return {"vertices": verts, "triangles": tris, "face_of_tri": face_of_tri, "faces": faces, "edges": edges,
            "bbox": [bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax]}


def viewer_from_fem(shape, solve, max_tris=60000):
    """Pane mesh from the FE surface itself: tri6 boundary faces -> 4 linear triangles, exact nodal von Mises."""
    r, fm = solve["_result"], solve["_mesh"].FemMesh
    vm = dict(zip(r.NodeNumbers, r.vonMises))
    dv = dict(zip(r.NodeNumbers, r.DisplacementVectors))
    base = viewer_mesh(shape, max_tris=1)  # face table, edges, bbox (triangles replaced below)
    index, verts, values, tris, fot, disp = {}, [], [], [], [], []

    def vid(n):
        if n not in index:
            p = fm.getNodeById(n)
            index[n] = len(values)
            verts.extend((round(p.x, 3), round(p.y, 3), round(p.z, 3)))
            values.append(round(vm.get(n, 0.0), 3))
            d = dv.get(n)
            disp.extend((round(d.x, 6), round(d.y, 6), round(d.z, 6)) if d is not None else (0.0, 0.0, 0.0))
        return index[n]

    for i, f in enumerate(shape.Faces, 1):
        try:
            elems = fm.getFacesByFace(f)
        except Exception:
            elems = []
        for el in elems:
            nodes = fm.getElementNodes(el)
            if len(nodes) >= 6:      # tri6: corners 0,1,2  mid-sides 3 (0-1), 4 (1-2), 5 (2-0)
                a, b, c, ab, bc, ca = (vid(n) for n in nodes[:6])
                sub = ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca))
            elif len(nodes) >= 3:
                sub = ((vid(nodes[0]), vid(nodes[1]), vid(nodes[2])),)
            else:
                continue
            for t in sub:
                tris.extend(t)
                fot.append(i)
        if len(fot) > max_tris:
            break
    if not fot:
        raise FeaError("could not extract the FE surface for the viewer")
    base.update(vertices=verts, triangles=tris, face_of_tri=fot, values=values, displacements=disp)
    return base


def _vertex_values(shape, mesh_out, solve):
    """von Mises at each viewer vertex: nearest FE surface node of the same face."""
    r, fm = solve["_result"], solve["_mesh"].FemMesh
    vm = dict(zip(r.NodeNumbers, r.vonMises))
    verts = mesh_out["vertices"]
    nv = len(verts) // 3
    vals = [0.0] * nv
    # vertex -> face (first triangle that uses it)
    vface = [0] * nv
    t, fot = mesh_out["triangles"], mesh_out["face_of_tri"]
    for k, fi in enumerate(fot):
        for j in t[3 * k:3 * k + 3]:
            if not vface[j]:
                vface[j] = fi
    cache = {}
    for j in range(nv):
        fi = vface[j]
        if fi not in cache:
            try:
                ids = [n for n in fm.getNodesByFace(shape.Faces[fi - 1]) if n in vm]
            except Exception:
                ids = []
            cache[fi] = [(fm.getNodeById(n), vm[n]) for n in ids]
        nodes = cache[fi]
        if not nodes:
            continue
        x, y, z = verts[3 * j], verts[3 * j + 1], verts[3 * j + 2]
        best = min(nodes, key=lambda q: (q[0].x - x) ** 2 + (q[0].y - y) ** 2 + (q[0].z - z) ** 2)
        vals[j] = round(best[1], 3)
    return vals


# ----------------------------------------------------------------- plot
def _plot(shape, solve, path, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    r, mesh = solve["_result"], solve["_mesh"]
    d = V(1, -1, 1)
    d.normalize()
    u = V(0, 0, 1).cross(d)
    u.normalize()
    w = d.cross(u)
    fm = mesh.FemMesh
    surf = set()
    for f in shape.Faces:
        if _planar(f) and _outward(f).dot(d) <= 0:  # hide faces pointing away from the viewer
            continue
        try:
            surf.update(fm.getNodesByFace(f))
        except Exception:
            pass
    vm = dict(zip(r.NodeNumbers, r.vonMises))
    ids = [n for n in surf if n in vm] or list(vm)
    pts = [fm.getNodeById(n) for n in ids]
    # draw far points first so near ones stay visible
    order = sorted(range(len(pts)), key=lambda i: pts[i].dot(d))
    xs = [pts[i].dot(u) for i in order]
    ys = [pts[i].dot(w) for i in order]
    cs = [vm[ids[i]] for i in order]
    fig = plt.figure(figsize=(8, 6), dpi=200)
    ax = fig.add_axes([0.02, 0.02, 0.84, 0.9])
    sc = ax.scatter(xs, ys, c=cs, s=4, cmap="turbo", linewidths=0)
    for e in shape.Edges:
        p = e.discretize(Deflection=0.1) if e.Length > 0 else []
        if len(p) > 1:
            ax.plot([q.dot(u) for q in p], [q.dot(w) for q in p], color="#111827", linewidth=0.5)
    pk = V(*solve["vm_max_at"])
    ax.plot([pk.dot(u)], [pk.dot(w)], marker="x", color="black", markersize=9)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(title, fontsize=9)
    cax = fig.add_axes([0.88, 0.1, 0.03, 0.75])
    fig.colorbar(sc, cax=cax, label="von Mises (MPa)")
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


# ----------------------------------------------------------------- entry point
def run(doc_shape, spec, out_dir, doc_factory):
    """doc_shape: Part.Shape to analyse. spec: material/supports/loads/mesh_size/convergence."""
    if doc_shape is None or doc_shape.isNull() or doc_shape.Volume <= 0:
        raise FeaError("no solid to analyse")
    if len(doc_shape.Solids) != 1:
        raise FeaError(f"analysis needs exactly one solid (found {len(doc_shape.Solids)}) — "
                       "assemblies/multi-body need bonded contacts, not supported yet")
    os.makedirs(out_dir, exist_ok=True)
    bb = doc_shape.BoundBox
    dims = sorted([bb.XLength, bb.YLength, bb.ZLength])
    auto = max(min(bb.DiagonalLength / 30.0, dims[0] / 2.0), 0.5)
    h = float(spec.get("mesh_size") or auto * float(spec.get("mesh_factor", 1.0)))
    healed_note = None
    tiny = [f for f in doc_shape.Faces if f.Area < max(0.5, (h * 0.2) ** 2 * 0.25)]
    if tiny:                       # sliver faces where several tubes meet make Gmsh fail: heal them first
        try:
            healed = doc_shape.defeaturing(tiny)
            if (healed.isValid() and len(healed.Solids) == 1
                    and abs(healed.Volume - doc_shape.Volume) <= 0.002 * doc_shape.Volume):
                healed_note = f"healed {len(tiny)} sliver face(s) before meshing (volume change "                               f"{(healed.Volume - doc_shape.Volume) / doc_shape.Volume * 100:+.3f} %)"
                doc_shape = healed
        except Exception:
            pass
        spec = {**spec, "_straight_2nd": True}

    doc = doc_factory()
    geom = doc.addObject("Part::Feature", "FeaGeometry")
    geom.Shape = doc_shape
    doc.recompute()

    s1 = _solve(doc, geom, spec, h, os.path.join(out_dir, "run1"), "a")
    conv = None
    if spec.get("convergence", True):
        h2 = round(h / 1.6, 3)
        s2 = _solve(doc, geom, spec, h2, os.path.join(out_dir, "run2"), "b")
        dvm = abs(s2["vm_max_MPa"] - s1["vm_max_MPa"]) / max(s2["vm_max_MPa"], 1e-9)
        dd = abs(s2["disp_max_mm"] - s1["disp_max_mm"]) / max(s2["disp_max_mm"], 1e-12)
        conv = {"coarse_mesh_mm": h, "fine_mesh_mm": h2, "vm_change": round(dvm, 4), "disp_change": round(dd, 4),
                "converged": dvm < 0.10 and dd < 0.05}
        final = s2
    else:
        final = s1

    m = spec["material"]
    fy = m.get("yield_MPa")
    out = {k: v for k, v in final.items() if not k.startswith("_")}
    out["material"] = {k: m.get(k) for k in ("name", "E_MPa", "nu", "density_kg_m3", "yield_MPa", "note")}
    out["mass_kg"] = round(doc_shape.Volume * 1e-9 * float(m["density_kg_m3"]), 4)
    out["volume_mm3"] = round(doc_shape.Volume, 2)
    out["safety_factor_yield"] = round(float(fy) / final["vm_max_MPa"], 3) if fy and final["vm_max_MPa"] > 0 else None
    out["convergence"] = conv
    out["supports"] = [{"selector": s["faces"], "resolved": [_face_info(doc_shape, i) for i in
                                                           resolve_faces(doc_shape, s["faces"])]}
                       for s in spec["supports"]]

    warnings = []
    if out["equilibrium_error"] is None:
        warnings.append("reaction forces could not be read — equilibrium not verified")
    elif out["equilibrium_error"] > 0.01:
        warnings.append(f"equilibrium error {out['equilibrium_error']:.1%} — loads/supports may be wrong")
    if conv and not conv["converged"]:
        warnings.append(f"mesh not converged (peak stress changed {conv['vm_change']:.0%} on refinement) — "
                        "refine further or judge stress away from the peak")
    if final["peak_on_support"]:
        # A true constraint singularity keeps growing as the mesh is refined; a peak that settles
        # (e.g. the root of a cantilever) is real bending stress and needs no warning.
        if conv is None:
            warnings.append("peak stress sits at a fixed support — could be a singularity from the idealised "
                            "constraint; re-run with convergence on to tell")
        elif conv["vm_change"] >= 0.05:
            warnings.append(f"peak stress sits at a fixed support and grew {conv['vm_change']:.0%} on refinement — "
                            "likely a constraint singularity; judge stresses a short distance away")
    if out["safety_factor_yield"] is not None and out["safety_factor_yield"] < 1:
        warnings.append("peak stress exceeds yield — linear results above yield are not physical")
    out["warnings"] = warnings
    if healed_note:
        out["geometry_note"] = healed_note
    out["verified"] = not warnings
    out["image"] = _plot(doc_shape, final, os.path.join(out_dir, "von_mises.png"),
                         f"von Mises max {final['vm_max_MPa']:.1f} MPa, disp max {final['disp_max_mm']:.3f} mm")
    try:  # scene for the Desktop pane: geometry + stress per vertex (not returned to the model)
        import json as _json
        try:
            vm_mesh = viewer_from_fem(doc_shape, final)
        except Exception:
            vm_mesh = viewer_mesh(doc_shape)
            vm_mesh["values"] = _vertex_values(doc_shape, vm_mesh, final)
        vm_mesh["value_label"] = "von Mises (MPa)"
        vm_mesh["support_faces"] = sorted({i for f in support_faces_all(spec, doc_shape) for i in f})
        vm_mesh["load_faces"] = sorted({i for ld in spec["loads"] if "faces" in ld
                                        for i in resolve_faces(doc_shape, ld["faces"])})
        with open(os.path.join(out_dir, "scene.json"), "w", encoding="utf-8") as fh:
            _json.dump(vm_mesh, fh)
        out["scene"] = os.path.join(out_dir, "scene.json")
    except Exception as e:  # the pane is optional; never fail the analysis for it
        out["scene_error"] = str(e)
    out["workdir"] = out_dir
    App.closeDocument(doc.Name)
    return out
