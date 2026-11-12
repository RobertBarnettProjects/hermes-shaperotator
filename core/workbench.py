"""The engineering workbench around the CAD tools: analysis plans the user approves, a design history with diffs,
generated artifacts (report, BOM, risk register), shareable snapshots, and live sync of edits made in the CAD app.

Everything here is kept in the session's job folder and mirrored into the Desktop pane's scene, so the pane can show
plan cards, the version timeline and the artifacts list.
"""
from __future__ import annotations

import base64
import copy
import uuid
import datetime as _dt
import html
import json
import math
import os
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from .evidence import EVIDENCE
from .state import STATE

G = 9.80665


# --------------------------------------------------------------------------- small store
def _store(name: str) -> Path:
    return STATE.jobs() / name


def _load(name: str, default):
    p = _store(name)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return default
    return default


def _save(name: str, data) -> None:
    p = _store(name)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def _scene_patch(**fields) -> None:
    """Merge fields into the pane's scene without touching the model mesh."""
    try:
        from .scene import SCENE_LOCK, _notify, _write_scene, live_dir
        with SCENE_LOCK:
            p = live_dir() / "scene.json"
            scene = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
            scene.update(fields)
            scene["rev"] = f"{int(time.time() * 1000)}-wb"
            _write_scene(scene)
        _notify({"rev": scene["rev"], "kind": "workbench"})
    except Exception:
        pass


def publish_workbench() -> None:
    _scene_patch(plans=_load("plans.json", {}), history=_load("history.json", []), artifacts=_load("artifacts.json", []),
                 model_info=model_info())


# --------------------------------------------------------------------------- model facts
def _inventor() -> bool:
    return STATE.family == "inventor"


def _native(tool: str, **args) -> dict:
    return STATE.backend().call("native", tool=tool, args=args)


def model_info() -> dict:
    """Material, mass, size and the user parameters of the active model (best effort, never raises)."""
    info: dict[str, Any] = {}
    try:
        st = STATE.backend().call("status")
        doc = st.get("document") or st.get("active") or {}
        info.update({k: doc.get(k) for k in ("name", "type", "path", "material", "volume_mm3", "features", "occurrences")
                     if doc.get(k) is not None})
        if _inventor():
            mp = _native("mass_properties")
            info.update({k: mp.get(k) for k in ("mass_kg", "volume_mm3", "area_mm2", "cog_mm", "material") if mp.get(k) is not None})
            ins = _native("inspect") if info.get("type") != "assembly" else {}
            if ins.get("size_mm"):
                info["size_mm"] = ins["size_mm"]
            params = _native("get_parameters", user_only=True).get("user", [])
        else:
            params = STATE.backend().call("get_parameters").get("parameters", [])
            ins = STATE.backend().call("inspect")
            for k in ("volume_mm3", "size_mm", "faces"):
                if ins.get(k) is not None:
                    info[k] = ins[k]
            plans = _load("plans.json", {})
            mat = next((p.get("material") for p in reversed(list(plans.values())) if p.get("material")), "")
            if mat and info.get("volume_mm3"):
                from .materials import resolve
                m = resolve(mat, None)
                info["material"] = m.get("name", mat)
                if m.get("density_kg_m3"):
                    info["mass_kg"] = round(info["volume_mm3"] * 1e-9 * m["density_kg_m3"], 3)
        info["parameters"] = {p["name"]: p.get("value", p.get("expression")) for p in params if p.get("name")}
    except Exception as e:  # the pane and reports work without it
        info.setdefault("note", f"model details unavailable: {type(e).__name__}")
    return info


# --------------------------------------------------------------------------- plans (conversational FEA)
def _vec(v) -> list[float]:
    return [float(x) for x in (v or [0, 0, 0])][:3]


def _resolve_ref(ref) -> dict:
    """A face reference from chat or the pane (@Face_12{near:[x,y,z]}, {"near": [...]}, an Inventor index, or
    "Comp:1/5") -> what the solvers take, with a readable description."""
    if isinstance(ref, (int, float)):
        return {"index": int(ref), "label": f"face {int(ref)}"}
    if isinstance(ref, str):
        s = ref.strip()
        if "{" in s and "near" in s:
            try:
                near = json.loads(s[s.index("["):s.index("]") + 1])
                return {"near": [float(x) for x in near], "label": s.split("{")[0] or "face"}
            except (ValueError, IndexError):
                pass
        if "/" in s:
            return {"component_face": s, "label": s}
        if s.isdigit():
            return {"index": int(s), "label": f"face {s}"}
        raise ValueError(f"can't read the face reference {ref!r}; use a face index, \"Comp:1/5\" or {{\"near\": [x, y, z]}}")
    if isinstance(ref, dict):
        if "near" in ref:
            return {"near": _vec(ref["near"]), "label": ref.get("label") or "face near " + str(_vec(ref["near"]))}
        if "face" in ref:
            return _resolve_ref(ref["face"])
    raise ValueError(f"can't read the face reference {ref!r}")


def _inventor_face(ref: dict):
    if "index" in ref:
        return ref["index"]
    if "component_face" in ref:
        return ref["component_face"]
    x, y, z = ref["near"]
    try:                                    # the face the point actually lies on
        hit = _native("face_at_point", x=x, y=y, z=z, tolerance=1.0)
        ref["resolved"] = {"face": hit["face"], "ambiguous": hit.get("ambiguous"), "area_mm2": (hit.get("match") or {}).get("area_mm2")}
        return {"face": hit["face"], "at": [x, y, z]}       # the point is where the mouse pick aims
    except Exception:
        return _native("find_face", x=x, y=y, z=z)["face"]


