# Session, COM and unattended runs

## Working alongside the user
- Use `connect(private=true)` whenever the user may have Inventor open. A private Inventor can crash or be closed
  without touching their work.
- The session is pinned to the Inventor process chosen at connect. If that process goes, calls are refused rather
  than moved to another Inventor; `connect(reattach=true)` switches deliberately.
- Never drive the user's Inventor while it solves. `solve_state` reads Windows only and is always safe.
- Save after each confirmed step. One crash loses every unsaved document in that Inventor.
- Application options (visual style and others) are shared by every Inventor of the Windows user and written back
  on exit. Change the view or document setting (`set_display(scope=view|document)`), not the application one.
- The active project is shared too: `set_active_project` refuses while other Inventors run.
- Elevated windows (Task Manager, installers) block cursor moves and focus changes; mouse-driven tools check first.

## Failures
- The same call failing the same way twice in a row is refused a third time. Change the arguments, check the model
  (`list_features`, `inspect`), or reproduce the step in a private Inventor. Runs of failing calls come before crashes.
- "Busy" refusals are retried automatically for up to a minute (a COM message filter). Persistent busy means a
  dialog is open: `pending_call` shows it, `answer_dialog` answers it.
- A call that stops making progress is reported: a warning after 2 minutes, given up after 10
  (`INVENTOR_FORGE_HANG_WARN` / `INVENTOR_FORGE_HANG_STOP`). Stuck means Windows says "not responding", or Inventor and
  its solver use no CPU at all (a responsive Inventor sitting on a modal prompt).
- Read the result after setting anything that can be silently ignored: names (`rename` reads back), suppression,
  parameter values.

## COM binding
- Everything is late-bound, method results included. In `execute_python`, wrap objects that come back typed from a
  gen_py cache with `late(obj)` before using part-specific members.
- Use the type library's named constants (`K.kJoinOperation`), never raw numbers. Constant names don't always say
  what they do on screen: check the result (e.g. a two-point distance's orientation).
- Methods with `[out]` arguments need `typed_call(obj, "Method", ...)`. `api_lookup` shows which arguments are out.
- Optional arguments that must be "not given" go as `pythoncom.Missing`, not `None`. Typed arrays (prompt strings)
  go as `win32com.client.VARIANT(VT_ARRAY | VT_BSTR, values)`; a Python list arrives as an array of VARIANTs and
  fails with E_INVALIDARG, which looks like wrong content.

## Starting Inventor
- Starts are serialised across processes by a lock file in the temp folder, held until the new Inventor is
  identified, and staggered (`INVENTOR_FORGE_START_STAGGER`, s). Two starts in the same second can't tell their
  processes apart.
- A start that can't identify its process quits the Inventor it created; hidden instances must not pile up.
- `connect` checks the active project (`expect_project`), documents with unsaved changes and free disk space.

## Unattended runs
- Keep runs short and resumable: record each finished step to a file, and resume from it after a restart.
- Long calls keep the PC awake while they run; a sleep timer otherwise stops a solve (idle counts only input).
- Watch every run for: a traceback, a failed solve, the run ending, and a hang (no CPU across the process tree for a
  set time, not only "not responding"). Report the first one at once.
- Chain follow-on work (renders, spreadsheets) after the specific run it depends on, not after "nothing running":
  that also fires while a failed run is being restarted.
- Wait on a positive marker that only the awaited step writes; a check that errors must not read as "done".
- Diagnostic pictures: grab only Inventor's own window or dialog, never the whole screen.
- Keep at most two solves running on one PC, and do drawing work only while at most one runs.
