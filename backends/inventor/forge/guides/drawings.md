# Drawings

## Building sheets
- Build each sheet type from one script or one call sequence and rebuild it end to end after every change; don't
  hand-patch.
- Look at a picture of every build (`render_sheet`), zoomed where it matters, before reporting.
- Lay out by measurement: place, read the range box, then move.
- Choose scales by rule from the model size, so the same steps fit other sizes.
- Pick geometry by exact component name from the view's curves, not by guessed coordinates.
- Ask for house style early: view titles, scales, sheets, which views.

## Company templates
- A template that draws its title block in the border has no title block object: tell the tools where it is with
  `set_title_area`.
- A drawing made from a template doesn't carry the template's prompted values: `set_prompted_fields` on every sheet.
- Prompt values for `AddTitleBlock` / `AddBorder` must be a typed string array (`set_sheet_format` does this). Prompt
  names must be unique. Inventor writes them as `<Prompt ReadOnlyUniqueID='n'>`.
- Author and Designer iProperties fill in with the Windows user name; use prompted entries for initials.
- A title block is placed 10 mm in from the corner, so a full-sheet layout belongs in a border definition.

## Views and annotation
- Dimensions to sloped or chamfered ends show fractions (195.88); `set_dimension_precision` rounds them.
- Section cut lines run 10-16 mm past the view so the letters clear the part. Put cuts off-centre so they don't
  collide with a centred view title.
- Notes keep clear of view labels, sketched symbols and section letters; check every title and leader anyway.
- End views of fabricated assemblies hide internal members: use a section plus an enlarged detail.
- Hide analysis-only bodies (weld bodies, ground pads) in views: `hide_components_in_views`.
- Before calling a stray shape a hidden-component failure, list which components the view actually draws.
- `AddDetailView` with a circular fence takes a radius number; pass omitted optional arguments as missing, not None.

## Rebuilding edited drawings
- People edit generated drawings. Before a rebuild, `supersede_drawing(path)`: it records where notes and symbols
  sit and moves the old file into a Superseded folder. After the rebuild, `apply_positions`, then save to the
  original path.
- Never put sample values into the file's iProperties; only in memory, and only when asked.
