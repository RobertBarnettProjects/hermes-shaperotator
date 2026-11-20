# ShapeRotator for Hermes (`hermes-shaperotator`) — verified engineering design

Describe what a part has to survive; Hermes designs it in your CAD, analyses it, and **flags any engineering
figure in its reply that no analysis backs**.

![Click a face of the kart in the ShapeRotator pane and ask Hermes about it](docs/click-a-face.gif)

*Click a tube of the go-kart frame in the ShapeRotator pane; `@Face_25` lands in the chat and Hermes answers from the
model's real geometry (26.9 mm OD, 2.3 mm wall).*

![Hermes → ShapeRotator plugin → MCP → inventor-forge (Autodesk Inventor), FreeCAD, or any CAD MCP server](docs/architecture.png)

ShapeRotator is the workflow (plans, hand-calc checks, the evidence log, drawing QA, the 3D pane). The CAD program
behind it is swappable: FreeCAD is built in, Autodesk Inventor runs through
[inventor-forge](https://github.com/RobertBarnettProjects/inventor-forge) (an MCP server installed on first use, with
your approval), and any other CAD MCP server can be added in config.

- **Parametric CAD** through one toolset: FreeCAD (headless, cross-platform; parts) or Autodesk Inventor (Windows,
  COM; parts, sheet metal, assemblies, drawings). Every feature returns a volume/topology delta, so the agent checks its
  own work against a hand calculation.
- **Conversational analysis (`cad_plan`):** "survive a 3 g landing with a 120 kg driver, SF ≥ 2" becomes a plan —
  supports, loads with the arithmetic shown (120 kg × 3 g × 9.81 = 3,530 N), material, mesh, pass criteria — shown as
  editable cards in the CAD pane. Nothing runs until you approve (in chat or with the card's button).
- **Predict, then check:** the plan carries the agent's own hand calculation (formula, section modulus, load path),
  written before the solve. After the run it is compared with the solver — `CHECK OK` within 2×, `CHECK MISMATCH`
  otherwise, and then the agent must explain the difference before quoting any number.
- **Two solvers:** Inventor plans run in Inventor's own Stress Analysis (driven with the mouse after you approve,
  because it has no API) and are re-solved with CalculiX, which gives the pane an interactive stress field and a
  second opinion. The FreeCAD route (FreeCAD FEM + Gmsh 2nd-order tets + CalculiX) needs no mouse; its runs report an
  equilibrium check, an optional mesh-convergence re-solve and a yield safety factor (AS/NZS steels or others).
- **Design sweep:** `cad_fea(action="sweep")` solves several values of one dimension and picks the lightest that meets
  the target SF on its first-pass mesh, then re-solves that design with the convergence check (check the confirmed
  figures it returns).
- **Design history (`cad_history`):** plan runs and Inventor analyses record V1, V2… automatically (and
  `cad_history(action="checkpoint")` records any state), each with a diff — "wall 3 → 2.5 mm; mass
  15.9 → 13.6 kg (−14 %); SF 3.0 → 2.3".
- **Deliverables (`cad_artifact`):** a formal HTML + PDF report (model, loads and assumptions, results with their
  evidence ids, history, risk register, BOM, model references), BOM CSV with mass, a risk register, and a `.hcad.zip`
  snapshot another Hermes user can load and continue from.
- **Evidence log + claims checker:** analyses are logged (FEA-001 …). After each reply, stress, deflection,
  safety-factor and computed-mass figures written in the usual forms ("116 MPa", "SF 2.1", "0.35 mm deflection") are
  matched against the log and a footer is appended: ✅ matches · ⚠️ matches a run with warnings or failed checks ·
  ❌ contradicts the analysis · ❓ not backed by any analysis. It flags; it doesn't rewrite the reply.
- **Standards, chosen per chat:** the agent asks once at the start (AS 1100, ISO, ASME, DIN, BS 8888, JIS or GB)
  and keeps the answer for the rest of the chat; drawings, fastener and material suggestions follow it.
- **Drawings:** sheets to the chat's standard (template, projection angle) with title block and projection symbol on both backends; a QA
  check runs on the sheet and PDF export waits for it to be clean (override once with `/cad force-export`).
