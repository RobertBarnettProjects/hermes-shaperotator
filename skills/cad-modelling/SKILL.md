---
name: cad-modelling
description: Build and edit parametric mechanical parts with the hermes-shaperotator cad_* tools (FreeCAD or Autodesk Inventor), checking every feature's volume delta against a hand calculation. Use for any request to model, modify, export or preview a part.
version: 0.1.0
metadata:
  hermes:
    tags: [cad, freecad, inventor, modelling, mechanical]
    related_skills: [hermes-shaperotator:cad-fea]
---

# Parametric modelling with hermes-shaperotator

All lengths are millimetres.

**Standard:** at the start of a CAD chat call `cad_standard(action="get")`. If nothing is chosen, ask the user
once which standard to work to (the `clarify` tool if you have it; options from `cad_standard(action="list")`:
AS 1100, ISO, ASME, DIN, BS 8888, JIS, GB), then `cad_standard(action="set", standard=...)`. It holds for the
rest of the chat: drawing template and projection angle, general tolerance note, and the fastener and material
series you suggest (e.g. AS/NZS 3678 Grade 350 for AS, S355 for ISO/DIN, ASTM A36 for ASME). Don't ask again.

## Workflow

1. `cad_status` — which backend is active and available, and where job files go.
2. `cad_document(action="new_part", name=...)` (or `open`).
3. One feature per call. Every `cad_feature` returns a **delta** (volume and face count before → after).
   Check it against a hand calculation before the next step:
   - extrude/pad: sketch area × distance
   - through hole / pocket: π/4·d²·depth, or area × depth
   - fillet/chamfer: small volume change of the right sign
   A delta of +0 or the wrong sign means the feature did not do what you meant — fix it now
   (`cad_feature(type="delete_feature")` or `cad_transaction(action="abort")`), don't build on it.
4. Prefer **one closed profile + one extrude** over stacking boxes: an L-bracket is one six-point
   `profile` on the XZ plane extruded once — one delta to check, no fused seams.
5. Holes: `type="hole"` drills along the sketch normal from a point on a sketch plane. For a hole through
   a vertical plate, sketch a `circle` on that plate's plane (XZ / YZ, with `offset`) and extrude with
   `operation="cut"`.
6. Read the sketch result's `axes_global` before extruding: "positive" follows the **sketch** normal
   (XZ's normal is −Y), which matters for coordinates you use later.
7. Look before you report: `cad_inspect(what="render", view="iso")` and open the PNG. In Hermes Desktop the
   CAD pane also shows the live model; the user can click a face there to reference it in chat.
8. `cad_document(action="save")`, and `export` STEP/STL when asked (a 3D-print handoff wants STL).

## Frames and weldments (Inventor)

A welded tube frame (go-kart, trailer, stand) is one call: `cad_native(tool="frame_members", args={"members":
[[x1,y1,x2,y2], ...], "width": 32, "height": 32, "wall": 3})` with member centre lines in the XY plane; members sharing
an end point make a mitred corner. It returns the cut list (saved as an artifact). Then add solid pads/brackets with
`cad_sketch` + `cad_feature` extrudes (`create_sketch` offset = 32 to sit on top of the tubes; negative extrudes for
blocks underneath), then `set_material`.

## Drawings

`cad_drawing`: `new` (standard AS1100 = third angle, or ISO) → `part_sheet` (FreeCAD: the active part, no
path needed; Inventor: the saved .ipt) → `properties` (FreeCAD title block: title, drawing_number, material,
drawn_by) → `check` → `render` and **look at the PNG** → fix → `check` again → `export_pdf`. Export is
blocked until the latest check reports zero issues; don't ask the user to override unless they insist.

## Parameters

Drive dimensions from `cad_parameters` (a "Params" spreadsheet in FreeCAD, user parameters in Inventor)
when the user may want variants: `set` a value and re-check the deltas.

## Things to avoid

- Don't quote mass, stress or deflection you did not compute — the reply is checked against this
  session's analysis log and unbacked figures are flagged.
- `cad_python` runs arbitrary code in the CAD application and asks the user first; use it only when no
  dedicated tool covers the job.
