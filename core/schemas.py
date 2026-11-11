"""Tool schemas the model reads. Units: mm, degrees."""

_NUM = {"type": "number"}
_INT = {"type": "integer"}
_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_PTS = {"type": "array", "items": {"type": "array", "items": _NUM, "minItems": 2, "maxItems": 2}}
_IDX = {"type": "array", "items": _INT}


def _tool(name, description, props=None, required=None):
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []},
    }


CAD_STATUS = _tool(
    "cad_status",
    "Show the active CAD backend (FreeCAD or Inventor), whether each backend is installed, the open "
    "document, and the last geometry delta. Call this first in a CAD task.",
)

CAD_BACKEND = _tool(
    "cad_backend",
    "Switch the CAD backend used by every cad_* tool. 'freecad' = headless FreeCAD (cross-platform). "
    "'inventor' = Autodesk Inventor via COM (Windows, opens the Inventor window). Only switch when the "
    "user asks or the current backend lacks a feature. Any other name is a CAD MCP server configured under "
    "mcp_servers (cad_status lists them) - the plugin is CAD-agnostic: plans, FEA checks, history and "
    "artifacts work the same on every backend.",
    {"backend": {"type": "string", "description": "freecad, inventor, or a configured MCP server name"}},
    ["backend"],
)

CAD_DOCUMENT = _tool(
    "cad_document",
    "Create, open, save, close or export CAD documents. All lengths are millimetres. "
    "export formats: STEP, STL, 3MF, IGES (and PDF for an Inventor drawing). "
    "Paths may be absolute; relative paths go to the session's job folder.",
    {
        "action": {"type": "string", "enum": ["new_part", "new_assembly", "open", "save", "close", "export"]},
        "path": {**_STR, "description": "File path for open/save/export"},
        "format": {**_STR, "description": "Export format (default from the extension)"},
        "name": {**_STR, "description": "Display name for a new part"},
        "save": {**_BOOL, "description": "close: save first"},
    },
    ["action"],
)

CAD_SKETCH = _tool(
    "cad_sketch",
    "Make a sketch and draw geometry in it, in one call. By default creates a NEW sketch on `plane` "
    "(XY, XZ or YZ; `offset` mm moves it along the plane normal: XY→+Z, XZ→+Y, YZ→+X) or on body face "
    "`face_index` (find it with cad_inspect). Set new=false to add to the latest sketch. "
    "Each item in `shapes` is one of:\n"
    '  {"shape":"rect","x":0,"y":0,"width":100,"height":50}   (centred)\n'
    '  {"shape":"circle","x":0,"y":0,"diameter":20,"construction":false}\n'
    '  {"shape":"line","x1":0,"y1":0,"x2":10,"y2":0,"construction":false}\n'
    '  {"shape":"polygon","x":0,"y":0,"radius":12,"sides":6}  (circumscribed radius)\n'
    '  {"shape":"profile","points":[[0,0],[50,0],[25,40]]}     (closed polyline)\n'
    "Sketch coordinates are 2D in the sketch plane; the result tells you how they map to global axes.",
    {
        "plane": {"type": "string", "enum": ["XY", "XZ", "YZ"]},
        "offset": _NUM,
        "face_index": {**_INT, "description": "Sketch on this body face instead of a plane"},
        "new": {**_BOOL, "description": "Create a new sketch (default true)"},
        "shapes": {"type": "array", "items": {"type": "object"}, "description": "Geometry to draw"},
    },
    ["shapes"],
)

