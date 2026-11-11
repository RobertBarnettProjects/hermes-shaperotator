---
name: cad-fea
description: Run and report linear-static stress analysis with hermes-shaperotator's cad_fea (FreeCAD FEM + CalculiX) so that every figure in the reply is backed by a logged, checked run. Use when the user asks whether a part is strong enough, for stresses, deflections, safety factors or a design check.
version: 0.1.0
metadata:
  hermes:
    tags: [fea, stress, engineering, freecad, calculix, verification]
    related_skills: [hermes-shaperotator:cad-modelling]
---

# Verified FEA with cad_fea

`cad_fea` meshes the active part (2nd-order tets), solves with CalculiX, and returns peak von Mises
stress, max displacement, reaction forces, an **equilibrium check**, a **mesh-convergence check** and a
yield **safety factor** for the material. Each run is logged as evidence (FEA-001, FEA-002 …). After you
reply, hermes-shaperotator checks every stress / deflection / safety-factor / mass figure in your text against
that log and appends a verification footer — so quote numbers exactly as returned.

## When the user states a goal in words: plan first

"It has to survive a 3 g landing with a 120 kg driver, SF ≥ 2" → `cad_plan(action="propose", ...)` with supports and
loads (give `mass_kg` + `g_factor` and the tool does the arithmetic) and `min_safety_factor`, **and your own hand calculation in `estimates`** (at least one of peak stress or deflection, with the working covering
beam/plate formula, section modulus and load path). You predict first; the run then checks you against the solver
(`CHECK OK` within 2x, `CHECK MISMATCH` otherwise). On a mismatch, find out why before quoting any number — never
adjust an estimate after the fact. Faces can be
`{"near": [x, y, z]}` or the references the user drops from the pane, e.g. `@Face_12{near:[255,-284,38]}`. Show the
user the summary and wait. They approve in chat or with the pane's button ("Run plan P1 …" with any edits) — then
`cad_plan(action="run", plan_id="P1", edits=...)`. On Inventor this runs Inventor's Stress Analysis (approval prompt,
takes the mouse) and re-solves with CalculiX for the pane's interactive colours; it records a design version. Use
`cad_history` to show what changed between versions and `cad_artifact` for the report, BOM, risk register or snapshot.

## Before solving

1. Know the faces. `cad_fea(action="faces")` lists every face with area, centroid and normal.
   The user may also click a face in the Desktop CAD pane, which inserts its selector into the chat.
2. Choose selectors (geometry-based, so they survive the STEP copy made for Inventor parts):
   - `{"plane": "z", "value": 0}` — planar face(s) lying in z = 0 (best for flat ends and bases)
   - `{"near": [x, y, z]}` — the face closest to a point (hole bores: use a point on the bore axis)
   - `{"normal": [0, 0, -1]}` — the outermost face pointing down; add `"at_min"`/`"at_max"` to pick the
     lowest/highest of the faces pointing that way
   - `{"face": n}` — an index from the faces table
3. Material: AS/NZS 3678-250 is the default; give `thickness_mm` for plate — yield depends on it
   (250 grade: 280 MPa ≤ 8 mm, 260 MPa ≤ 12 mm …). Other grades: 3678-350, 3679.1-300,
   1163-C350L0/C450L0, Aluminium 6061-T6, Stainless 304, or a custom dict.
4. Loads: `force` (TOTAL newtons shared over the faces, with a direction), `pressure` (MPa) and
   `gravity`.

## After solving — check before you quote

- **Did the faces resolve as intended?** The result echoes each support/load with its resolved faces
  (area, centroid). If one is wrong, re-run with an explicit selector. Never "mentally correct" a run;
  quote the run that is right.
- `equilibrium_error` should be ≈ 0 (reactions balance the applied load).
- `convergence.converged` should be true; if not, refine `mesh_size` or judge stress away from the peak.
- Read `warnings`. A peak sitting on a fixed support **that grew on refinement** is a constraint
  singularity, not real stress.
- Open the stress plot PNG (or look at the Desktop pane in Stress mode): the hot spot should be where
  the physics says it should be.
- Do a hand check where one exists (beam: σ = M·c/I, δ = F·L³/3EI) and say whether it agrees.

## Sizing a part (design sweep)

To answer "how thick does it need to be?" use `cad_fea(action="sweep")` instead of guessing:
`sweep={"parameter": "Pad.Length", "values": [4, 6, 8, 10], "is_thickness": true}`, `target_sf=2`, plus the
same supports/loads. Names come from `cad_parameters(action="get")`. Every value is solved and logged;
the lightest passing value is re-solved with the convergence check and returned as `confirmed` — quote
that run. The model is restored to its original value unless `keep_best=true`.

## Reporting

Short: a table of peak von Mises (and where), max displacement, safety factor on yield (with the yield
used), then one or two lines on mesh/equilibrium and caveats (linear-elastic, small deflection; not a
code check or certification). If the safety factor is below 1, say plainly that the part yields under
that load and that results above yield are not physical.

Every `cad_plan` propose requires `estimates` containing at least one finite positive `vm_max_MPa` or `disp_max_mm` and non-empty `working`. An explicit `estimates={"exempt":"reason"}` records and displays `CHECK SKIPPED: reason`. Edited setups require revised estimates. Every stress prediction also requires non-empty `location`, `stress_definition` and `applicability`. The global solver peak can be compared only to a prediction for `location="global maximum"`, `stress_definition="von Mises"` with applicability explaining use of that peak. Other stress predictions are recorded with a skipped comparison.