- **Inventor automation:** the Inventor backend is [inventor-forge](https://github.com/RobertBarnettProjects/inventor-forge),
  an MCP server of its own, downloaded on first use once you approve it; `cad_native` reaches its 200+ tools (sheet metal and flat patterns,
  assemblies, interference, BOM, materials, mass properties, section/detail views, Stress Analysis studies, load-case
  sweeps without the mouse once a study exists), including welded round-tube frames on 3D centre lines with a cut
  list (`tube_frame`, real AS/NZS 1163 CHS sizes), named grades the library lacks (`create_material`, e.g. C250L0)
  and appearances.
- **Any CAD program:** the workbench sits on a backend-neutral contract; a CAD MCP server is added in config and
  swapped with `cad_backend` (see below).
- **Hermes Desktop pane:** live 3D view (refreshes after the agent's edits, and after edits you make by hand in
  Inventor), hover tooltips, **click a face to drop `@Face_12{near:[x,y,z]}` into the chat**, ctrl-click / lasso to
  select several, assembly parts coloured by kind with a Parts list to hide components ("Frame only" before an
  analysis), Inventor-style load arrows (with the force) and support symbols, stress colours with palette, bands,
  range, a balanced/log scale so a peak doesn't paint everything blue, a Yield button and animated deflection, "zones above X"
  that become `@HighStress_Zone_A` references, a probe with "Ask Hermes about this spot", model info, plan cards,
  design history, artifacts, the evidence log, and drawing / Inventor-result previews. The 3D view meshes models with
  FreeCAD, so it needs FreeCAD installed even when you work in Inventor.

![Hand calculation against Inventor's solver on the kart frame: a disagreement it has to explain, then agreement](docs/numbers-check.png)

*The kart frame under a 3 g landing in Inventor. The first model fixed the bearing bores, and the solver's 485 MPa
peak disagreed with the 140 MPa hand calculation; the agent had to explain the singularity before quoting either.
With the bearings as pins, deflection agrees within 16 %.*

![Stress map of the kart frame in the ShapeRotator pane, with the loads drawn as arrows](docs/kart-stress-pane.jpg)

*The same load case re-solved with CalculiX, in the pane: stress colours, the 3,530 N driver load and the 588 N
battery-pack load drawn on the faces they act on, and the supports as green symbols.*

| | |
|---|---|
| ![Bracket detail drawing with section A-A and detail B](docs/bracket-detail-drawing.jpg) | ![Go-kart general arrangement with parts list and balloons](docs/kart-ga-drawing.jpg) |
| A detail drawing from one sentence: section A-A, detail B, hole notes, AS 1100, QA clean before the PDF | The kart's general arrangement: four views, 23-item parts list, balloons |

| | |
|---|---|
| ![FreeCAD-backend drawing](docs/freecad-drawing-sample.png) | ![Design sweep with the verification footer](docs/cad-sweep-desktop.jpg) |
| The same drawing workflow on the FreeCAD backend | A design sweep, with the verification footer on the reply |

## Install

```bash
hermes plugins install RobertBarnettProjects/hermes-shaperotator
hermes plugins enable hermes-shaperotator
```

Then in Hermes Desktop: **Capabilities → Plugins → hermes-shaperotator → Desktop** switch on, to get the pane.

Requirements:

| | |
|---|---|
| FreeCAD 1.0+ | required for FEA and the default backend. Auto-detected (Windows `%LOCALAPPDATA%\Programs\FreeCAD *`, `Program Files`, macOS app bundle, Linux `freecadcmd`); or set `freecad_path`. Ships Gmsh and CalculiX. |
| Autodesk Inventor 2024+ | optional, Windows only. Its add-on, inventor-forge, is downloaded on first use: see below. |

**First use.** The first CAD chat finds the CAD programs on the computer and the agent asks which to use; the
answer is kept. A program that needs an add-on (Inventor needs inventor-forge) gets it through
`cad_backend(action='install')`: Hermes's approval prompt names the package, its source (a pinned commit of its
repository) and where it goes. It is installed with pip into its own Python environment under the plugin's data
folder (`servers/<name>`), so nothing in Hermes's Python changes, and deleting that folder removes it. A plugin
release that moves the pin offers the update the same way. The known programs are listed in
`backends/known.json`.

Settings (Desktop: **Capabilities → Plugins → hermes-shaperotator**, or `plugins.entries.hermes-shaperotator.settings` in
`config.yaml`): `backend` (`freecad`/`inventor`; leave it empty and the agent asks on first use), `freecad_path`, `standard` (optional: fixes the standard for every chat, e.g. `AS1100`, `ISO`, `ASME`, `DIN`,
`BS8888`, `JIS`, `GB`; leave it empty and the agent asks once per chat), `job_dir`, `inventor_visible`,
`server_sources` (development: install a backend's server from a local checkout instead of its pinned commit, e.g.
`{inventor: C:/src/inventor-forge}`).

## Any CAD program: swap the MCP server

The workbench (plans, FEA verification, the viewport, history and artifacts) sits on a small
backend-neutral contract (`core/backend.py`). FreeCAD and Inventor are built in; **any CAD program
with an MCP server** can be added in config, without code:

```yaml
plugins:
  hermes-shaperotator:
    mcp_servers:
      inventor-mcp:                 # inventor-forge as a separate MCP server process
        command: [python, -m, inventor_forge.server]
        dialect: inventor-forge
      my-cad:                       # anything else that speaks MCP over stdio
        command: [node, my-cad-mcp.js]
        ops:                        # hermes-shaperotator op -> that server's tool (+ argument renames)
          status: get_status
          extrude: {tool: pad, args: {distance: length}}
```

Then `cad_backend backend=my-cad`. Unmapped ops fall back to a server tool of the same name, and
every server tool is reachable through `cad_native`.

A server can describe its tools for the plugin with a `cad/*` profile in each tool's `_meta` (inventor-forge's
`profile.py` defines it): which tools change the shape (the pane refreshes after them), take over the mouse and
keyboard (the approval prompt says so), run code (approval with the code shown), which arguments name files they
write or read (writes outside the job folder ask first; nothing is guessed), and how long a call may take. While a
call runs, MCP progress notifications keep it alive past its budget, up to twice it; a silent server still times
out. A server without a profile works too, with cautious answers to each of these. `tests/test_mcp_backend.py` drives a stand-in
CAD server this way.