CAD_FEATURE = _tool(
    "cad_feature",
    "Add a 3D feature to the active part. Every call returns a delta like "
    "'Pad | V 23497 (+12000 mm³) | F7 E14' — check that volume moved the way you expected.\n"
    "type and its arguments:\n"
    "  extrude: distance, direction (positive|negative|symmetric), operation (join|cut), "
    "extent (distance|all), sketch_index (0 = latest)\n"
    "  revolve: angle (360), axis (X|Y|Z), operation (join|cut), sketch_index — profile on one side of the axis\n"
    "  hole: x, y (on the XY plane), diameter (nominal for tapped, e.g. 16 for M16), depth (0 = through), "
    "tapped (bool), pitch (0 = ISO coarse)\n"
    "  fillet: radius, edges (list of edge indices)\n"
    "  chamfer: distance, edges\n"
    "  pattern_circular: feature_index, count (total incl. original), angle (360), axis (Z)\n"
    "  delete: indices (feature indices from cad_inspect)\n"
    "Find edge/face indices with cad_inspect (find_edge / find_face by coordinates) — never guess.",
    {
        "type": {"type": "string",
                 "enum": ["extrude", "revolve", "hole", "fillet", "chamfer", "pattern_circular", "delete"]},
        "distance": _NUM, "direction": _STR, "operation": _STR, "extent": _STR, "sketch_index": _INT,
        "angle": _NUM, "axis": _STR, "x": _NUM, "y": _NUM, "diameter": _NUM, "depth": _NUM,
        "tapped": _BOOL, "pitch": _NUM, "radius": _NUM, "edges": _IDX, "feature_index": _INT,
        "count": _INT, "indices": _IDX,
    },
    ["type"],
)

CAD_INSPECT = _tool(
    "cad_inspect",
    "Inspect the active model. what:\n"
    "  summary: volume, bbox, face/edge counts, feature tree\n"
    "  edges / faces: indexed tables (min_length / min_area filters)\n"
    "  find_edge: edge index by MIDPOINT x/y/z (give only the coords you know) + tolerance\n"
    "  find_face: face index by CENTROID x/y/z + tolerance\n"
    "  render: PNG preview (view iso|front|top|right); LOOK at the image it returns",
    {
        "what": {"type": "string", "enum": ["summary", "edges", "faces", "find_edge", "find_face", "render"]},
        "x": _NUM, "y": _NUM, "z": _NUM, "tolerance": _NUM, "min_length": _NUM, "min_area": _NUM,
        "view": _STR,
    },
    ["what"],
)

CAD_PARAMETERS = _tool(
    "cad_parameters",
    "Read or change model parameters so designs stay editable. get: list parameters. "
    "set: name + expression (number, '50 mm', or a formula referencing parameters). "
    "add: new user parameter name + expression (+ units, default mm). "
    "FreeCAD: user parameters live in Params.<name>; feature dimensions are '<Feature>.<Property>' "
    "e.g. 'Pad.Length'. Inventor: parameter names as listed (d0, width ...).",
    {
        "action": {"type": "string", "enum": ["get", "set", "add"]},
        "name": _STR, "expression": _STR, "units": _STR,
    },
    ["action"],
)

CAD_DRAWING = _tool(
    "cad_drawing",
    "Engineering drawings (AS 1100 third-angle by default, ISO first-angle on request). "
    "Workflow: new → part_sheet (or assembly_sheet) → check → render (look at it) → fix → check again → "
    "export_pdf. export_pdf is BLOCKED until the latest check reports zero issues.\n"
    "  new: standard (AS1100|ISO), sheet_size (A0-A4), orientation\n"
    "  part_sheet / assembly_sheet: model_path (Inventor: saved .ipt/.iam; FreeCAD: optional — the active part, "
    "or a .FCStd/.step), views ('front,top,right,iso'), scale (0 = auto), sheet_size, up ('z' for parts built here)\n"
    "  properties (FreeCAD): title block fields — title, drawing_number, material, drawn_by, date, sheet\n"
    "  check: QA report (overlaps, missing dimensions, labels, title block)\n"
    "  render: PNG of the active sheet\n"
    "  export_pdf: path",
    {
        "action": {"type": "string", "enum": ["new", "part_sheet", "assembly_sheet", "properties", "check", "render",
                                              "export_pdf"]},
        "standard": _STR, "sheet_size": _STR, "orientation": _STR, "model_path": _STR, "views": _STR,
        "scale": _NUM, "up": _STR, "path": _STR, "new_sheet": _BOOL,
        "properties": {"type": "object", "description": "Title block / iProperties (author, company ...)"},
    },
    ["action"],
)

