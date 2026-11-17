# 3D-print parts

## Building
- Model Z-up on the XY plane as the bed. Screenshots and poses take `up="z"`.
- Multi-body parts (print-in-place, captive parts): give every cut, join, hole, fillet and chamfer `body=`, and check
  the per-body volume deltas. A cut with no volume change missed its body.
- Clearances between bodies: `body_gap` (two bodies of one part) and `pose_bodies` for a moved arrangement, which
  never changes the saved build pose.
- Batches of parameter, sketch and feature steps: `batch` runs them as one undo step and rolls back on the first
  failure, naming the failed step.

## Exporting
- `export_print_package` writes the native file, STL, 3MF and STEP in millimetres in one go: everything goes to
  temporary files first, so a failing translator never removes the last good file.
- Every mesh is checked: shells (connected pieces), open edges, bed contact (area at the lowest Z), exact extents,
  triangle count. More shells than bodies means parts touch or a body came apart.
- Inventor's 3MF translator may be missing or refuse a document; the package then writes a millimetre 3MF from the
  STL mesh and says so. `export_capabilities` lists the translators this Inventor has.
- Tessellation: `resolution` (coarse, medium, fine) and the surface/normal deviation used are reported, so variants
  can be exported alike.
