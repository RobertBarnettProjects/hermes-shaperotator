"""Tool handlers: LLM arguments -> backend ops. Always return a JSON string, never raise."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .backend import BackendError
from .evidence import EVIDENCE
from .materials import resolve as resolve_material
from .scene import publish as publish_scene
from .state import BACKENDS, STATE


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, "backend": STATE.backend_name, **data}, default=str, ensure_ascii=False)


def _err(msg: str, **data: Any) -> str:
    return json.dumps({"ok": False, "backend": STATE.backend_name, "error": msg, **data},
                      default=str, ensure_ascii=False)


def _call(op: str, **kw: Any) -> dict:
    res = STATE.backend().call(op, **kw)
    if isinstance(res, dict) and res.get("delta"):
        STATE.last_delta = res["delta"]
    return res


def _path(p: str | None, default_name: str) -> str:
    """Absolute path; relative or empty paths land in the job folder."""
    if not p:
        return str(STATE.jobs() / default_name)
    pp = Path(os.path.expandvars(os.path.expanduser(p)))
    return str(pp if pp.is_absolute() else STATE.jobs() / pp)


def safe(fn: Callable[..., str]) -> Callable[..., str]:
    def handler(args: dict, **kwargs: Any) -> str:
        try:
            return fn(args or {}, **kwargs)
        except BackendError as e:
            return _err(str(e))
        except (KeyError, TypeError, ValueError) as e:
            return _err(f"bad arguments: {e}")
        except Exception as e:  # last resort: never raise into the agent loop
            return _err(f"{type(e).__name__}: {e}")
    handler.__name__ = fn.__name__
    return handler


# --------------------------------------------------------------------------- tools
@safe
def cad_status(args: dict, **_: Any) -> str:
    avail = {}
    for name in BACKENDS:
        try:
            ok, why = STATE.backend(name).available()
        except Exception as e:
            ok, why = False, f"configuration error: {e}"
        avail[name] = {"available": ok, "detail": why}
    doc = None
    if avail[STATE.backend_name]["available"] and STATE.backend_name in STATE.backends:
        try:
            doc = _call("status")
        except BackendError as e:
            doc = {"error": str(e)}
    return _ok(backends=avail, document=doc, last_delta=STATE.last_delta or None,
               job_folder=str(STATE.jobs()), drawing_qa={"dirty": STATE.qa.dirty, "issues": STATE.qa.last_issues})


@safe
def cad_backend(args: dict, **_: Any) -> str:
    name = args["backend"].lower()
    b = STATE.backend(name)
    ok, why = b.available()
    if not ok:
        return _err(why)
    res = b.call("connect")              # only switch once it actually connected
    STATE.backend_name = name
    STATE.last_delta = ""
    STATE.qa.dirty, STATE.qa.last_issues = True, None
    return _ok(message=f"backend is now {name}", detail=res)


@safe
def cad_document(args: dict, **_: Any) -> str:
    a = args["action"]
    if a == "new_part":
        return _ok(**_call("new_part", name=args.get("name", "Part")))
    if a == "new_assembly":
        return _ok(**_call("new_assembly"))
    if a == "open":
        return _ok(**_call("open", path=_path(args["path"], "")))
    if a == "save":
        ext = ".FCStd" if STATE.family == "freecad" else ".ipt"
        return _ok(**_call("save", path=_path(args.get("path"), f"model{ext}") if args.get("path") or
                           STATE.family == "freecad" else ""))
    if a == "close":
        return _ok(**_call("close", save=bool(args.get("save", False))))
    if a == "export":
        fmt = (args.get("format") or "").upper()
        path = _path(args.get("path"), f"model.{(fmt or 'step').lower()}")
        if (fmt or os.path.splitext(path)[1].lstrip(".").upper()) == "PDF":
            why = STATE.qa.export_blocked()
            if why:
                return _err(f"QA gate: PDF export blocked because {why}. Use cad_drawing(action='check') first.")
            STATE.qa.force_once = False
        return _ok(**_call("export", path=path, format=fmt))
    return _err(f"unknown action {a}")


_SHAPE_OPS = {"rect": "rect", "rectangle": "rect", "circle": "circle", "line": "line",
              "polygon": "polygon", "profile": "profile", "closed_profile": "profile"}


@safe
def cad_sketch(args: dict, **_: Any) -> str:
    out: dict[str, Any] = {}
    if args.get("new", True):
        out["sketch"] = _call("sketch", plane=args.get("plane", "XY"), face_index=args.get("face_index", 0),
                              offset=args.get("offset", 0))
    drawn = []
    for i, s in enumerate(args.get("shapes") or [], 1):
        s = dict(s)
        kind = _SHAPE_OPS.get(str(s.pop("shape", s.pop("op", s.pop("type", "")))).lower())
        if not kind:
            return _err(f"shape {i}: unknown shape (use rect, circle, line, polygon, profile)", **out, drawn=drawn)
        try:
            drawn.append(_call(kind, **s).get("message", kind))
        except BackendError as e:
            return _err(f"shape {i} ({kind}): {e}", **out, drawn=drawn)
    return _ok(**out, drawn=drawn)


@safe
def cad_feature(args: dict, **_: Any) -> str:
    t = args["type"]
    g = args.get
    if t == "extrude":
        r = _call("extrude", distance=g("distance", 10), direction=g("direction", "positive"),
                  operation=g("operation", "join"), sketch_index=g("sketch_index", 0),
                  extent_type=g("extent", "distance"))
    elif t == "revolve":
        r = _call("revolve", angle=g("angle", 360), axis=g("axis", "Y"), sketch_index=g("sketch_index", 0),
                  operation=g("operation", "join"))
    elif t == "hole":
        r = _call("hole", x=g("x", 0), y=g("y", 0), diameter=g("diameter", 10), depth=g("depth", 0),
                  tapped=bool(g("tapped", False)), pitch=g("pitch", 0))
    elif t == "fillet":
        r = _call("fillet", radius=g("radius", 2), edge_indices=g("edges") or [1])
    elif t == "chamfer":
        r = _call("chamfer", distance=g("distance", 2), edge_indices=g("edges") or [1])
    elif t == "pattern_circular":
        r = _call("pattern_circular", feature_index=args["feature_index"], count=g("count", 8),
                  angle=g("angle", 360), axis=g("axis", "Z"))
    elif t == "delete":
        r = _call("delete_feature", indices=args["indices"])
    else:
        return _err(f"unknown feature type {t}")
    return _ok(**r)


@safe
def cad_inspect(args: dict, **_: Any) -> str:
    w = args["what"]
    xyz = {k: args[k] for k in ("x", "y", "z") if args.get(k) is not None}
    if w == "summary":
        return _ok(**_call("inspect"))
    if w == "edges":
        return _ok(**_call("list_edges", min_length=args.get("min_length", 0)))
    if w == "faces":
        return _ok(**_call("list_faces", min_area=args.get("min_area", 0)))
    if w == "find_edge":
        return _ok(**_call("find_edge", tolerance=args.get("tolerance", 0.1), **xyz))
    if w == "find_face":
        return _ok(**_call("find_face", tolerance=args.get("tolerance", 0.5), min_area=args.get("min_area", 100), **xyz))
    if w == "render":
        view = args.get("view", "iso")
        path = _path(None, f"preview-{view}.png")
        r = _call("render", path=path, view=view)
        STATE.last_preview = r.get("path", path)
        return _ok(**r, hint="Open the PNG (read the image) to check the model visually.")
    return _err(f"unknown what={w}")


@safe
def cad_parameters(args: dict, **_: Any) -> str:
    a = args["action"]
    if a == "get":
        return _ok(**_call("get_parameters"))
    if a == "set":
        return _ok(**_call("set_parameter", name=args["name"], expression=str(args["expression"])))
    if a == "add":
        return _ok(**_call("add_parameter", name=args["name"], expression=str(args["expression"]),
                           units=args.get("units", "mm")))
    return _err(f"unknown action {a}")


DRAWING_MUTATING = {"new", "part_sheet", "assembly_sheet", "properties"}


@safe
def cad_drawing(args: dict, **_: Any) -> str:
    a = args["action"]
    g = args.get
    if a == "new":
        r = _call("drawing_new", standard=g("standard") or STATE.settings.get("standard", "AS1100"),
                  sheet_size=g("sheet_size", "A3"), orientation=g("orientation", "landscape"),
                  properties=g("properties") or {})
    elif a == "part_sheet" and STATE.family == "freecad":
        r = _call("drawing_part_sheet", model_path=_path(g("model_path"), "") if g("model_path") else "",
                  views=g("views", "front,top,right,iso"), scale=g("scale", 0), sheet_size=g("sheet_size", "A3"),
                  new_sheet=bool(g("new_sheet", False)))
        if g("properties"):
            r["properties"] = _call("drawing_set_properties", properties=g("properties"))["properties"]
    elif a == "properties":
        if STATE.family != "freecad":
            return _err("on Inventor, pass title-block fields as `properties` to action 'new'")
        r = _call("drawing_set_properties", properties=g("properties") or {})
    elif a in ("part_sheet", "assembly_sheet"):
        if not g("model_path"):
            return _err("model_path is required (save the part first)")
        r = _call(f"drawing_{a}", model_path=_path(args["model_path"], ""), views=g("views", "front,top,right,iso"),
                  scale=g("scale", 0), sheet_size=g("sheet_size", "A3"), up=g("up", "z"),
                  new_sheet=bool(g("new_sheet", False)))
    elif a == "check":
        r = _call("drawing_check")
        STATE.qa.last_issues = r.get("issues")
        STATE.qa.last_report = r.get("report", "")
        STATE.qa.dirty = r.get("issues") != 0
        return _ok(**r, export_allowed=not STATE.qa.dirty)
    elif a == "render":
        path = _path(g("path"), "sheet.png")
        r = _call("drawing_render" if STATE.family == "freecad" else "render", path=path)
        STATE.last_preview = path
        from .scene import publish_drawing
        publish_drawing(r.get("path") or path, os.path.basename(path), STATE.qa.last_issues)
        return _ok(**r, hint="Read the PNG and fix anything unreadable, then run check again.")
    elif a == "export_pdf":
        why = STATE.qa.export_blocked()      # second line of defence behind the pre_tool_call gate
        if why:
            return _err(f"QA gate: export blocked because {why}. Run cad_drawing(action='check'), fix every "
                        "issue, check again, then export.", report=STATE.qa.last_report[-1500:])
        STATE.qa.force_once = False
        r = _call("drawing_export_pdf", path=_path(g("path"), "drawing.pdf"))
    else:
        return _err(f"unknown action {a}")
    if a in DRAWING_MUTATING:
        STATE.qa.dirty = True
        STATE.qa.last_issues = None
    return _ok(**r)


@safe
def cad_transaction(args: dict, **_: Any) -> str:
    return _ok(**_call("transaction", action=args["action"], name=args.get("name", "hermes-shaperotator")))


@safe
def cad_python(args: dict, **_: Any) -> str:
    r = _call("python", code=args["code"])
    if "error" in r:
        return _err(f"python raised: {r.get('error')}", **{k: v for k, v in r.items() if k != "error"})
    return _ok(**r)


def _fea_source(args: dict) -> str:
    """Path the FreeCAD worker should analyse ('' = its own active body)."""
    if args.get("source_path"):
        return _path(args["source_path"], "")
    if STATE.backend_name == "freecad":
        return ""
    # other backends: hand the solid over as STEP
    step = _path(None, f"fea-source-{int(time.time())}.step")
    _call("export", path=step, format="STEP")
    return step


@safe
def cad_fea(args: dict, **_: Any) -> str:
    fc = STATE.backend("freecad")
    ok, why = fc.available()
    if not ok:
        return _err(f"FEA runs on FreeCAD FEM + CalculiX, which is not available: {why}")
    action = args.get("action", "run")
    src = _fea_source(args)
    if action == "faces":
        return _ok(**fc.call("shape_faces", source_path=src), source=src or "active FreeCAD body")
    if not args.get("supports"):
        return _err("give at least one support, e.g. supports=[{\"faces\": {\"plane\": \"x\", \"value\": 0}}]")
    if not args.get("loads"):
        return _err("give at least one load")
    if action == "sweep":
        return _fea_sweep(args)
    return _fea_run(args, src)


def _fea_run(args: dict, src: str) -> str:
    fc = STATE.backend("freecad")
    material = resolve_material(args.get("material"), args.get("thickness_mm"))
    spec = {"material": material, "supports": args["supports"], "loads": args["loads"],
            "mesh_size": args.get("mesh_size"), "mesh_factor": args.get("mesh_factor", 1.0), "convergence": args.get("convergence", True)}
    n = len([r for r in EVIDENCE.items() if r.kind == "fea"]) + 1
    out_dir = str(STATE.jobs() / f"fea-{n:03d}")
    res = fc.call("fea", spec=spec, out_dir=out_dir, source_path=src)
    label = args.get("name") or f"load case {n}"
    rec = EVIDENCE.add(
        "fea",
        f"{label}: von Mises {res['vm_max_MPa']:g} MPa, disp {res['disp_max_mm']:g} mm, "
        f"SF {res.get('safety_factor_yield')}",
        {k: res.get(k) for k in ("vm_max_MPa", "disp_max_mm", "safety_factor_yield", "mass_kg")}
        | {"yield_MPa": material.get("yield_MPa")},
        verified=bool(res.get("verified")), warnings=res.get("warnings"),
        data={"label": label, "source": src or "active FreeCAD body", "material": material["name"]},
        path=STATE.jobs() / "evidence.jsonl",
    )
    STATE.last_preview = res.get("image")
    scene_path = res.pop("scene", None)
    res.pop("scene_error", None)
    publish_scene(scene_path, "fea", f"{rec.id} · {label}",
                  fea={"evidence_id": rec.id, "label": label, "vm_max_MPa": res["vm_max_MPa"],
                       "vm_max_at": res["vm_max_at"], "disp_max_mm": res["disp_max_mm"],
                       "safety_factor_yield": res.get("safety_factor_yield"),
                       "yield_MPa": material.get("yield_MPa"), "material": material["name"],
                       "warnings": res.get("warnings"), "verified": res.get("verified"),
                       "equilibrium_error": res.get("equilibrium_error"), "convergence": res.get("convergence")})
    return _ok(evidence_id=rec.id, label=label, **res,
               hint="Look at the stress plot. Quote figures exactly as given; they are checked against "
                    f"{rec.id}. Resolve every warning before calling the design acceptable.")


def _param_value(name: str):
    for p in _call("get_parameters").get("parameters", []):
        if p.get("name") == name:
            return p.get("value")
    return None


def _fea_sweep(args: dict) -> str:
    """Re-run the analysis for several values of one driving dimension and pick the lightest design that
    meets the target safety factor. Every run is logged as evidence; the winner is re-solved with the
    mesh-convergence check before it is reported."""
    sw = args.get("sweep") or {}
    name = sw.get("parameter") or ""
    values = [float(v) for v in (sw.get("values") or [])]
    if not name or not 2 <= len(values) <= 8:
        return _err("sweep needs {\"parameter\": \"Pad.Length\" (or a user parameter), \"values\": [2 to 8 numbers]} "
                    "— see cad_parameters(action='get') for names")
    target = float(args.get("target_sf") or 2.0)
    original = _param_value(name)
    if original is None:
        return _err(f"parameter {name!r} not found — use cad_parameters(action='get')")
    thickness_follows = bool(sw.get("is_thickness"))
    rows, best = [], None
    try:
        for v in values:
            set_res = _call("set_parameter", name=name, expression=str(v))
            if isinstance(set_res, dict) and set_res.get("error"):
                rows.append({"value": v, "error": set_res["error"]})
                continue
            run_args = {**args, "convergence": False, "name": f"{args.get('name') or 'sweep'} {name}={v:g}"}
            if thickness_follows:
                run_args["thickness_mm"] = v
            r = json.loads(_fea_run(run_args, _fea_source(args)))
            if not r.get("ok"):
                rows.append({"value": v, "error": r.get("error")})
                continue
            row = {"value": v, "evidence_id": r["evidence_id"], "vm_max_MPa": r["vm_max_MPa"],
                   "disp_max_mm": r["disp_max_mm"], "safety_factor_yield": r.get("safety_factor_yield"),
                   "mass_kg": r.get("mass_kg"), "warnings": len(r.get("warnings") or [])}
            rows.append(row)
            sf = row["safety_factor_yield"]
            if sf is not None and sf >= target and (best is None or (row["mass_kg"] or 0) < (best["mass_kg"] or 0)):
                best = row
        final = None
        if best is not None:
            _call("set_parameter", name=name, expression=str(best["value"]))
            run_args = {**args, "convergence": True,
                        "name": f"{args.get('name') or 'sweep'} {name}={best['value']:g} (confirmed)"}
            if thickness_follows:
                run_args["thickness_mm"] = best["value"]
            final = json.loads(_fea_run(run_args, _fea_source(args)))
    finally:
        keep = bool(args.get("keep_best")) and best is not None
        if not keep:
            _call("set_parameter", name=name, expression=str(original))
    result = {"parameter": name, "original_value": original, "target_sf": target, "runs": rows,
              "best": best, "model_left_at": best["value"] if (best is not None and args.get("keep_best")) else original}
    if final and final.get("ok"):
        result["confirmed"] = {k: final.get(k) for k in ("evidence_id", "vm_max_MPa", "disp_max_mm",
                                                         "safety_factor_yield", "mass_kg", "convergence", "warnings")}
    if best is None:
        result["note"] = f"no value reached SF {target:g}; widen the range"
    return _ok(**result, hint="Quote the confirmed run's figures (they carry the convergence check). Sweep rows "
                              "were solved without the re-mesh check.")


def _native_fea_evidence(args: dict, r: dict) -> dict:
    """Log an Inventor Stress Analysis result so the claims checker can back the figures in the reply."""
    if r.get("max_von_mises_MPa") is None:
        return r
    warnings = [r["warning"]] if r.get("warning") else []
    warnings += [f"solver: {w}" for w in r.get("solver_warnings") or []]
    eq = r.get("equilibrium_error_pct")
    if eq is None:
        warnings.append("equilibrium not checked (the report gave no reactions to compare)")
    elif eq > 1.0:
        warnings.append(f"reactions don't balance the loads ({eq:g} % error): check the supports and loads")
    eq_ok = eq is not None and eq <= 1.0
    moment_ok = (r.get("moment_check") or {}).get("ok", True)
    n = len([x for x in EVIDENCE.items() if x.kind == "fea"]) + 1
    label = args.get("name") or f"Inventor load case {n}"
    y = None
    try:
        y = float(str((r.get("material") or {}).get("yield_MPa", "")).split()[0])
    except (ValueError, IndexError):
        pass
    rec = EVIDENCE.add("fea", f"{label}: von Mises {r['max_von_mises_MPa']:g} MPa, disp {r.get('max_displacement_mm')} mm, "
                              f"SF {r.get('safety_factor_yield')} (Inventor Stress Analysis, mesh {r.get('mesh_avg_size_fraction')})",
                       {"vm_max_MPa": r["max_von_mises_MPa"], "disp_max_mm": r.get("max_displacement_mm"),
                        "safety_factor_yield": r.get("safety_factor_yield") or r.get("safety_factor_min"),
                        "yield_MPa": y},
                       verified=eq_ok and moment_ok, warnings=warnings,
                       data={"label": label, "source": "Autodesk Inventor Stress Analysis", "report": r.get("report")},
                       path=STATE.jobs() / "evidence.jsonl")
    r["evidence_id"] = rec.id
    r["evidence_ids"] = [rec.id]
    r["hint"] = (f"Quote figures exactly as given; they are checked against {rec.id}. Inventor's default mesh is "
                 "coarse — run again with mesh_fraction 0.05 and compare before relying on the peak stress.")
    return r


def _native_study_evidence(tool: str, r: dict) -> dict:
    """Log peaks read back from Inventor's studies so the claims checker can back them."""
    rows = {}
    if tool == "read_result_maximum":
        rows[r.get("study", "study")] = {r.get("result", ""): r.get("maximum"), "warnings": [r["warning"]] if r.get("warning") else []}
    else:
        for name, c in (r.get("cases") or {}).items():
            rows[name] = {**(c.get("results") or {}), "warnings": c.get("warnings") or []}
    ids = []
    for name, c in rows.items():
        values = {}
        if c.get("Von Mises Stress") is not None:
            values["vm_max_MPa"] = c["Von Mises Stress"]
        if c.get("Displacement") is not None:
            values["disp_max_mm"] = c["Displacement"]
        if not values:
            continue
        text = ", ".join(f"{k} {v:g}" for k, v in values.items())
        rec = EVIDENCE.add("fea", f"{name}: {text} (read from Inventor Stress Analysis)", values, verified=not c["warnings"],
                           warnings=c["warnings"], data={"label": name, "source": "Autodesk Inventor Stress Analysis"},
                           path=STATE.jobs() / "evidence.jsonl")
        ids.append(rec.id)
    if ids:
        r["evidence_ids"] = ids
    return r