CAD_TRANSACTION = _tool(
    "cad_transaction",
    "Group several operations so they can be undone together: begin before a risky multi-step build, "
    "commit when it worked, abort to roll everything back to the begin point.",
    {"action": {"type": "string", "enum": ["begin", "commit", "abort"]}, "name": _STR},
    ["action"],
)

CAD_PYTHON = _tool(
    "cad_python",
    "Escape hatch: run Python inside the CAD application when no cad_* tool covers the job. "
    "FreeCAD namespace: App, Part, Sketcher, doc, body, shape, V. Inventor namespace: app, inv, doc, comp, tg "
    "(COM units are CENTIMETRES). Print what you need. Always asks the user for approval first.",
    {"code": _STR},
    ["code"],
)

_SEL = {"description": 'Face selector: {"face": 3} | {"plane": "x", "value": 0} | {"near": [x, y, z]} | '
                       '{"normal": [0, 0, 1], "at_max": true} | a list of these'}

CAD_FEA = _tool(
    "cad_fea",
    "Linear-static stress analysis (FreeCAD FEM + CalculiX, second-order tets) of the active part — works "
    "for both backends (Inventor parts are exported to STEP automatically). Returns peak von Mises stress, "
    "max displacement, reactions, an equilibrium check, a mesh-convergence check, the yield-based safety "
    "factor and a stress plot PNG (look at it). Each run is logged as evidence (FEA-001 ...); figures you "
    "quote to the user are checked against that log, so quote them exactly as returned.\n"
    "Pick faces with geometric selectors (NOT Inventor face indices — the STEP copy is renumbered):\n"
    '  {"plane": "x", "value": 0}  planar face(s) lying in x = 0\n'
    '  {"near": [x, y, z]}         face closest to a point\n'
    '  {"normal": [0, 0, -1]}      outermost face pointing down (the underside)\n'
    '  {"normal": [0, 0, 1], "at_min": true}  of the faces pointing up, the lowest (at_max: highest)\n'
    '  {"face": n}                 index from cad_fea(action="faces")\n'
    "The result echoes the faces each support/load resolved to (area, centroid) - check them before "
    "quoting numbers, and re-run with {\"face\": n} if one is wrong.\n"
    "loads items: {\"type\":\"force\",\"faces\":SEL,\"force_N\":1000,\"direction\":[0,0,-1]} (TOTAL force shared "
    "over the faces) | {\"type\":\"pressure\",\"faces\":SEL,\"pressure_MPa\":0.2} | "
    "{\"type\":\"gravity\",\"direction\":[0,0,-1],\"g_factor\":1}.\n"
    "supports items: {\"faces\": SEL} (fixed). Materials: 'AS/NZS 3678-250' (default), 'AS/NZS 3678-350', "
    "'AS/NZS 3679.1-300', 'AS/NZS 1163-C350L0', 'Aluminium 6061-T6', 'Stainless 304', or "
    "{E_MPa, nu, density_kg_m3, yield_MPa}. Give thickness_mm for plate grades (yield depends on it).\n"
    "Linear elastic only: no contact, no plasticity, no buckling; not a code check.",
    {
        "action": {"type": "string", "enum": ["run", "faces", "sweep"],
                   "description": "run an analysis, list faces, or sweep one dimension to find the lightest "
                                  "design meeting target_sf (each value is solved and logged; the winner is "
                                  "re-solved with the convergence check)"},
        "sweep": {"type": "object", "description": "For action=sweep: {\"parameter\": \"Pad.Length\" (a name from "
                                                  "cad_parameters get), \"values\": [4, 6, 8, 10], \"is_thickness\": "
                                                  "true if it is the plate thickness (sets the yield band)}"},
        "target_sf": {**_NUM, "description": "For action=sweep: required safety factor on yield (default 2)"},
        "keep_best": {**_BOOL, "description": "For action=sweep: leave the model at the best value (default: "
                                              "restore the original)"},
        "material": {"description": "Material name or property object"},
        "thickness_mm": _NUM,
        "supports": {"type": "array", "items": {"type": "object", "properties": {"faces": _SEL}}},
        "loads": {"type": "array", "items": {"type": "object"}},
        "mesh_size": {**_NUM, "description": "Max element size mm (default: automatic)"},
        "convergence": {**_BOOL, "description": "Re-solve on a finer mesh and compare (default true)"},
        "source_path": {**_STR, "description": "Analyse this STEP file instead of the active part"},
        "name": {**_STR, "description": "Short label for this load case"},
    },
    ["action"],
)