def _near_of(ref: dict) -> list[float] | None:
    """A point on a referenced face, for the CalculiX re-solve (which reads geometry from STEP, not Inventor indices)."""
    if "near" in ref:
        return ref["near"]
    if "index" in ref and _inventor():
        rows = _native("list_faces", max_count=2000).get("faces", [])
        row = next((r for r in rows if r.get("index") == ref["index"]), None)
        return row.get("centroid_mm") if row else None
    return None


def _cross_check(plan: dict, inventor_res: dict) -> dict | None:
    """Solve the same set-up again with CalculiX (FreeCAD FEM): gives the pane a field it can colour, probe and zone,
    and an independent second opinion on Inventor's numbers. Parts only; every support is treated as fixed."""
    from .tools import _fea_run, _fea_source
    fc = STATE.backend("freecad")
    if not fc.available()[0]:
        return None
    sups, notes = [], []
    material = plan.get("material") or None
    try:                                       # use Inventor's own material properties so both solvers see the same steel
        md = _native("material_properties")
        if md.get("E_MPa") and md.get("density_kg_m3"):
            material = {"name": md.get("name", "Inventor material"), "E_MPa": md["E_MPa"], "nu": md.get("poisson") or 0.3,
                        "density_kg_m3": md["density_kg_m3"], "yield_MPa": md.get("yield_MPa")}
    except Exception:
        notes.append("material properties not read from Inventor; CalculiX used the plan's material")
    for sp in plan["supports"]:
        p = _near_of(sp["ref"])
        if p is None:
            return None
        if sp["kind"] != "fixed":
            return {"error": f"CalculiX cross-check does not support {sp['kind']} supports"}
        sups.append({"faces": {"near": p}})
    loads = []
    for ld in plan["loads"]:
        pts = [_near_of(f) for f in ld["faces"]]
        if any(p is None for p in pts):
            return None
        mag = math.sqrt(sum(c * c for c in ld["force_N"]))
        loads.append({"type": "force", "faces": [{"near": p} for p in pts], "force_N": mag, "direction": ld["force_N"]})
    if plan.get("gravity"):
        loads.append({"type": "gravity", "direction": [0, 0, -1]})
    if not loads:
        return {"error": "nothing to load in the cross-check"}
    args = {"action": "run", "material": material, "name": f"{plan['id']} cross-check (CalculiX)",
            "supports": sups, "loads": loads, "convergence": False,
            "mesh_factor": {"coarse": 1.8, "default": 1.0, "fine": 0.65, "very fine": 0.45}[plan["mesh"]]}
    r = json.loads(_fea_run(args, _fea_source(args)))
    if not r.get("ok"):
        return {"error": r.get("error")}
    out = {"solver": "CalculiX (FreeCAD FEM)", "vm_max_MPa": r.get("vm_max_MPa"), "disp_max_mm": r.get("disp_max_mm"),
           "safety_factor": r.get("safety_factor_yield"), "evidence_id": r.get("evidence_id"), "peak_at_mm": r.get("vm_max_at"),
           "notes": notes}
    a, b = inventor_res.get("vm_max_MPa"), out["vm_max_MPa"]
    if a and b:
        out["peak_difference_pct"] = round((b - a) / a * 100, 1)
    da, db = inventor_res.get("disp_max_mm"), out["disp_max_mm"]
    if da and db:
        out["deflection_difference_pct"] = round((db - da) / da * 100, 1)
    return out


def _load_basis(ld: dict) -> tuple[list[float], str]:
    """Force vector (N) of a load and the arithmetic behind it, from either a force or mass x g-factor."""
    if ld.get("force") is not None:
        f = _vec(ld["force"])
        return f, f"{math.sqrt(sum(c * c for c in f)):.0f} N as given"
    mass = float(ld.get("mass_kg") or 0)
    g = float(ld.get("g_factor") or 1)
    direction = _vec(ld.get("direction") or [0, 0, -1])
    n = math.sqrt(sum(c * c for c in direction)) or 1
    mag = mass * g * G
    return [direction[i] / n * mag for i in range(3)], f"{mass:g} kg x {g:g} g x {G:.2f} m/s² = {mag:.0f} N"


def _model_binding() -> dict:
    st = STATE.backend().call("status")
    doc = st.get("document") or st.get("active") or {}
    path = (doc.get("path") or doc.get("full_path")) if isinstance(doc, dict) else st.get("file")
    path = path or st.get("file") or st.get("path")
    version = st.get("model_version")
    if _inventor():
        mv = _native("model_version")
        version = mv.get("version")
        if mv.get("document") and os.path.isabs(mv["document"]):
            path = mv["document"]
    elif version is None:
        version = doc.get("model_version", doc.get("version")) if isinstance(doc, dict) else st.get("version")
    if version is None:
        raise ValueError("backend must expose model_version in status before proposing an analysis plan")
    return {"backend": STATE.backend_name, "document_path": os.path.normcase(os.path.abspath(path)) if path else "",
            "model_version": version}