## Tools (toolset `cad`)

| Tool | What it does |
|---|---|
| `cad_status`, `cad_backend` | backend availability / switch backend |
| `cad_document` | new part/assembly, open, save, close, export (STEP, STL, IGES, SAT, PDF) |
| `cad_sketch` | rect, circle, line, polygon, closed profile on XY/XZ/YZ (+offset) or a face |
| `cad_feature` | extrude/cut, revolve, hole, fillet, chamfer, circular pattern, delete — returns the delta |
| `cad_inspect` | mass properties, edges/faces tables, find edge/face, render a PNG |
| `cad_parameters` | get/set/add driving parameters |
| `cad_standard` | the chat's standard: get, list, set (asked once per chat) |
| `cad_drawing` | drawing sheets to the chat's standard, check, export PDF (gated) |
| `cad_transaction` | begin/commit/abort groups of operations |
| `cad_fea` | FEA run / face table / design sweep; materials, supports, force/pressure/gravity loads |
| `cad_python` | escape hatch: run Python inside the CAD app (**asks the user every time**) |
| `cad_native` | Inventor only: any inventor-forge tool by name (sheet metal, assemblies, materials, mass, analyze_stress / analyze_modes, drawing detail…); its Python and PDF tools are redirected to `cad_python` / the gated export |

Slash command: `/cad status | backend <name> | preview | folder | force-export`.
Bundled skills: `hermes-shaperotator:cad-modelling`, `hermes-shaperotator:cad-fea`.

## How the verification works

```
cad_fea / cad_native analyze_stress  ──►  evidence log (FEA-001: vm_max 13.79 MPa, disp 0.00163 mm, SF 20.3, warnings …)
                                   │
agent reply ── transform_llm_output hook ── extract figures ── compare (2 % / rounding tolerance)
                                   │
                         footer: ✅ / ⚠️ / ❌ / ❓ per figure
```

Figures that are inputs (the load you were given, a plate thickness) are not claims. Masses are only
reported when an analysis computed one. The footer is append-only, so streaming is unaffected.

## Hooks and what they do

| Hook | Behaviour |
|---|---|
| `pre_tool_call` | asks for approval before `cad_python`, before downloading a CAD program's add-on, before a server tool that deletes, runs code, takes over the mouse and keyboard or writes outside the job folder, and before a server tool that doesn't describe itself; blocks drawing PDF export until QA is clean |
| `post_tool_call` | after a geometry change, publishes the model to the Desktop pane (limited to about a fifth of build time; catches up at the end of the reply) |
| `pre_llm_call` | one line of CAD state (backend, last delta, QA status) so the model keeps its bearings; until a CAD program is chosen, asks the model to offer you the ones found on this computer |
| `transform_llm_output` | appends the verification footer when a reply quotes engineering figures |
| `on_session_finalize` | closes worker processes |