CAD_NATIVE = _tool(
    "cad_native",
    "Inventor or MCP-server backends: call any tool of the CAD server directly. Inventor (inventor-forge): welded round-tube (CHS) frames in 3D from centre lines with a cut list (tube_frame: members [[x1,y1,z1,x2,y2,z2],...] or {line, od, wall, name}; use real AS/NZS 1163 sizes such as 33.7x2.6, 26.9x2.3), grades the library lacks (create_material: name, base, yield_MPa, ultimate_MPa), colours (set_appearance), welded box-section frames from centre lines with a "
    "cut list (frame_members: members [[x1,y1,x2,y2],...], width, height, wall), sheet metal (set_sheet_metal_thickness, "
    "sheet_metal_face, flange, sheet_metal_cut, flat_pattern + DXF), assemblies (place_component, add_constraint, "
    "inspect_assembly, check_interference, bill_of_materials), materials (list_materials, set_material), "
    "mass_properties, measure, iProperties, standard fasteners (create_standard_part), sketch constraints and "
    "dimensions, more features (rectangular_pattern, mirror, shell, thread, sweep, loft, coil, face_draft, "
    "thicken_face, split_part, combine_bodies, emboss_text, work_plane, hole with points/tap/cbore/csink), drawing "
    "detail (add_section_view, add_detail_view, add_linear_dimension, set_dimension_tolerance, add_surface_finish, "
    "add_hole_note, add_hole_table, add_revision_table, add_parts_list, auto_balloon...), and analysis in Inventor's own "
    "Stress Analysis (analyze_stress, analyze_modes — these take over the user's mouse and keyboard and ask for "
    "approval first). tool='list_tools' lists them; mm / degrees / N / MPa.",
    {"tool": _STR, "args": {"type": "object", "description": "Arguments for that tool"}},
    ["tool"],
)

_REF = {"description": 'A face: an index (Inventor part), "Component:1/5" (Inventor assembly), {"near": [x, y, z]} (mm), '
                       'or a pane reference like "@Face_12{near:[255,-284,38]}"'}