def _validate_setup(plan: dict) -> None:
    kinds = {"fixed", "pin", "frictionless"} if _inventor() else {"fixed"}
    if any(sp["kind"] not in kinds for sp in plan["supports"]):
        raise ValueError(f"unsupported support kind for this solver; supported kinds: {', '.join(sorted(kinds))}")
    if plan.get("mesh", "default") not in ("coarse", "default", "fine", "very fine"):
        raise ValueError("mesh must be coarse, default, fine or very fine")


def plan_propose(args: dict) -> dict:
    """Turn the agent's reading of the user's intent into a reviewable plan card."""
    estimates = _estimates(args.get("estimates"))
    plans = _load("plans.json", {})
    pid = f"P{len(plans) + 1}"
    supports = []
    for s in args.get("supports") or []:
        r = _resolve_ref(s.get("face") if isinstance(s, dict) else s)
        supports.append({"kind": (s.get("kind") if isinstance(s, dict) else None) or "fixed", "ref": r,
                         "why": s.get("why", "") if isinstance(s, dict) else ""})
    loads = []
    for ld in args.get("loads") or []:
        faces = [_resolve_ref(f) for f in (ld.get("faces") or ([ld["face"]] if "face" in ld else []))]
        vec, basis = _load_basis(ld)
        loads.append({"name": ld.get("name") or f"Load {len(loads) + 1}", "faces": faces, "force_N": [round(c, 1) for c in vec],
                      "basis": basis, "kind": "force", "why": ld.get("why", "")})
    if not supports or not (loads or args.get("gravity")):
        raise ValueError("a plan needs at least one support and one load (or gravity)")
    total = [round(sum(ld["force_N"][i] for ld in loads), 1) for i in range(3)]
    unresolved = []
    if _inventor():
        for r in [sp["ref"] for sp in supports] + [f for ld in loads for f in ld["faces"]]:
            try:
                _inventor_face(r)
            except Exception as e:
                unresolved.append(f"{r.get('label')}: {e}")
    binding = _model_binding()
    plan = {"id": pid, "title": args.get("title") or "Analysis plan", "intent": args.get("intent", ""),
            "study": "static", "material": args.get("material") or "", "gravity": bool(args.get("gravity")),
            "mesh": args.get("mesh") or "default", "supports": supports, "loads": loads,
            "criteria": {"min_safety_factor": float(args.get("min_safety_factor") or 2.0),
                         "max_displacement_mm": args.get("max_displacement_mm"), "objective": args.get("objective") or ""},
            "total_load_N": total, "assumptions": args.get("assumptions") or [
                "linear elastic material, small deflections", "supports are ideal (no flex in the mounts)",
                "loads applied as uniform forces over the chosen faces", "welds modelled as continuous material"],
            "estimates": estimates,
            "status": "proposed", "created": _dt.datetime.now().isoformat(timespec="seconds"),
             **binding, "unresolved": unresolved}
    _validate_setup(plan)
    plans[pid] = plan
    _save("plans.json", plans)
    publish_workbench()
    return plan