def _native_misc_evidence(tool: str, r: dict) -> dict:
    """Modal frequencies, report summaries, point values and convergence runs are logged too."""
    values, text = {}, ""
    if tool == "study_report" and r.get("summary"):
        sm = r["summary"]
        values = {"vm_max_MPa": (sm.get("Von Mises Stress") or {}).get("max"),
                  "disp_max_mm": (sm.get("Displacement") or {}).get("max"),
                  "safety_factor_yield": (sm.get("Safety Factor") or {}).get("min")}
        text = f"report of {r.get('study')}"
    elif tool == "result_at_point" and r.get("value") is not None:
        key = "vm_max_MPa" if "Mises" in str(r.get("result")) else "disp_max_mm" if "Displacement" in str(r.get("result")) else ""
        if key:
            values = {key: r["value"]}
        text = f"{r.get('result')} at a point in {r.get('study')} (±{r.get('tolerance')})"
    elif tool == "mesh_convergence":
        for run in r.get("runs") or []:
            values = {"vm_max_MPa": run.get("peak")}
        text = f"mesh convergence: change {r.get('last_change_pct')} %"
    elif tool == "analyze_modes" and r.get("frequencies_Hz"):
        values = {"frequency_Hz": r["frequencies_Hz"][0]}
        text = f"modal: first frequency {r['frequencies_Hz'][0]} Hz"
    values = {k: v for k, v in values.items() if v is not None}
    if values:
        rec = EVIDENCE.add("fea", f"{text}: " + ", ".join(f"{k} {v:g}" for k, v in values.items()), values,
                           verified=not r.get("warning"), warnings=[r["warning"]] if r.get("warning") else [],
                           data={"label": text, "source": "Autodesk Inventor Stress Analysis", "tool": tool},
                           path=STATE.jobs() / "evidence.jsonl")
        r["evidence_id"] = rec.id
    return r