CAD_PLAN = _tool(
    "cad_plan",
    "Conversational analysis set-up. When the user describes what a part must survive in words, turn it into a plan "
    "and PROPOSE it (action=propose): supports, loads (give mass_kg and g_factor and the tool does the arithmetic, "
    "or force [Fx,Fy,Fz] N), material, mesh, pass criteria. The plan appears as editable cards in the CAD pane and its "
    "summary comes back to you: show it to the user in a few lines and ask them to approve or change it. Only when "
    "they approve (in chat, or with the pane's Approve button, which sends 'Run plan P1' plus any edits) call "
    "action=run with plan_id and edits. A run analyses with the active backend (Inventor's own Stress Analysis takes "
    "over the mouse after an approval prompt), checks the criteria, and records a new design version.",
    {"action": {"type": "string", "enum": ["propose", "run", "show"]},
     "plan_id": _STR, "title": _STR, "intent": {**_STR, "description": "The user's goal in their words"},
     "supports": {"type": "array", "items": {"type": "object", "properties": {
         "face": _REF, "kind": {"type": "string", "enum": ["fixed", "frictionless", "pin"]}, "why": _STR}}},
     "loads": {"type": "array", "items": {"type": "object", "properties": {
         "name": _STR, "faces": {"type": "array", "items": _REF}, "force": {"type": "array", "items": _NUM},
         "mass_kg": _NUM, "g_factor": _NUM, "direction": {"type": "array", "items": _NUM}, "why": _STR}}},
     "gravity": _BOOL, "material": _STR,
     "mesh": {"type": "string", "enum": ["coarse", "default", "fine", "very fine"]},
     "min_safety_factor": _NUM, "max_displacement_mm": _NUM, "objective": _STR,
     "assumptions": {"type": "array", "items": _STR},
     "estimates": {"type": "object", "description":
         "REQUIRED for propose: your own hand calculation BEFORE the solve, e.g. {\"disp_max_mm\": 0.5, \"working\": \"cantilever span 1000 mm, FL^3/(3EI) = ...\"}. "
         "The run compares it with the solver: within 2x is CHECK OK, otherwise CHECK MISMATCH and you must "
         "explain the difference before quoting any number. At least one finite positive quantity and non-empty working are required. "
         "Alternatively estimates={exempt: reason} records CHECK SKIPPED. Stress estimates require location, stress_definition and applicability. "
         "Comparison with the global result requires location=global maximum, "
         "stress_definition=von Mises and applicability explaining why the global peak corresponds to the prediction.",
         "properties": {"vm_max_MPa": _NUM, "disp_max_mm": _NUM, "working": _STR, "exempt": _STR, "location": _STR,
                        "stress_definition": _STR, "applicability": _STR}},
     "rerun": {**_BOOL, "description": "For run: run a plan that has already run"},
     "cross_check": {**_BOOL, "description": "Inventor runs: also solve with CalculiX for the pane's interactive colours and a "
                                            "second opinion (default true)"},
     "edits": {"type": "object", "description": "For run: changes from the user, e.g. {\"loads\": [{\"force_N\": [0,0,-5000]}], "
                                                "\"min_safety_factor\": 2.5}"}},
    ["action"],
)

CAD_HISTORY = _tool(
    "cad_history",
    "Design versions with diffs. checkpoint records the model now (parameters, mass, material, latest analysis, a STEP "
    "copy) as V1, V2... and returns what changed since the last version, e.g. 'wall 3 → 4 mm; mass 15.9 → 18.2 kg "
    "(+14 %); peak stress 160 → 120 MPa (-25 %)'. Plans record a version automatically when they run. list shows the "
    "timeline (also in the CAD pane).",
    {"action": {"type": "string", "enum": ["checkpoint", "list"]}, "label": _STR},
    ["action"],
)

CAD_ARTIFACT = _tool(
    "cad_artifact",
    "Engineering deliverables from this session, kept in the job folder and listed in the CAD pane: report (formal "
    "HTML + PDF: model, loads and assumptions, results with their evidence ids, design history, risk register, BOM, "
    "model references), bom (CSV with mass), risks (risk register: low safety factors, solver warnings, a weak point "
    "that moved between versions, assumptions to confirm), snapshot_save (a .hcad.zip another Hermes user can load: "
    "model, evidence, plans, history), snapshot_load (path).",
    {"action": {"type": "string", "enum": ["report", "bom", "risks", "snapshot_save", "snapshot_load"]},
     "title": _STR, "path": _STR, "name": _STR},
    ["action"],
)

ALL = [CAD_FEA, CAD_STATUS, CAD_BACKEND, CAD_DOCUMENT, CAD_SKETCH, CAD_FEATURE, CAD_INSPECT, CAD_PARAMETERS,
       CAD_DRAWING, CAD_TRANSACTION, CAD_PYTHON, CAD_NATIVE, CAD_PLAN, CAD_HISTORY, CAD_ARTIFACT]