def plan_run(args: dict) -> dict:
    plans = _load("plans.json", {})
    pid = args.get("plan_id") or (list(plans)[-1] if plans else "")
    if pid not in plans:
        raise ValueError(f"no plan {pid!r}; propose one first")
    plan = copy.deepcopy(plans[pid])
    if plan.get("status") != "proposed" and not args.get("rerun"):
        raise ValueError(f"{pid} has already run; propose a new plan (or pass rerun=true to run it again)")
    if plan.get("backend") != STATE.backend_name:
        raise ValueError("Backend changed. Propose again and review the new plan.")
    binding = _model_binding()
    if any(plan.get(k) != v for k, v in binding.items()):
        raise ValueError("Backend, document full path or model_version changed. Propose again and review the new plan.")
    edits = args.get("edits") or {}
    if any(k in edits for k in ("loads", "supports", "material", "mesh", "gravity")) and "estimates" not in args:
        raise ValueError("Edited setup requires revised estimates. Supply estimates or propose again.")
    plan["estimates"] = _estimates(args.get("estimates", plan.get("estimates")))
    for k, v in (args.get("edits") or {}).items():         # edits from the pane's cards or the chat
        if k == "loads":
            for i, ld in enumerate(v):
                if i < len(plan["loads"]) and ld.get("force_N") is not None:
                    plan["loads"][i]["force_N"] = _vec(ld["force_N"])
                    plan["loads"][i]["basis"] = "edited by the user"
        elif k == "supports":
            for i, sp in enumerate(v):
                if i < len(plan["supports"]) and sp.get("kind"):
                    plan["supports"][i]["kind"] = sp["kind"]
        elif k == "min_safety_factor":
            plan["criteria"]["min_safety_factor"] = float(v)
        elif k in ("material", "mesh", "gravity", "title"):
            plan[k] = v
    plan["total_load_N"] = [round(sum(ld["force_N"][i] for ld in plan["loads"]), 1) for i in range(3)]
    _validate_setup(plan)
    from .tools import _call, _fea_run, _native_fea_evidence, _fea_source
    out: dict[str, Any] = {"plan_id": pid}
    if _inventor():
        if plan.get("material"):
            _native("set_material", name=plan["material"])
        by_kind: dict[str, list] = {"fixed": [], "frictionless": [], "pin": []}
        for sp in plan["supports"]:
            by_kind.setdefault(sp["kind"], []).append(_inventor_face(sp["ref"]))
        loads = [{"faces": [_inventor_face(f) for f in ld["faces"]], "force": ld["force_N"]} for ld in plan["loads"]]
        mesh = {"coarse": 0.12, "default": 0.0, "fine": 0.05, "very fine": 0.035}.get(str(plan.get("mesh")), 0.0)
        r = _call("native", tool="analyze_stress", args={
            "fixed_faces": by_kind["fixed"] or None, "frictionless_faces": by_kind["frictionless"] or None,
            "pin_faces": by_kind["pin"] or None, "loads": loads, "gravity": plan.get("gravity", False),
            "mesh_fraction": mesh, "study_name": f"{pid} {plan['title']}"[:60], "user_notified": True})
        r = _native_fea_evidence({"name": f"{pid} {plan['title']}"}, r)
        if r.get("path"):
            from .scene import publish_drawing
            publish_drawing(r["path"], f"{pid} · Inventor result", None, kind="result")
        res = {"vm_max_MPa": r.get("max_von_mises_MPa"), "disp_max_mm": r.get("max_displacement_mm"),
               "safety_factor": r.get("safety_factor_yield") or r.get("safety_factor_min"),
               "equilibrium_error_pct": r.get("equilibrium_error_pct"), "evidence_id": r.get("evidence_id"),
               "solver": "Autodesk Inventor Stress Analysis", "report": r.get("report")}
        if args.get("cross_check", True):
            try:
                cc = _cross_check(plan, res)
            except Exception as e:  # the Inventor result stands on its own
                cc = {"error": f"{type(e).__name__}: {e}"}
            if cc:
                res["cross_check"] = cc
                if cc.get("peak_at_mm"):
                    res["peak_at_mm"] = cc["peak_at_mm"]
    else:
        sel = lambda ref: {"near": ref["near"]} if "near" in ref else {"face": ref.get("index")}  # noqa: E731
        fea_args = {"action": "run", "material": plan.get("material") or None, "name": f"{pid} {plan['title']}",
                    "supports": [{"faces": sel(sp["ref"])} for sp in plan["supports"]],
                    "mesh_factor": {"coarse": 1.8, "default": 1.0, "fine": 0.65, "very fine": 0.45}[plan["mesh"]],
                    "loads": [{"type": "force", "faces": [sel(f) for f in ld["faces"]],
                               "force_N": math.sqrt(sum(c * c for c in ld["force_N"])),
                               "direction": ld["force_N"]} for ld in plan["loads"]]}
        if plan.get("gravity"):
            fea_args["loads"].append({"type": "gravity", "direction": [0, 0, -1]})
        r = json.loads(_fea_run(fea_args, _fea_source(fea_args)))
        if not r.get("ok"):
            raise RuntimeError(r.get("error"))
        res = {"vm_max_MPa": r.get("vm_max_MPa"), "disp_max_mm": r.get("disp_max_mm"),
               "safety_factor": r.get("safety_factor_yield"), "evidence_id": r.get("evidence_id"),
               "equilibrium_error_pct": (r.get("equilibrium_error") or 0) * 100, "solver": "CalculiX (FreeCAD FEM)",
               "peak_at_mm": r.get("vm_max_at")}
    crit = plan["criteria"]
    sf = res.get("safety_factor")
    verdict = []
    if sf is not None:
        verdict.append(("PASS" if sf >= crit["min_safety_factor"] else "FAIL") + f": safety factor {sf:g} vs {crit['min_safety_factor']:g} required")
    if crit.get("max_displacement_mm") and res.get("disp_max_mm") is not None:
        ok = res["disp_max_mm"] <= float(crit["max_displacement_mm"])
        verdict.append(("PASS" if ok else "FAIL") + f": deflection {res['disp_max_mm']:g} mm vs {crit['max_displacement_mm']} mm allowed")
    hand = hand_check(plan.get("estimates"), res)
    verdict += [h["line"] for h in hand]
    plan.update(status="ran", result=res, verdict=verdict, hand_check=hand,
                ran=_dt.datetime.now().isoformat(timespec="seconds"))
    setup = {k: copy.deepcopy(v) for k, v in plan.items() if k not in ("result", "verdict", "hand_check", "runs", "ran")}
    run = {"id": uuid.uuid4().hex, "setup": setup, "result": copy.deepcopy(res),
           "verdict": list(verdict), "hand_check": copy.deepcopy(hand), "ran": plan["ran"]}
    run_dir = _store("runs")
    run_dir.mkdir(exist_ok=True)
    with (run_dir / f"{run['id']}.json").open("x", encoding="utf-8") as fh:
        json.dump(run, fh, indent=1)
    plan.setdefault("runs", []).append(run["id"])
    plans[pid] = plan
    _save("plans.json", plans)
    version = checkpoint(f"{pid} analysed", result=res, plan_id=pid)
    out.update(result=res, verdict=verdict, version=version["id"], diff=version.get("diff_text"))
    out["run_id"] = run["id"]
    if any(not h["agrees"] for h in hand):
        out["hand_check_warning"] = ("The prediction check disagreed or could not be completed. Inspect the check lines. Do not quote either "
                                     "number until you have found out why (wrong load path, missing support, units, "
                                     "stress concentration the hand calc ignores) and said so to the user.")
    publish_workbench()
    return out