NATIVE_REDIRECT = {
    "execute_python": "use cad_python (it asks the user for approval)",
    "export_pdf": "use cad_drawing(action='export_pdf') — PDF export goes through the QA gate",
    "reload_api": "not available inside Hermes",
}


@safe
def cad_native(args: dict, **_: Any) -> str:
    if STATE.backend_name == "freecad":
        return _err("cad_native needs Inventor or an MCP-server backend (cad_backend; cad_status lists them)")
    tool = str(args.get("tool", ""))
    targs = dict(args.get("args") or {})
    if tool in NATIVE_REDIRECT:
        return _err(f"{tool}: {NATIVE_REDIRECT[tool]}")
    if tool == "export_document" and (str(targs.get("format", "")).upper() == "PDF"
                                      or str(targs.get("path", "")).lower().endswith(".pdf")):
        return _err("PDF export: use cad_drawing(action='export_pdf') so the QA gate applies")
    if tool in ("analyze_stress", "analyze_modes", "result_at_point") or (tool == "study_report" and targs.get("only_this_study")):
        # the pre_tool_call hook showed the mouse/keyboard notice in Hermes's approval prompt and the user approved
        targs["user_notified"] = True
    r = _call("native", tool=tool, args=targs)
    if tool == "analyze_stress":
        r = _native_fea_evidence(targs, r)
        try:
            from .workbench import checkpoint
            checkpoint(f"analysed: {targs.get('study_name') or r.get('study') or 'study'}",
                       result={"vm_max_MPa": r.get("max_von_mises_MPa"), "disp_max_mm": r.get("max_displacement_mm"),
                               "safety_factor": r.get("safety_factor_yield") or r.get("safety_factor_min"),
                               "evidence_id": r.get("evidence_id"), "report": r.get("report")})
        except Exception:
            pass
    elif tool in ("read_result_maximum", "run_study_cases"):
        r = _native_study_evidence(tool, r)
    elif tool == "tube_frame" and r.get("cut_list"):
        try:
            from .workbench import _add_artifact, _store
            import csv as _csv
            out = _store("artifacts")
            out.mkdir(exist_ok=True)
            pth = out / f"cut-list-{uuid.uuid4().hex}.csv"
            with open(pth, "x", newline="", encoding="utf-8") as fh:
                w = _csv.writer(fh)
                w.writerow(["member", "name", "section", "length_mm", "end_1", "end_2", "stock_length_mm"])
                for row in r["cut_list"]:
                    w.writerow([row["member"], row["name"], row["section"], row["length_mm"], *row["ends"], row["stock_mm"]])
            _add_artifact("cut_list", f"Cut list ({len(r['cut_list'])} CHS members)", str(pth))
            r["cut_list_csv"] = str(pth)
        except Exception as e:
            r["artifact_error"] = f"cut-list generation failed: {e}"
    elif tool == "frame_members" and r.get("cut_list"):
        try:
            from .workbench import _add_artifact, _store
            import csv as _csv
            out = _store("artifacts")
            out.mkdir(exist_ok=True)
            pth = out / f"cut-list-{uuid.uuid4().hex}.csv"
            with open(pth, "x", newline="", encoding="utf-8") as fh:
                w = _csv.writer(fh)
                w.writerow(["member", "length_mm", "end_angle_1_deg", "end_angle_2_deg", "stock_length_mm"])
                for row in r["cut_list"]:
                    w.writerow([row["member"], row["length_mm"], *row["end_angles_deg"], row["stock_length_mm"]])
            _add_artifact("cut_list", f"Cut list ({len(r['cut_list'])} members)", str(pth))
            r["cut_list_csv"] = str(pth)
        except Exception as e:
            r["artifact_error"] = f"cut-list generation failed: {e}"
    elif tool in ("analyze_modes", "study_report", "result_at_point", "mesh_convergence"):
        r = _native_misc_evidence(tool, r)
    if tool in ("analyze_stress", "show_result", "set_result_colors") and r.get("path"):
        from .scene import publish_drawing
        publish_drawing(r["path"], r.get("result") and f"{r.get('study')} · {r['result']}" or "Inventor Stress Analysis",
                        None, kind="result")
    elif tool == "mass_properties" and r.get("mass_kg") is not None:
        rec = EVIDENCE.add("inspect", f"mass {r['mass_kg']:g} kg ({r.get('material', '')})",
                           {"mass_kg": r["mass_kg"]}, verified=True, data={"tool": tool},
                           path=STATE.jobs() / "evidence.jsonl")
        r["evidence_id"] = rec.id
    if tool in ("check_drawing",):
        STATE.qa.last_issues = r.get("total")
        STATE.qa.last_report = r.get("report", "")
        STATE.qa.dirty = r.get("total") != 0
    elif tool in DRAWING_NATIVE_MUTATING:
        STATE.qa.dirty = True
        STATE.qa.last_issues = None
    return _ok(tool=tool, **r)


