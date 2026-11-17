# Modelling parts

## Checking every step
- Every feature reports a volume delta. Compare it with a hand calculation; a feature that "succeeds" with zero
  change missed its body.
- In a multi-body part, give `body=` to cuts, holes, fillets, chamfers and queries. Without it Inventor picks a body
  for you, and a cut can remove material from the wrong one. `list_bodies` gives stable body names and per-body
  volumes.
- Face and edge indices change after every feature. Find them again (`select_edges`, `find_edge`, `find_face`) or
  name faces (`name_face`).
- `find_edge` with `strict=true` refuses a match outside the tolerance instead of returning the nearest edge.

## Sketches and profiles
- Re-profiling an extrusion: clear and redraw its own sketch, then reassign the profile from it. A profile from
  another sketch is rejected.
- `SetDistanceFromFaceExtent` accepts a work plane, not another body's face.
- Sketches built from projected edges go stale when the source geometry moves. Re-project or dimension from
  origin geometry.
- New parts for this kind of work: create them visible and active. `Profiles.AddForSolid` has been refused in an
  invisible part while other documents were open.
- Profiles with arcs and circles: `parametric_profile` (lines, arcs, circles, all parameter-driven), then extrude or
  revolve in the same call.
- Check a sketch before extruding: `check_sketch` lists open ends, gaps, crossings and lines drawn twice.

## Derived parts and parameters
- Derived parts read the source as it is in memory; saving one can save its source. Assembly saves don't re-save
  derived parts.
- Derived parameter tables: `Add2` links nothing without a collection; reassign `LinkedParameters` to pick up new
  parameters.
- Don't model an added plate by thickening an existing one through a shared parameter: other features hang off it
  and move. `set_parameter` reports every body or component whose range box moved; explain each before going on.

## Model states
- `ModelStates.Add` copies the active state. Say explicitly what each new state suppresses
  (`add_model_state(suppress=..., unsuppress=...)`).
- Components go into every state only with MemberEditScope = all members (`place_component` does this).

## Split faces and work geometry
- A sketch is refused as a split tool. `split_faces` uses paths per line, then a hidden work surface extruded from
  the sketch, then work planes.
- For a region, extrude its outline as a hidden surface and split the wanted faces in one feature.
- A plate placed turned over shows its other face: split both faces.

## Weld bodies at any angle
- Sketch the cross-section on a fixed work plane (`WorkPlanes.AddFixed(point, x_axis, y_axis)`) built from the weld
  run's own direction; map 3-D corner points with `Sketch.ModelToSketchSpace`; extrude along the run.
- Check the extrude went the intended way from the body's range box; flip the direction if not.
- Place weld parts at the origin and ground them in every model state.
- Before solving, check each weld body: mass about density x leg^2 / 2 x length, range box where the weld should be,
  legs on both parts, toe clear of other welds and edges, leg at least the minimum for the thicker part.

## Cuts
- A one-sided cut can land on the mirror side of its sketch and still remove the right volume. Sketch on the cut's
  mid-plane and cut symmetrically, then run an interference check of the assembly.
