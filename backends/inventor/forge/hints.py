"""What to try next when Inventor rejects an operation: a short hint per tool for the usual causes, appended to the
error so the model's next attempt is a fix rather than the same call again."""
from __future__ import annotations

import re

# (tool name pattern, error text pattern or "", hint). First match wins.
HINTS: list[tuple[str, str, str]] = [
    (r"", r"e_invalidarg|invalid argument|-2147024809", "an optional argument may need to be missing rather than None, "
                                                       "or an array typed (prompt strings: VT_ARRAY | VT_BSTR); "
                                                       "guide('session') has the COM rules"),
    (r"^(extrude|parametric_extrusion|parametric_profile)$", r"addforsolid|profile",
     "a part that is invisible while other documents are open can refuse profiles: make it visible and active "
     "(activate_document); check_sketch lists open ends"),
    (r"^split_faces$", r"", "a sketch can't be the tool: split_faces(tool='surface') extrudes it as a hidden surface, "
                            "tool='plane' uses work planes"),
    (r"^(add_title_block|set_sheet_format|set_prompted_fields)$", r"",
     "prompt names must be unique in the definition; values go in the definition's prompt order"),
    (r"^(solve_study|run_study_cases|mesh_convergence)$", r"degenerate|jacobian",
     "suppress parts one at a time and solve to find the culprit; look for slivers and near-tangent contacts"),
    (r"^(update_contacts|set_contact_type|list_contacts)$", r"",
     "update the mesh first (update_contacts) and list contacts back; guide('stress-analysis')"),
    (r"^copy_assembly$", r"", "the source must be saved and closed cleanly; check its missing references first "
                              "(open_document lists them)"),
    (r"^fillet$", r"", "the radius may be too big for the faces next to that edge, or the edge numbers changed after "
                      "the last feature: re-read them with list_edges / find_edge and try a smaller radius first"),
    (r"^chamfer$", r"", "the distance may be bigger than the faces it cuts into, or the edge numbers changed: re-read "
                        "them with list_edges / find_edge; distance+angle chamfers also need face_index"),
    (r"^hole", r"", "the hole centre must lie on the sketch plane or face it starts from, and the depth must reach into "
                    "material: check with find_face, or use a through hole (depth 0)"),
    (r"^(extrude|revolve)$", r"profile", "the sketch isn't closed: check_sketch lists open ends and gaps"),
    (r"^extrude$", r"", "a cut needs material in its direction: try direction='negative' or 'both', or through_all; "
                        "a join needs to touch the body"),
    (r"^revolve$", r"", "the axis must not cross the profile: move the profile to one side of the axis"),
    (r"^(sweep|loft)$", r"", "profiles must be closed and the path must not cross itself or turn tighter than the "
                             "profile size; check_sketch on each sketch"),
    (r"^shell$", r"", "the wall may be thicker than the thinnest part of the body, or a fillet radius is smaller than "
                      "the wall: try a thinner wall"),
    (r"^add_constraint$", r"", "the two entities may not be able to meet that way (parallel faces for mate/flush, round "
                               "faces for insert); check the names with inspect_assembly(faces_of=...), or flip "
                               "opposed"),
    (r"^place_component$", r"", "the file may be outside the active project's search paths: open it once with "
                                "open_document, or check the path"),
    (r"^(set_parameter|edit_dimension|add_parameter)$", r"", "give the value with units Inventor knows ('25 mm', "
                                                             "'30 deg') and only refer to parameters that exist "
                                                             "(get_parameters)"),
    (r"^(flange|sheet_metal_face|sheet_metal_cut)$", r"", "sheet-metal features need a sheet-metal part "
                                                         "(create_part(sheet_metal=true)) and an edge of the existing "
                                                         "face for flanges"),
    (r"^circular_pattern$|^rectangular_pattern$", r"", "pattern the feature by its name from list_features, and check "
                                                      "the axis exists (X, Y, Z or a work axis); a pattern of a "
                                                      "feature in a multi-body part may need its body: draw every "
                                                      "instance in one sketch and cut once with body="),
    (r"^export", r"3mf", "this Inventor's 3MF translator refused the document: export_print_package writes a "
                         "millimetre 3MF from the STL mesh instead; export_capabilities lists the translators"),
    (r"^export", r"", "export_capabilities lists the translators this Inventor has; close the file if it is open "
                      "elsewhere, and check the folder is writable"),
    (r"", r"busy|refused", "a dialog may be open in Inventor: pending_call shows it"),
]


def hint_for(tool: str, message: str) -> str:
    msg = message.lower()
    for tool_rx, msg_rx, hint in HINTS:
        if tool_rx and not re.search(tool_rx, tool):
            continue
        if msg_rx and not re.search(msg_rx, msg):
            continue
        return hint
    return ""
