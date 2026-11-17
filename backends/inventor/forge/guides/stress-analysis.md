# Stress Analysis

## Before solving
- Hand-check loads, load paths and stability first. Decide which loads are global: a self-equilibrating preload
  (a tie-down pulling a lug up and the payload down) adds nothing to global bending.
- After any geometry change, update the mesh before generating automatic contacts (`update_contacts` does both).
  Automatic contacts use the last mesh: new parts get no contacts and nothing says so.
- Contacts between parts that no longer touch stay in the list. Delete only after measuring that they are apart
  (`update_contacts(delete_stale=true)`), never by part name alone, and review the list of deletions.
- After every contact change, `list_contacts`: each component has every neighbour it should touch, and each type is
  the intended one. A component with no contacts floats.
- Separation contacts make solves 10-20 times slower than bonded ones.
- "Degenerate element" appears only at solve time; a mesh preview doesn't catch it. Suppress-and-solve finds the
  culprit.

## Running
- Never call COM during a solve or a report: Inventor can crash. `solve_state` polls with Windows calls only.
- Entering the Stress Analysis environment is the commonest crash point. Save before, keep runs resumable.
- At most two solves at once on one PC; light work alongside two solves has cost finished results.
- Load values live in `sa_N` parameters. A copied study gets a new block; match by value
  (`run_study_cases` does this).
- A large study copy can take over 8 s to appear; wait about 40 s before retrying.
- `solve_study` skips a study whose inputs haven't changed since its last solve.
- A finished solve can leave stacked "Cancel study?" prompts that stop Inventor responding. Close the Simulate
  dialog as soon as the study reports finished.
- A solve that ends in an error leaves no Results node; check before reading results.

## Reading results
- The colour bar's maximum is the true peak only while "Maximum" is automatic.
- Find where the peak is (`read_result_maximum(locate=true)`). Peaks at sharp corners, point supports and contact
  edges are usually singularities; judge the stress a short way off (`result_at_point`).
- Hand-check every campaign with a beam model. A beam value far below the peak points to a local effect or
  singularity to locate.
- `study_report` checks reactions against applied forces. A solve much faster than its siblings suggests lost
  contacts.
- Inventor's Reaction Forces dialog has crashed it; read reactions from the report.
- Before/after figures: render both from the model's own view with the same orthographic camera, so they overlay.

## Disk
- Every solved study keeps its mesh and results beside the model (`<model>\AIP\FEA`, roughly 0.1-0.5 GB each) until
  the study is deleted. `analysis_storage` reports the size; `purge_studies` deletes superseded studies.
- On a network drive, saving results takes minutes with Inventor unresponsive. Work on a local disk.
