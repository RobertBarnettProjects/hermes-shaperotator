"""Which tool arguments name files, and whether the tool writes or only reads them. Clients use this to put writes
outside a sandbox behind an approval, without guessing from argument names. No imports, so a client can load this
file on its own.

Arguments that only become part of a file name (a study or report name) are in NAMES: a client should reduce them to
a plain file name.
"""

WRITES: dict[str, tuple[str, ...]] = {
    "save_document": ("path",),
    "export_document": ("path",),
    "export_pdf": ("path",),
    "screenshot": ("path",),
    "render_sheet": ("path",),
    "bill_of_materials": ("csv_path",),
    "flat_pattern": ("dxf_path",),
    "create_standard_part": ("save_path",),
    "run_study_cases": ("results_path",),
}

READS: dict[str, tuple[str, ...]] = {
    "open_document": ("path",),
    "place_component": ("path",),
    "read_pdf_text": ("path",),
    "read_stress_report": ("path",),
    "set_active_project": ("project",),
    "link_parameters": ("source",),
    "add_base_view": ("model",),
    "make_part_sheet": ("model",),
    "make_assembly_sheet": ("model",),
}

NAMES: dict[str, tuple[str, ...]] = {
    "analyze_stress": ("name", "study_name"),
    "analyze_modes": ("name", "study_name"),
    "study_report": ("name",),
    "create_part": ("name",),
    "create_assembly": ("name",),
    "create_drawing": ("name",),
}

# tools that save a document over its own file when given no path
SAVES_IN_PLACE = {"save_document", "close_document", "run_study_cases"}