DRAWING_NATIVE_MUTATING = {
    "set_dimension_tolerance", "add_surface_finish", "add_hole_table", "add_revision_table", "add_projection_symbol",
    "set_view_component_visibility", "open_document", "activate_document", "set_parameter", "edit_dimension",
    "move_dimension_text", "delete_drawing_dimension", "add_diameter_dimension", "create_drawing", "add_sheet", "add_base_view", "add_projected_view", "add_section_view", "add_detail_view",
    "move_view", "set_view_scale", "add_linear_dimension", "add_diameter_dimension", "auto_dimension_view",
    "add_centermarks", "add_hole_note", "add_general_notes", "add_parts_list", "auto_balloon",
    "set_title_block_field", "place_view_labels", "make_part_sheet", "make_assembly_sheet"}


@safe
def cad_plan(args: dict, **_: Any) -> str:
    from . import workbench as W
    act = args.get("action", "propose")
    if act == "propose":
        p = W.plan_propose(args)
        lines = [f"{p['id']} {p['title']}: {len(p['supports'])} support(s), {len(p['loads'])} load(s), total "
                 f"{p['total_load_N']} N, SF ≥ {p['criteria']['min_safety_factor']:g}"]
        lines += [f"- {ld['name']}: {ld['force_N']} N ({ld['basis']}) on {', '.join(str(f.get('label')) for f in ld['faces'])}"
                  for ld in p["loads"]]
        lines += [f"- {sp['kind']} support: {sp['ref'].get('label')}" for sp in p["supports"]]
        return _ok(plan=p, summary="\n".join(lines),
                   next="Show this to the user and wait for approval or edits. Then call cad_plan(action='run', "
                        f"plan_id='{p['id']}', edits=...).")
    if act == "run":
        r = W.plan_run(args)
        return _ok(**r, hint="Quote figures exactly as given; they are checked against the evidence log.")
    plans = W._load("plans.json", {})
    return _ok(plans=plans if not args.get("plan_id") else plans.get(args["plan_id"]))


