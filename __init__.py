"""hermes-shaperotator — parametric CAD (FreeCAD / Autodesk Inventor) for Hermes Agent."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .core import schemas
from .core.evidence import EVIDENCE
from .core.scene import publish_geometry
from .core.state import BACKENDS, STATE
from .core.tools import HANDLERS
from .core.verify import check as verify_check
from .core.verify import footer as verify_footer

logger = logging.getLogger(__name__)

TOOLSET = "cad"
PREVIEW_MIN_INTERVAL_S = 1.5


_AVAIL_CACHE = {"at": 0.0, "value": False}



def MOUSE_TOOLS(args: dict) -> tuple:
    """forge tools that take over the mouse and keyboard (shown the notice in the approval prompt)."""
    a = args.get("args") or {}
    return ("analyze_stress", "analyze_modes", "result_at_point") + (("study_report",) if a.get("only_this_study") else ())

def _any_backend_available() -> bool:
    """check_fn for every cad_* tool; cached because the registry may call it often."""
    now = time.monotonic()
    if now - _AVAIL_CACHE["at"] < 60:
        return _AVAIL_CACHE["value"]
    value = False
    for name in BACKENDS:
        try:
            if STATE.backend(name).available()[0]:
                value = True
                break
        except Exception:
            continue
    _AVAIL_CACHE.update(at=now, value=value)
    return value


# --------------------------------------------------------------------------- hooks
def _is_pdf_export(tool_name: str, args: Any) -> bool:
    if not isinstance(args, dict):
        return False
    if tool_name == "cad_drawing" and args.get("action") == "export_pdf":
        return True
    if tool_name == "cad_document" and args.get("action") == "export":
        fmt = (args.get("format") or os.path.splitext(args.get("path") or "")[1].lstrip(".")).upper()
        return fmt == "PDF"
    return False


def _native_executes_code(args: Any) -> bool:
    if not isinstance(args, dict):
        return False
    name = str(args.get("tool", ""))
    backend = STATE.backend()
    client = getattr(backend, "client", None)
    if client is None and getattr(backend, "server", None):
        client = backend.forge.client
    description = ""
    mapped = ""
    if client is not None:
        description = str(client.tools.get(name, {}).get("description", ""))
        mapping = getattr(backend, "ops", {}).get("python")
        mapped = mapping if isinstance(mapping, str) else (mapping or {}).get("tool", "")
    import re
    return name == mapped or bool(re.search(r"python|exec|eval|script|shell|run_code|execute", name + " " + description, re.I))


NL = chr(10)


def _pre_tool_call(tool_name: str = "", args: Any = None, **kw: Any):
    if not str(tool_name).startswith("cad_"):
        return None
    from .core.paths import outside_writes
    from .core.workbench import LIVE
    STATE.session_id = str(kw.get("session_id") or "")
    outside, bad = outside_writes(str(tool_name), args)
    if bad:
        return {"action": "block", "message": "hermes-shaperotator: " + "; ".join(bad)}
    LIVE.enter()
    out = _pre_tool_call_inner(tool_name, args, **kw)
    if outside and (out is None or out.get("action") == "approve"):
        note = (f"hermes-shaperotator wants {tool_name} to write outside its job folder ({STATE.jobs()}):" + NL * 2
                + NL.join(outside) + NL * 2 + "Approve only if you asked for the file to go there.")
        out = {"action": "approve", "message": note + (NL * 2 + out["message"] if out else "")}
    return out


def _pre_tool_call_inner(tool_name: str = "", args: Any = None, **_: Any):
    if tool_name == "cad_python" or (tool_name == "cad_native" and _native_executes_code(args)):
        code = str((args or {}).get("code", args) if isinstance(args, dict) else "")
        preview = code if len(code) <= 1200 else code[:1200] + "\n... (truncated)"
        return {"action": "approve",
                "message": f"hermes-shaperotator wants to run Python inside {STATE.backend_name} "
                           f"with your user permissions:\n\n{preview}"}
    if (tool_name == "cad_plan" and isinstance(args, dict) and args.get("action") == "run"
            and STATE.family == "inventor") or (
            tool_name == "cad_native" and isinstance(args, dict) and args.get("tool") in MOUSE_TOOLS(args)):
        return {"action": "approve",
                "message": "hermes-shaperotator wants to run Autodesk Inventor's Stress Analysis. Inventor's Stress Analysis has "
                           "no automation API, so this TAKES OVER YOUR MOUSE AND KEYBOARD for about 30-60 seconds: "
                           "Inventor comes to the front, its camera moves, faces are clicked and dialogs are filled in "
                           "automatically. Save your work in other apps, keep Inventor visible and unobstructed, and "
                           "don't touch the mouse or keyboard until it finishes (a click that would land outside "
                           "Inventor stops the run). Approve to start now."}
    if _is_pdf_export(tool_name, args):
        qa = STATE.qa
        why = qa.export_blocked()          # the handler checks again and consumes a /cad force-export
        if why:
            report = qa.last_report[-1500:] if qa.last_report else ""
            return {"action": "block",
                    "message": f"hermes-shaperotator QA gate: export blocked because {why}. Run "
                               "cad_drawing(action='check'), fix every issue, check again, then export. "
                               "(The user can override once with /cad force-export.)"
                               + (f"\n\nLast report:\n{report}" if report else "")}
    return None


NATIVE_GEOMETRY_TOOLS = {
    "create_part", "create_assembly", "open_document", "activate_document", "undo", "delete_feature", "suppress_feature",
    "extrude", "revolve", "hole", "hole_linear", "fillet", "chamfer", "circular_pattern", "rectangular_pattern", "mirror",
    "shell", "sweep", "loft", "coil", "face_draft", "thicken_face", "split_part", "combine_bodies", "emboss_text",
    "set_parameter", "edit_dimension", "create_standard_part", "parametric_extrusion", "split_faces",
    "link_parameters", "activate_model_state", "set_component_model_state",
    "sheet_metal_face", "flange", "sheet_metal_cut", "set_sheet_metal_thickness",
    "place_component", "add_constraint", "move_component", "pattern_component", "frame_members", "tube_frame", "create_material", "set_appearance",
}


def _geometry_changed(tool_name: str, args: Any) -> bool:
    a = args if isinstance(args, dict) else {}
    if tool_name == "cad_feature":
        return True
    if tool_name == "cad_native":          # read-only tools (list_faces...) must not re-export the whole model
        return a.get("tool") in NATIVE_GEOMETRY_TOOLS
    if tool_name == "cad_document":
        return a.get("action") in ("open", "new_part")
    if tool_name == "cad_parameters":
        return a.get("action") == "set"
    if tool_name == "cad_transaction":
        return a.get("action") == "abort"
    return False


_CAD_SESSIONS: set[str] = set()     # sessions that used cad_* tools (the footer only applies there)
_SESSION_EVIDENCE: dict[str, set[str]] = {}   # session -> evidence ids its own cad_fea calls produced


def _note_evidence(session_id: str, result: str) -> None:
    import json
    try:
        r = json.loads(result)
    except (TypeError, ValueError):
        return
    ids = {r.get("evidence_id")} | {row.get("evidence_id") for row in r.get("runs") or [] if isinstance(row, dict)}
    ids |= set(r.get("evidence_ids") or [])
    ids |= {(r.get("confirmed") or {}).get("evidence_id"), (r.get("result") or {}).get("evidence_id"),
            ((r.get("result") or {}).get("cross_check") or {}).get("evidence_id")}
    _SESSION_EVIDENCE.setdefault(session_id or "_", set()).update(i for i in ids if i)


_PREVIEW = {"next_at": 0.0, "dirty": False}


def _refresh_preview(label: str) -> None:
    """Re-mesh for the pane, spending at most about a fifth of the time on it: a big assembly takes many
    seconds to export and mesh, and must not stall every placement."""
    t0 = time.monotonic()
    publish_geometry(label)
    now = time.monotonic()
    _PREVIEW.update(next_at=now + max(PREVIEW_MIN_INTERVAL_S, (now - t0) * 4), dirty=False)


def _post_tool_call(tool_name: str = "", args: Any = None, result: str = "", session_id: str = "", **_: Any):
    """Keep the Desktop pane's 3D view in step with the model (throttled, best effort)."""
    if str(tool_name).startswith("cad_"):
        _CAD_SESSIONS.add(session_id or "_")
        from .core.workbench import LIVE
        LIVE.leave(_geometry_changed(tool_name, args) or tool_name in ("cad_plan", "cad_document"))
        if STATE.family == "inventor":
            LIVE.start()
    if tool_name in ("cad_fea", "cad_native", "cad_plan") and isinstance(result, str):
        _note_evidence(session_id, result)
    if not isinstance(result, str) or '"ok": true' not in result or not _geometry_changed(tool_name, args):
        return
    if time.monotonic() < _PREVIEW["next_at"]:
        _PREVIEW["dirty"] = True            # shown when the budget allows, or at the end of the reply
        return
    _refresh_preview(STATE.last_delta.split(" | ")[0] if STATE.last_delta else tool_name)