## Disclosures (risky behaviour)

- **Runs local programs:** starts FreeCAD's Python as a worker process (`freecad_path`), which runs Gmsh
  and CalculiX; on Windows can drive Autodesk Inventor over COM. It starts a separate Inventor for each chat
  (an Inventor you have open is left alone unless you ask for it) and closes it when the chat ends; one holding
  unsaved changes is shown and left open for you instead.
- **Arbitrary code (opt-in per call):** `cad_python` executes Python inside FreeCAD/Inventor with your user
  permissions. It always goes through Hermes's approval prompt (refused when nobody answers). inventor-forge also
  has an `execute_python` tool; `cad_native` refuses it and points to `cad_python`.
- **Downloads (opt-in, once per program):** `cad_backend(action='install')` downloads a CAD program's add-on server
  (inventor-forge from its GitHub repository at the commit pinned in `backends/known.json`, plus pywin32 from PyPI)
  and installs it with pip into its own environment under the plugin data folder. Always through Hermes's approval
  prompt, which names what is downloaded and where it goes (refused when nobody answers). The pinned commit only
  changes with a new release of this plugin; nothing updates itself. Without git, GitHub's archive of the same commit
  is used, and pip refuses it unless its sha256 matches the one in `known.json`. pip runs isolated from your pip
  configuration; only proxy and certificate settings are passed to it. The `server_sources` setting installs from a
  source you name instead (a local checkout, for example).
- **Background process:** once installed and in use, the add-on runs as a child process (an MCP server over stdio)
  until the session ends or you switch CAD program. It drives Inventor through COM and, while a long call runs,
  sends a progress message every 10 seconds.
- **Takes over the mouse and keyboard (opt-in per call):** `cad_native analyze_stress / analyze_modes` drive
  Inventor's Stress Analysis (which has no API) by clicking faces in Inventor's viewport and typing into its
  dialogs for about half a minute. Each call goes through Hermes's approval prompt (refused when nobody
  answers); every click is checked to land on Inventor first, otherwise the run stops. Inventor writes its
  HTML report to its working folder.
- **Files:** reads files you name (`open`, STEP for FEA) and writes models, renders, FEA working folders and
  exports under the plugin data folder (or `job_dir`), plus paths you give to `save`/`export`.
  Every call to the CAD add-on carries the folders and files it may write (`cad/write_scope`): the job folder,
  plus files outside it that you approved for that call. inventor-forge checks this at each write, including the
  changed parts an assembly save also writes and a name that changes on the way (an added extension, a `.dwg`
  fallback), and refuses before writing anything else; the refusal names the files, and the same call sent again
  asks you about them. Inventor itself still writes its stress-analysis reports and solver scratch folders to its
  own working folders.
- **Modifies replies:** appends a verification footer to assistant messages that contain engineering
  figures (text only, appended — never edits what the model wrote).
- Configured MCP commands launch child processes with your permissions. They get a minimal environment (what the OS needs) plus the server's own `env` from config, not Hermes's provider keys; the same goes for the FreeCAD worker and the headless browser. MCP servers are read only from the plugin's `mcp_servers` config.
- Files the model writes go to the session's job folder. Paths are taken literally (no `~` or `$VAR` expansion, `..` refused, symlinks resolved); a write that lands outside the job folder asks you first in Hermes's approval prompt, naming the resolved path. The pane polls the plugin's backend route for scene data. Live sync polls Inventor for model changes. PDF report generation launches a local Chrome or Edge browser in headless mode.

## Limits (honest)

- Linear-elastic, small-deflection, single solid. No contact, plasticity, buckling, fatigue or code checks;
  the FEA is a design aid, not certification.
- Assemblies / multi-body need bonded contacts → not analysed yet.
- Geometric selectors are robust to renumbering, but always check the resolved faces the run echoes.

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install pytest pyyaml fastapi
cd tests && ../.venv/Scripts/python -m pytest -q -c pytest.ini --rootdir . .
hermes plugins doctor . --ci
hermes plugins validate .
```

Tests that need FreeCAD skip when it isn't installed.

## Credits

The Inventor backend is [inventor-forge](https://github.com/RobertBarnettProjects/inventor-forge) (MIT, same author),
installed on first use; `scripts/pin_backend.py inventor ../inventor-forge` moves the pin to a pushed commit. FreeCAD, Gmsh
and CalculiX are separate programs installed by the user. Licence: MIT.