def _estimates(e) -> dict:
    """Validate predictions before approval and again before solving."""
    if not isinstance(e, dict):
        raise ValueError("estimates are required, with a finite positive quantity and non-empty working, or exempt reason")
    if "exempt" in e:
        if not isinstance(e["exempt"], str) or not e["exempt"].strip():
            raise ValueError("estimates.exempt requires a non-empty reason")
        return {"exempt": e["exempt"].strip()}
    working = e.get("working")
    if not isinstance(working, str) or not working.strip():
        raise ValueError("estimates.working must be non-empty")
    out = {}
    for k in ("vm_max_MPa", "disp_max_mm"):
        if k in e:
            if isinstance(e[k], bool):
                raise ValueError(f"estimates.{k} must be finite and positive")
            try:
                value = float(e[k])
            except (TypeError, ValueError):
                raise ValueError(f"estimates.{k} must be finite and positive") from None
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"estimates.{k} must be finite and positive")
            out[k] = value
    if not out:
        raise ValueError("estimates requires at least one finite positive quantity")
    out["working"] = working.strip()
    if "vm_max_MPa" in out:
        for key in ("location", "stress_definition", "applicability"):
            if not isinstance(e.get(key), str) or not e[key].strip():
                raise ValueError(f"stress estimates require non-empty {key} to identify the comparable stress")
    for key in ("location", "stress_definition", "applicability"):
        if key in e:
            out[key] = e[key]
    return out


HAND_LABEL = {"vm_max_MPa": ("peak stress", "MPa"), "disp_max_mm": ("max deflection", "mm")}


def hand_check(est: dict | None, res: dict) -> list[dict]:
    """Compare the pre-run hand estimate with the solver. Within 2x either way agrees: hand calcs ignore
    stress concentrations and joint flexibility, so they are a sanity check on the load path, not a match."""
    out = []
    if est and est.get("exempt"):
        return [{"agrees": True, "line": "CHECK SKIPPED: " + est["exempt"]}]
    for k, (name, unit) in HAND_LABEL.items():
        if not est or est.get(k) is None:
            continue
        if res.get(k) is None:
            out.append({"quantity": k, "agrees": False, "line": f"CHECK UNAVAILABLE: solver did not return {k}"})
            continue
        if k == "vm_max_MPa" and not (est.get("location") == "global maximum"
                and est.get("stress_definition") == "von Mises" and est.get("applicability")):
            out.append({"quantity": k, "agrees": True, "line": "CHECK SKIPPED: stress comparison requires location='global maximum', stress_definition='von Mises' and applicability explaining use of the global peak"})
            continue
        e, f = float(est[k]), float(res[k])
        ratio = f / e if e else (1.0 if f == 0 else float("inf"))
        agrees = 0.5 <= ratio <= 2.0
        out.append({"quantity": k, "estimate": e, "fea": f, "ratio": round(ratio, 2), "agrees": agrees,
                    "line": (("CHECK OK" if agrees else "CHECK MISMATCH")
                             + f": hand calc {name} {e:g} {unit} vs FEA {f:g} {unit} ({ratio:.2f}x)")})
    return out


# --------------------------------------------------------------------------- design history
def _fmt(v, unit="") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v, 3)
    return f"{v}{unit}"


def _diff(prev: dict | None, cur: dict) -> tuple[list[dict], str]:
    if not prev:
        return [], "first version"
    rows, bits = [], []
    pp, cp = prev.get("parameters") or {}, cur.get("parameters") or {}
    for k in sorted(set(pp) | set(cp)):
        if pp.get(k) != cp.get(k):
            rows.append({"what": k, "was": pp.get(k), "now": cp.get(k)})
            bits.append(f"{k} {_fmt(pp.get(k))} → {_fmt(cp.get(k))}")
    for key, label, unit in (("mass_kg", "mass", " kg"), ("material", "material", "")):
        a, b = prev.get(key), cur.get(key)
        if a != b and b is not None:
            pct = f" ({(b - a) / a * 100:+.0f} %)" if isinstance(a, (int, float)) and isinstance(b, (int, float)) and a else ""
            rows.append({"what": label, "was": a, "now": b})
            bits.append(f"{label} {_fmt(a, unit)} → {_fmt(b, unit)}{pct}")
    pr, cr = prev.get("result") or {}, cur.get("result") or {}
    for key, label, unit in (("vm_max_MPa", "peak stress", " MPa"), ("safety_factor", "SF", ""), ("disp_max_mm", "deflection", " mm")):
        a, b = pr.get(key), cr.get(key)
        if b is not None and a != b:
            pct = f" ({(b - a) / a * 100:+.0f} %)" if isinstance(a, (int, float)) and a else ""
            rows.append({"what": label, "was": a, "now": b})
            bits.append(f"{label} {_fmt(a, unit)} → {_fmt(b, unit)}{pct}")
    return rows, "; ".join(bits) or "no change"


def checkpoint(label: str = "", result: dict | None = None, plan_id: str = "") -> dict:
    """Record the model as a new version: parameters, mass, material, the latest analysis, a STEP copy for the record."""
    hist = _load("history.json", [])
    info = model_info()
    vid = f"V{len(hist) + 1}"
    snap_dir = _store("history")
    snap_dir.mkdir(exist_ok=True)
    step = str(snap_dir / f"{vid}.stp")
    try:
        STATE.backend().call("export", path=step, format="STEP")
    except Exception:
        step = ""
    v = {"id": vid, "label": label or vid, "at": _dt.datetime.now().isoformat(timespec="seconds"),
         "parameters": info.get("parameters") or {}, "mass_kg": info.get("mass_kg"), "material": info.get("material"),
         "size_mm": info.get("size_mm"), "result": result,
         "plan_id": plan_id, "step": step}
    v["diff"], v["diff_text"] = _diff(hist[-1] if hist else None, v)
    hist.append(v)
    _save("history.json", hist)
    publish_workbench()
    return v