def _pre_llm_call(session_id: str = "", **_: Any):
    """One line of CAD state so the model keeps its bearings after compaction."""
    if not STATE.backends or (session_id and session_id not in _CAD_SESSIONS):
        return None
    parts = [f"backend={STATE.backend_name}"]
    try:
        from .core import standard as _std
        got = _std.chosen(session_id)
        parts.append(f"standard for this chat: {got['standard']} ({got['drawing_units']} drawings)" if got
                     else "standard: not chosen yet - " + _std.ASK)
    except Exception:
        pass
    if STATE.last_delta:
        parts.append(f"last delta: {STATE.last_delta}")
    if STATE.qa.last_issues is not None or STATE.qa.dirty:
        parts.append(f"drawing QA: {'needs check' if STATE.qa.dirty else 'clean'}")
    parts.append("pane references like @Face_12{near:[x,y,z]} or @HighStress_Zone_A{peak:..., near:[x,y,z]} mean the face at "
                 "that point (mm): pass {\"near\": [x, y, z]} to cad_plan, or find_face(x, y, z) on Inventor; the face number "
                 "is the viewer's own, not Inventor's")
    return {"context": "[hermes-shaperotator] " + "; ".join(parts)}


def _transform_llm_output(response_text: str = "", session_id: str = "", **_: Any):
    """Append a verification footer when the reply quotes engineering figures (append-only, keeps streaming)."""
    if _PREVIEW["dirty"]:                    # the model changed since the last refresh: catch the pane up
        try:
            _refresh_preview(STATE.last_delta.split(" | ")[0] if STATE.last_delta else "model")
        except Exception as e:
            logger.debug("hermes-shaperotator: preview refresh failed: %s", e)
    if not response_text or (not STATE.backends and not EVIDENCE.items()):
        return None
    if session_id and session_id not in _CAD_SESSIONS:
        return None          # an unrelated chat in the same long-running process
    if "**hermes-shaperotator verification** — engineering figures in this reply" in response_text:
        return None
    try:
        records = EVIDENCE.items()
        if session_id:      # only this conversation's own analyses back its claims
            mine = _SESSION_EVIDENCE.get(session_id, set())
            records = [r for r in records if r.id in mine]
        foot = verify_footer(verify_check(response_text, records))
    except Exception as e:  # never break the reply
        logger.debug("hermes-shaperotator verification failed: %s", e)
        return None
    return response_text + "\n" + foot if foot else None