@safe
def cad_history(args: dict, **_: Any) -> str:
    from . import workbench as W
    if args.get("action") == "checkpoint":
        v = W.checkpoint(args.get("label") or "")
        return _ok(version=v["id"], changes=v["diff_text"], details=v["diff"])
    hist = W._load("history.json", [])
    return _ok(versions=[{"id": v["id"], "label": v["label"], "at": v["at"], "changes": v.get("diff_text"),
                          "mass_kg": v.get("mass_kg"), "result": v.get("result")} for v in hist])


@safe
def cad_artifact(args: dict, **_: Any) -> str:
    from . import workbench as W
    act = args.get("action")
    if act == "report":
        return _ok(**W.report(args.get("title") or ""))
    if act == "bom":
        return _ok(**W.write_bom_csv())
    if act == "risks":
        return _ok(**W.write_risks())
    if act == "snapshot_save":
        return _ok(**W.snapshot_save(args.get("name") or ""))
    if act == "snapshot_load":
        return _ok(**W.snapshot_load(args.get("path") or ""))
    return _err("action must be report, bom, risks, snapshot_save or snapshot_load")


HANDLERS = {
    "cad_plan": cad_plan, "cad_history": cad_history, "cad_artifact": cad_artifact,
    "cad_native": cad_native,
    "cad_fea": cad_fea,
    "cad_status": cad_status, "cad_backend": cad_backend, "cad_document": cad_document,
    "cad_sketch": cad_sketch, "cad_feature": cad_feature, "cad_inspect": cad_inspect,
    "cad_parameters": cad_parameters, "cad_drawing": cad_drawing, "cad_transaction": cad_transaction,
    "cad_python": cad_python,
}