# --------------------------------------------------------------------------- risk register, BOM, report
def risk_register() -> list[dict]:
    hist = _load("history.json", [])
    plans = _load("plans.json", {})
    rows = []
    last = hist[-1] if hist else None
    target = 2.0
    if last and last.get("plan_id") in plans:
        target = plans[last["plan_id"]]["criteria"]["min_safety_factor"]
    for r in EVIDENCE.items():
        if r.kind != "fea":
            continue
        sf = r.values.get("safety_factor_yield")
        if sf is not None and sf < target:
            rows.append({"risk": f"{r.data.get('label', r.id)}: safety factor {sf:g} is below the {target:g} required",
                         "severity": "high" if sf < 1 else "medium", "evidence": r.id,
                         "action": "add material where the peak is, or reduce the load path length"})
        for w in r.warnings or []:
            rows.append({"risk": f"{r.data.get('label', r.id)}: {w}", "severity": "medium", "evidence": r.id,
                         "action": "check before relying on the peak value"})
    if len(hist) >= 2:
        a, b = hist[-2].get("result") or {}, hist[-1].get("result") or {}
        if a.get("peak_at_mm") and b.get("peak_at_mm") and math.dist(a["peak_at_mm"], b["peak_at_mm"]) > 20:
            rows.append({"risk": f"the highest-stress spot moved to {b['peak_at_mm']} in {hist[-1]['id']}: this joint is now the "
                                 "weak point", "severity": "medium", "evidence": b.get("evidence_id"),
                         "action": "look at that joint's weld and section"})
    for p in plans.values():
        for a in p.get("assumptions") or []:
            rows.append({"risk": f"assumption ({p['id']}): {a}", "severity": "low", "evidence": "",
                         "action": "confirm it holds for the real part"})
    return rows


def bom() -> list[dict]:
    if _inventor():
        info = STATE.backend().call("status").get("document") or {}
        if info.get("type") == "assembly":
            return _native("bill_of_materials").get("bom", [])
    mi = model_info()
    return [{"item": 1, "qty": 1, "part_number": mi.get("name", ""), "material": mi.get("material", ""),
             "mass_kg": mi.get("mass_kg"), "volume_mm3": mi.get("volume_mm3")}]


def _img(path: str | None) -> str:
    if not path or not os.path.isfile(path) or os.path.getsize(path) > 12_000_000:
        return ""
    with open(path, "rb") as fh:
        return f'<img src="data:image/png;base64,{base64.b64encode(fh.read()).decode()}" alt="{html.escape(os.path.basename(path))}">'


def _table(rows: list[dict], cols: list[str] | None = None) -> str:
    if not rows:
        return "<p class=muted>none</p>"
    cols = cols or list(rows[0])
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(_fmt(r.get(c)))}</td>" for c in cols) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _pdf(html_path: Path) -> str:
    """Print the HTML report to PDF with a locally installed Chrome or Edge (headless). "" if neither is there."""
    import winreg
    exe = ""
    for app in ("chrome.exe", "msedge.exe"):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{app}")
            exe = winreg.QueryValue(key, None)
            if exe and os.path.isfile(exe):
                break
        except OSError:
            continue
    if not exe:
        return ""
    pdf = html_path.with_suffix(".pdf")
    from .paths import child_env
    subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={pdf}",
                    html_path.as_uri()], capture_output=True, timeout=120, env=child_env())
    return str(pdf) if pdf.is_file() else ""


