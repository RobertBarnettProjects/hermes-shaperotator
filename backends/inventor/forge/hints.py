"""What to try next when Inventor rejects an operation: a short hint per tool for the usual causes, appended to the
error so the model's next attempt is a fix rather than the same call again."""
from __future__ import annotations

import re

# (tool name pattern, error text pattern or "", hint). First match wins.
HINTS: list[tuple[str, str, str]] = [
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
                                                      "the axis exists (X, Y, Z or a work axis)"),
    (r"^export", r"", "close the file if it is open elsewhere, and check the folder is writable"),
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