def _on_session_finalize(session_id: str = "", **_: Any):
    """Close the CAD workers when the last conversation that used them ends (not on unrelated chats)."""
    if session_id:
        _CAD_SESSIONS.discard(session_id)
        if not (_CAD_SESSIONS - {"_"}):
            try:
                from .core.workbench import LIVE
                LIVE.stop()
            except Exception:
                pass
        _SESSION_EVIDENCE.pop(session_id, None)
        if _CAD_SESSIONS - {"_"}:
            return
    STATE.shutdown()


# --------------------------------------------------------------------------- slash command
def _slash(raw_args: str = "", **_: Any) -> str:
    words = (raw_args or "").split()
    sub = words[0].lower() if words else "status"
    if sub == "status":
        import json
        return json.dumps(json.loads(HANDLERS["cad_status"]({})), indent=1)
    if sub == "backend" and len(words) > 1:
        return HANDLERS["cad_backend"]({"backend": words[1]})
    if sub == "force-export":
        STATE.qa.force_once = True
        return "hermes-shaperotator: the next PDF export will skip the QA gate (once)."
    if sub == "preview":
        return f"Latest preview: {STATE.last_preview or 'none yet'}"
    if sub == "folder":
        return str(STATE.jobs())
    return "usage: /cad [status | backend freecad|inventor | preview | folder | force-export]"


# --------------------------------------------------------------------------- register
def register(ctx) -> None:
    settings: dict[str, Any] = {}
    for key, default in (("backend", "freecad"), ("freecad_path", ""), ("standard", ""),
                         ("job_dir", ""), ("inventor_visible", True), ("mcp_servers", {})):
        try:
            settings[key] = ctx.get_config(key, default)
        except Exception:
            settings[key] = default
    if not isinstance(settings.get("mcp_servers"), dict):
        settings["mcp_servers"] = {}
    STATE.settings.update(settings)
    STATE.backend_name = str(settings.get("backend") or "freecad").lower()
    if STATE.backend_name not in BACKENDS:
        STATE.backend_name = "freecad"

    for schema in schemas.ALL:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema,
                          handler=HANDLERS[schema["name"]], check_fn=_any_backend_available)

    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_command("cad", _slash, description="CAD backend status, switching, previews, QA override",
                         args_hint="[status|backend <name>|preview|folder|force-export]")

    skills_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
    if os.path.isdir(skills_dir):
        for name in sorted(os.listdir(skills_dir)):
            p = os.path.join(skills_dir, name, "SKILL.md")
            if os.path.isfile(p):
                try:
                    ctx.register_skill(name, p)
                except Exception as e:
                    logger.warning("hermes-shaperotator: skill %s not registered: %s", name, e)
