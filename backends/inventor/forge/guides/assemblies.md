# Assemblies

## Placing and checking
- `place_component` adds the component to every model state.
- After each change, `check_interference` (fast, volumes in mm^3) or `check_assembly`. An overlap that "looks fine"
  is usually a cut on the wrong side or a part in the wrong place.
- Pick components by exact name (`Runner:1`). A name without `:n` is accepted only when one component has it;
  substrings are never matched (a weld body `FW_Runner_Deck` is not `Runner`).

## Design for fabrication before analysis
- Before modelling an added member for analysis, settle how it is attached and leave room for it: weld size (the
  minimum for the thicker part), toe clearance to edges and other welds, access, standard plate sizes.
- Model added plates the way they will be attached. Faces that touch become bonded contacts, which treats a lapped
  plate as a solid section. Use Separation contacts on lapped faces and carry the load through modelled weld bodies.

## Copying an assembly
- Use `copy_assembly`, not Save As. Save As leaves parts used only in inactive model states linked to the original
  folder, and nothing reports it.
- References are mapped by file name only. A whole-path text replace also rewrites folder names that share the
  prefix.
- Verify from the saved files' `ReferencedFileDescriptors`. `AllReferencedDocuments` lists loaded documents only.
- Never rename or move a folder other copies may still link to: their studies fail with "Invalid constraint".
- Repointing can drop Stress Analysis contacts, which automatic contacts recreate as Bonded. `copy_assembly`
  compares contact types before and after; set them again if they changed.
- Component names inside the copy keep the original names (`K1_Post:1`); only the files are renamed.
- Prune studies before copying (`purge_studies`): each study's mesh and results are copied with the model.

## Saving
- `save_document(list_only=true)` shows every document a save would write. A path equal to the document's own file
  saves in place.
- Saves run in silent mode, so a "save dependents?" prompt can't hold the session.