def report(title: str = "") -> dict:
    """A formal report from everything in this session: model, plans and assumptions, results, history, risks, BOM."""
    info = model_info()
    hist = _load("history.json", [])
    plans = _load("plans.json", {})
    risks = risk_register()
    fea = [r for r in EVIDENCE.items() if r.kind == "fea"]
    from .scene import _DRAWING
    pictures = []
    for v in hist[-3:]:
        res = v.get("result") or {}
        rep = res.get("report")
        if rep:
            pictures.append(rep)
    result_png = (_DRAWING.get("v") or {}).get("path") if (_DRAWING.get("v") or {}).get("kind") == "result" else None
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    title = title or f"Analysis report — {info.get('name', 'model')}"
    refs = []
    for p in plans.values():
        for sp in p["supports"]:
            refs.append({"ref": sp["ref"].get("label"), "role": f"{sp['kind']} support ({p['id']})", "where": sp["ref"].get("near") or sp["ref"].get("index")})
        for ld in p["loads"]:
            for f in ld["faces"]:
                refs.append({"ref": f.get("label"), "role": f"{ld['name']} ({p['id']})", "where": f.get("near") or f.get("index")})
    plan_html = ""
    for p in plans.values():
        loads = [{"load": ld["name"], "force N": ld["force_N"], "basis": ld["basis"],
                  "faces": ", ".join(f'<a href="#refs">{html.escape(str(f.get("label")))}</a>' for f in ld["faces"])} for ld in p["loads"]]
        plan_html += (f"<h3>{p['id']}: {html.escape(p['title'])}</h3><p>{html.escape(p.get('intent', ''))}</p>"
                      + _table([{k: v for k, v in ld.items() if k != "faces"} for ld in loads])
                      + "<p><b>Criteria:</b> SF ≥ " + _fmt(p["criteria"]["min_safety_factor"])
                      + (f"; deflection ≤ {p['criteria']['max_displacement_mm']} mm" if p["criteria"].get("max_displacement_mm") else "")
                      + (f"; {html.escape(p['criteria']['objective'])}" if p["criteria"].get("objective") else "") + "</p>"
                      + "<p><b>Assumptions:</b></p><ul>" + "".join(f"<li>{html.escape(a)}</li>" for a in p.get("assumptions", [])) + "</ul>"
                      + ("<p><b>Verdict:</b> " + "; ".join(html.escape(v) for v in p.get("verdict", [])) + "</p>" if p.get("verdict") else ""))
    hist_rows = [{"version": v["id"], "label": v["label"], "mass kg": v.get("mass_kg"),
                  "peak MPa": (v.get("result") or {}).get("vm_max_MPa"), "SF": (v.get("result") or {}).get("safety_factor"),
                  "changes": v.get("diff_text")} for v in hist]
    fea_rows = [{"id": r.id, "case": r.data.get("label"), "summary": r.summary, "checked": "yes" if r.verified else "see warnings"} for r in fea]
    css = ("body{font:14px/1.45 Segoe UI,Arial,sans-serif;margin:32px;color:#111}h1{margin-bottom:0}"
           "h2{border-bottom:2px solid #333;padding-bottom:2px;margin-top:28px}table{border-collapse:collapse;margin:8px 0;width:100%}"
           "td,th{border:1px solid #bbb;padding:4px 6px;text-align:left;vertical-align:top}th{background:#eee}"
           "img{max-width:100%;border:1px solid #ccc;margin:6px 0}.muted{color:#777}.sev-high{color:#b00}")
    model_rows = [{"property": k, "value": v} for k, v in info.items() if k != "parameters"]
    body = (f"<h1>{html.escape(title)}</h1><p class=muted>{stamp} · generated by hermes-shaperotator · every figure is taken from "
            f"the logged analyses ({', '.join(r.id for r in fea) or 'none yet'})</p>"
            "<h2>1. Model</h2>" + _table(model_rows) + "<h3>Parameters</h3>"
            + _table([{"name": k, "value": v} for k, v in (info.get("parameters") or {}).items()])
            + "<h2>2. Loads, supports and assumptions</h2>" + (plan_html or "<p class=muted>no plan recorded</p>")
            + "<h2>3. Results</h2>" + _table(fea_rows) + (_img(result_png) if result_png else "")
            + "<h2>4. Design history</h2>" + _table(hist_rows)
            + "<h2>5. Risk register</h2>" + _table(risks, ["risk", "severity", "evidence", "action"])
            + "<h2>6. Bill of materials</h2>" + _table(bom())
            + '<h2 id="refs">7. Model references</h2>' + _table(refs))
    out_dir = _store("artifacts")
    out_dir.mkdir(exist_ok=True)
    name = f"report-{_dt.datetime.now():%Y%m%d-%H%M%S}"
    p = out_dir / f"{name}.html"
    p.write_text(f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title><style>{css}</style>{body}", encoding="utf-8")
    pdf, pdf_error = "", ""
    try:
        pdf = _pdf(p)
        if not pdf:
            pdf_error = "no Chrome or Edge found to print the PDF; the HTML report is complete"
    except Exception as e:
        pdf_error = f"PDF printing failed: {e}"
    _add_artifact("report", title, str(p), extra={"pdf": pdf})
    out = {"html": str(p), "pdf": pdf, "risks": len(risks), "versions": len(hist)}
    if pdf_error:
        out["pdf_error"] = pdf_error
    return out


def _add_artifact(kind: str, title: str, path: str, extra: dict | None = None) -> None:
    arts = _load("artifacts.json", [])
    arts.append({"kind": kind, "title": title, "path": path, "at": _dt.datetime.now().isoformat(timespec="seconds"), **(extra or {})})
    _save("artifacts.json", arts)
    publish_workbench()


def write_bom_csv() -> dict:
    rows = bom()
    out_dir = _store("artifacts")
    out_dir.mkdir(exist_ok=True)
    p = out_dir / f"bom-{_dt.datetime.now():%Y%m%d-%H%M%S}.csv"
    import csv
    cols = sorted({k for r in rows for k in r})
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    _add_artifact("bom", "Bill of materials", str(p))
    return {"path": str(p), "rows": rows}


def write_risks() -> dict:
    rows = risk_register()
    p = _store("artifacts")
    p.mkdir(exist_ok=True)
    f = p / f"risks-{_dt.datetime.now():%Y%m%d-%H%M%S}.json"
    f.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    _add_artifact("risks", "Risk register", str(f))
    return {"path": str(f), "risks": rows}


# --------------------------------------------------------------------------- snapshots
def snapshot_save(name: str = "") -> dict:
    """Zip everything another Hermes user needs to carry on: the model (native file + STEP), evidence, plans, history,
    artifacts and the pane's scene."""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = _store("artifacts")
    out.mkdir(exist_ok=True)
    from .paths import safe_name
    z = out / f"{safe_name(name, 'snapshot')}-{stamp}.hcad.zip"
    if _inventor():
        try:
            if model_info().get("path"):
                _native("save_document")
        except Exception:
            pass
    info = model_info()
    step = _store(f"snapshot-{stamp}.stp")
    try:
        STATE.backend().call("export", path=str(step), format="STEP")
    except Exception:
        step = None
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for record in _store("runs").glob("*.json"):
            zf.write(record, "runs/" + record.name)
        for fn in ("evidence.jsonl", "plans.json", "history.json", "artifacts.json"):
            if _store(fn).is_file():
                zf.write(_store(fn), fn)
        native = info.get("path")
        files = [native] if native else []
        if _inventor():
            try:
                files = _native("referenced_files").get("files", files)
            except Exception:
                pass
        for fpath in files:
            if fpath and os.path.isfile(fpath):
                zf.write(fpath, "model/" + os.path.basename(fpath))
        if step and step.is_file():
            zf.write(step, "model/model.stp")
        for v in _load("history.json", []):
            if v.get("step") and os.path.isfile(v["step"]):
                zf.write(v["step"], "history/" + os.path.basename(v["step"]))
        try:
            from .scene import live_dir
            if (live_dir() / "scene.json").is_file():
                zf.write(live_dir() / "scene.json", "scene.json")
        except Exception:
            pass
        zf.writestr("snapshot.json", json.dumps({"created": stamp, "backend": STATE.backend_name, "model": info}, default=str))
    _add_artifact("snapshot", f"Snapshot {stamp}", str(z))
    return {"path": str(z), "bytes": z.stat().st_size}


def snapshot_load(path: str) -> dict:
    """Unpack a snapshot into this session: its evidence, plans and history become this session's, and the model opens."""
    from .paths import resolve
    zp = resolve(path) if path else Path("")
    if not zp.is_file():
        raise ValueError(f"no such snapshot: {zp}")
    dest = _store(f"loaded-{zp.stem}")
    dest.mkdir(exist_ok=True)
    with zipfile.ZipFile(zp) as zf:
        for m in zf.namelist():                          # refuse paths that climb out of the folder
            if m.startswith(("/", "\\")) or ".." in Path(m).parts:
                raise ValueError(f"unsafe path in snapshot: {m}")
        zf.extractall(dest)
    for fn in ("plans.json", "history.json", "artifacts.json"):
        if (dest / fn).is_file():
            shutil.copy(dest / fn, _store(fn))
    for record in (dest / "runs").glob("*.json"):
        run_dir = _store("runs")
        run_dir.mkdir(exist_ok=True)
        target = run_dir / record.name
        if target.exists():
            if target.read_bytes() != record.read_bytes():
                raise ValueError(f"run record collision: {record.name}")
        else:
            with target.open("xb") as fh:
                fh.write(record.read_bytes())
    n = 0
    if (dest / "evidence.jsonl").is_file():
        for line in (dest / "evidence.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                EVIDENCE.add(r.get("kind", "fea"), r.get("summary", ""), r.get("values") or {}, verified=r.get("verified", False),
                             warnings=r.get("warnings"), data={**(r.get("data") or {}), "from_snapshot": zp.name},
                             path=STATE.jobs() / "evidence.jsonl")
                n += 1
            except ValueError:
                continue
    model = next((p for p in (dest / "model").glob("*") if p.suffix.lower() in (".ipt", ".iam", ".fcstd")), None) \
        if (dest / "model").is_dir() else None
    model = model or (dest / "model" / "model.stp" if (dest / "model" / "model.stp").is_file() else None)
    opened = ""
    if model is not None:
        try:
            STATE.backend().call("open", path=str(model))
            opened = str(model)
        except Exception as e:
            opened = f"(could not open {model.name}: {e})"
    publish_workbench()
    return {"unpacked_to": str(dest), "evidence_records": n, "opened": opened,
            "versions": len(_load("history.json", [])), "plans": len(_load("plans.json", {}))}


# --------------------------------------------------------------------------- live sync with the CAD window
class LiveSync:
    """Notice edits made by hand in Inventor and refresh the pane (agent edits refresh it through the tool hook)."""

    def __init__(self):
        self._t: threading.Thread | None = None
        self._stop = threading.Event()
        self.last = ""
        self.busy = threading.Event()        # set while an agent tool call is running
        self.calls = 0
        self.lock = threading.Lock()

    def enter(self) -> None:
        with self.lock:
            self.calls += 1
            self.busy.set()

    def leave(self, mutated: bool) -> None:
        with self.lock:
            self.calls = max(0, self.calls - 1)
            if mutated:
                self.last = ""               # the agent's own edit: re-read the version without re-publishing
            if not self.calls:
                self.busy.clear()

    def start(self) -> None:
        if self._t and self._t.is_alive():
            return
        self._stop.clear()
        self._t = threading.Thread(target=self._run, name="hermes-shaperotator-live-sync", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from .scene import publish_geometry
        while not self._stop.wait(3.0):
            if self.busy.is_set() or not _inventor() or STATE.backend_name not in STATE.backends:
                continue
            from .scene import SCENE_LOCK
            with SCENE_LOCK:                  # never refresh while a tool is publishing
                if self.busy.is_set():
                    continue
                try:
                    v = _native("model_version")
                except Exception:
                    continue
                key = f"{v.get('document')}|{v.get('version')}"
                if self.last and key != self.last and v.get("type") in ("part", "assembly"):
                    try:
                        STATE.qa.dirty, STATE.qa.last_issues = True, None     # a hand edit invalidates the drawing check
                        publish_geometry(f"{os.path.basename(str(v.get('document')))} (edited in Inventor)")
                        publish_workbench()
                    except Exception:
                        pass
                self.last = key


LIVE = LiveSync()
