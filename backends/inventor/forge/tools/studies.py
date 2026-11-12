"""Working with Stress Analysis studies that already exist, without the mouse or keyboard.

Faces can only be picked with real clicks, but once a study has its supports and loads it can be copied,
renamed, given new load values, solved and read back through commands, the study browser and Inventor's
own parameters. Build a study once with analyze_stress, then sweep load cases from it with these tools.

Inventor keeps each study's load values as model parameters named sa_N. Writing them changes the load
directly, with no dialog; a copied study gets its own new block of them, in the order the original's
values were created.
"""
from __future__ import annotations

import json
import os
import re
import time

from .. import uidrive as U
from ..com import InventorError, K, items, r, safe
from ..registry import tool
from ..session import Session
from .fea import (_answer_results_prompt, _enter_analysis, _pid, _report, _run_command, _set_value, _simulate,
                  parse_report)

STUDY_PANE = "{B3D04494-EDD2-4FDC-9EC2-30BAF8D6B77B}"      # the add-in's id, stable across languages
FOLDERS = ("Constraints", "Loads", "Contacts", "Mesh", "Results")


def _label(node) -> str:
    return safe(lambda: node.BrowserNodeDefinition.Label, "") or ""


def _pane(s: Session):
    _enter_analysis(s)
    for p in items(s.doc().BrowserPanes):
        if safe(lambda p=p: p.InternalName) == STUDY_PANE:
            return p
    raise InventorError("this document has no Stress Analysis browser; build a study first with analyze_stress")


def _study_nodes(s: Session) -> dict:
    """{study name: browser node}. Direct children only: walking the whole tree visits every part of every study."""
    out = {}
    for n in items(_pane(s).TopNode.BrowserNodes):
        if any(_label(c) in FOLDERS for c in items(n.BrowserNodes)):
            out[_label(n)] = n
    return out


def _study(s: Session, name: str):
    nodes = _study_nodes(s)
    if name not in nodes:
        raise InventorError(f"no study {name!r}; this document has {sorted(nodes)}")
    return nodes[name]


def _child(node, label: str):
    return next((c for c in items(node.BrowserNodes) if _label(c) == label), None)


def _select(s: Session, study: str, item: str = "") -> None:
    """Select a study, or a named item inside it. Names repeat between studies, so the search stays inside one,
    and a load in a collapsed folder can't be selected until the folder is open."""
    node = _study(s, study)
    _pane(s).Activate()
    d = s.front()                         # selections and the command after them act on the active document
    d.SelectSet.Clear()
    target = node
    if item:
        target = None
        for folder in items(node.BrowserNodes):
            hit = _child(folder, item)
            if hit is not None:
                safe(lambda f=folder: setattr(f, "Expanded", True))
                target = hit
                break
        if target is None:
            raise InventorError(f"no item {item!r} in study {study!r}")
    safe(lambda: target.EnsureVisible())
    target.DoSelect()
    time.sleep(0.3)
    if d.SelectSet.Count != 1:
        raise InventorError(f"could not select {item or study!r} in Inventor's browser")


def _close_strays(pid: int, main: int) -> None:
    """Close any small prompt a browser command left open."""
    for h, cls, _title in U.window_rows(pid):
        if cls == "#32770" and h != main:
            U.close(h)


def _activate(s: Session, study: str) -> None:
    _select(s, study)
    _run_command(s, "FeaActivateSimCmd")
    time.sleep(1.5)
    s.doc().SelectSet.Clear()


def _sa_params(s: Session) -> dict:
    mp = s.doc().ComponentDefinition.Parameters.ModelParameters
    return {p.Name: p for p in items(mp) if re.fullmatch(r"sa_\d+", p.Name)}


def _param_row(p) -> dict:
    return {"name": p.Name, "expression": p.Expression, "units": p.Units}


def _by_number(names) -> list[str]:
    return sorted(names, key=lambda n: int(n.split("_")[1]))


@tool("analysis", mutates=False)
def list_studies(s: Session) -> dict:
    """The Stress Analysis studies in this document and what each holds: constraints, loads, contacts, results."""
    out = []
    for name, node in _study_nodes(s).items():
        row = {"name": name}
        for folder in items(node.BrowserNodes):
            label = _label(folder)
            if label in ("Constraints", "Loads", "Results"):
                row[label.lower()] = [_label(c) for c in items(folder.BrowserNodes)]
            elif label == "Contacts":
                row["contacts"] = [_label(c) for g in items(folder.BrowserNodes) for c in items(g.BrowserNodes)]
        out.append(row)
    return {"studies": out, "count": len(out)}


def _contacts(s: Session, study: str | None):
    node = _study(s, study) if study else list(_study_nodes(s).values())[-1]
    folder = _child(node, "Contacts")
    return [(g, c) for g in items(folder.BrowserNodes) for c in items(g.BrowserNodes)] if folder is not None else []


def _contact_rows(s: Session, study: str | None) -> list[dict]:
    return [{"contact": _label(c), "type": _label(g)} for g, c in _contacts(s, study)]


def _set_contact(s: Session, pid: int, study: str | None, between: list[str], ctype: str) -> int:
    """Change the type of contacts whose name mentions all of the given components. Returns how many changed."""
    changed = 0
    for _ in range(50):
        match = next(((g, c) for g, c in _contacts(s, study)
                      if all(b in _label(c) for b in between) and _label(g).lower() != ctype.lower()), None)
        if match is None:
            break
        g, c = match
        _pane(s).Activate()
        s.doc().SelectSet.Clear()
        safe(lambda: setattr(g, "Expanded", True))
        safe(lambda: c.EnsureVisible())
        c.DoSelect()
        time.sleep(0.4)
        _run_command(s, "FeaEditContactCmd")
        h = U.wait_window(pid, "Contact", 10)
        U.combo_select(U.find(h, cid=10025), ctype)
        time.sleep(0.3)
        ok = U.find(h, cid=1)
        U.click(ok)
        U.wait_gone(h, 10)
        time.sleep(0.8)
        changed += 1
    return changed


@tool("analysis")
def set_contact_type(s: Session, study: str, between: list[str], type: str) -> dict:
    """Change the type of the automatic contacts between components, e.g. Separation for parts that may lift apart
    (compression only, so the solve becomes nonlinear and slower). No mouse needed.

    between: component names the contact must involve, e.g. ["Base:1", "Beam:1"]; one name changes all its contacts
    type: Bonded, Separation, Sliding / No Separation, Separation / No Sliding, Shrink Fit / Sliding, Shrink Fit / No Sliding, Spring
    """
    n = _set_contact(s, _pid(s), study, between, type)
    return {"changed": n, "contacts": _contact_rows(s, study)}


@tool("analysis")
def activate_study(s: Session, study: str) -> dict:
    """Make a study the active one (later solve and result commands act on it)."""
    t0 = time.time()
    _activate(s, study)
    return {"active": study, "seconds": r(time.time() - t0, 1)}


def _rename(s: Session, pid: int, study: str, new_name: str) -> None:
    import win32gui
    _select(s, study)
    _run_command(s, "FeaEditSimCmd")
    h = U.wait_window(pid, "Study", 10)
    win32gui.SendMessage(U.find_all(h, cls="Edit")[0].hwnd, 0x000C, 0, new_name)    # WM_SETTEXT; a plain text box
    time.sleep(0.3)
    U.click(U.find(h, cid=1))
    U.wait_gone(h, 15)
    time.sleep(0.8)
    if new_name not in _study_nodes(s):
        raise InventorError(f"Inventor did not rename the study to {new_name!r}")


@tool("analysis")
def rename_study(s: Session, study: str, new_name: str) -> dict:
    """Rename a Stress Analysis study."""
    _rename(s, _pid(s), study, new_name)
    return {"renamed": new_name}


COPY_WAIT = 40.0          # s: a large study can take well over 8 s to appear, and a retry then makes a second copy


def _sa_list(s: Session) -> list[tuple[str, str]]:
    """Every sa_N parameter as (name, expression), lowest number first."""
    p = _sa_params(s)
    return [(n, p[n].Expression) for n in _by_number(p)]


def _settled_sa(s: Session, tries: int = 8) -> list[tuple[str, str]]:
    """The sa_ parameters once Inventor has stopped adding and renumbering them (two reads 1 s apart agree)."""
    last = _sa_list(s)
    for _ in range(tries):
        time.sleep(1.0)
        now = _sa_list(s)
        if now == last:
            return now
        last = now
    return last


def _inserted(before: list[tuple[str, str]], after: list[tuple[str, str]]) -> list[str]:
    """Names of the parameters a copy added. Inventor renumbers every sa_ parameter when it copies a study, so names
    from before the copy mean nothing afterwards; values still do. The parameters that were there before are matched
    into the new list by value, in order, lowest numbers first, so the template keeps the lowest-numbered set; every
    parameter left over belongs to the copy (sometimes two sets of it: a copy can briefly hold a transient set too)."""
    i, extra = 0, []
    for name, value in after:
        if i < len(before) and U.same_value(value, before[i][1]):
            i += 1
        else:
            extra.append(name)
    if i != len(before):
        raise InventorError("could not match the study parameters from before the copy against the ones after it "
                            f"({i} of {len(before)} matched by value); the copy's load parameters are unknown, so "
                            "nothing was written. Save, reopen the file and try again")
    return extra


def _copy(s: Session, pid: int, source: str, new_name: str) -> dict:
    """Copy a study. Returns the copy's name, its parameters (see _inserted) and any duplicate copies a retry left."""
    before_params = _settled_sa(s, tries=2)
    before = set(_study_nodes(s))
    copy, attempts = None, 0
    for attempts in range(1, 4):            # the command is occasionally ignored just after a document switch
        _select(s, source)
        _run_command(s, "FeaSimulationCopyCmd")
        t0 = time.time()
        while time.time() - t0 < COPY_WAIT and copy is None:
            time.sleep(1.0)
            copy = next((n for n in _study_nodes(s) if n not in before), None)
        if copy:
            break
    if copy is None:
        raise InventorError(f"Inventor did not copy study {source!r} (waited {COPY_WAIT:.0f} s, {attempts} tries)")
    time.sleep(2.0)                          # a slow earlier attempt may still land
    duplicates = [n for n in _study_nodes(s) if n not in before and n != copy]
    if new_name:
        _rename(s, pid, copy, new_name)
        copy = new_name
    after = _settled_sa(s)
    return {"study": copy, "parameters": _inserted(before_params, after), "duplicates": duplicates}


def _sets(names: list[str], size: int, what: str) -> list[list[str]]:
    """Split a copy's parameters into sets of one study's size."""
    if not names or size <= 0 or len(names) % size:
        raise InventorError(f"{what}: the copy added {len(names)} load parameters, which isn't a whole number of sets of "
                            f"{size}; nothing was written. list_analysis_parameters shows them")
    sets = [names[k:k + size] for k in range(0, len(names), size)]
    for one in sets:                          # one study's parameters are numbered in a run; a gap means a bad match
        nums = [int(n.split("_")[1]) for n in one]
        if nums != list(range(nums[0], nums[0] + len(nums))):
            raise InventorError(f"{what}: the copy's load parameters {one} are not one numbered run, so they can't be told "
                                "apart from other studies' by value; nothing was written")
    return sets


def _write_checked(s: Session, sets: list[list[str]], values: list) -> list[dict]:
    """Write the same values into every parameter set of a copy, then read them back."""
    p = _sa_params(s)
    first = [p[n].Expression for n in sets[0]]
    for other in sets[1:]:
        shown = [p[n].Expression for n in other]
        if not all(U.same_value(a, b) for a, b in zip(shown, first)):
            raise InventorError(f"the copy holds parameter sets that disagree ({first} vs {shown}); nothing was written")
    for names in sets:
        _write(s, {n: v for n, v in zip(names, values) if v is not None})
    p = _sa_params(s)
    bad = [{"parameter": n, "wanted": v, "shown": p[n].Expression}
           for names in sets for n, v in zip(names, values) if v is not None and not U.same_value(p[n].Expression, str(v))]
    if bad:
        raise InventorError(f"Inventor did not keep the load values written: {bad}")
    return [_param_row(p[n]) for n in sets[-1]]


@tool("analysis")
def copy_study(s: Session, source: str, new_name: str = "") -> dict:
    """Copy a study, keeping its faces, supports and loads, so no faces need picking. Returns the copy's own load
    parameters, in the same order as the original's values (set them with set_analysis_parameters). Inventor
    renumbers every load parameter when it copies a study; the copy's ones are found by matching values.

    new_name: name for the copy (default: Inventor's)
    """
    c = _copy(s, _pid(s), source, new_name)
    all_params = _sa_params(s)
    out = {"study": c["study"], "parameters": [_param_row(all_params[n]) for n in c["parameters"]]}
    if c["duplicates"]:
        out["warning"] = f"a slow copy also left {c['duplicates']}; delete them with delete_study_item"
    return out


@tool("analysis", mutates=False)
def list_analysis_parameters(s: Session) -> dict:
    """Every Stress Analysis load value in this document: the sa_N parameters, in creation order (forces in N as
    X, Y, Z triples; gravity in mm/s^2)."""
    p = _sa_params(s)
    return {"parameters": [_param_row(p[n]) for n in _by_number(p)], "count": len(p)}


def _write(s: Session, values: dict) -> list[dict]:
    p = _sa_params(s)
    unknown = [k for k in values if k not in p]
    if unknown:
        raise InventorError(f"no such analysis parameter(s): {unknown}")
    for k, v in values.items():
        p[k].Expression = str(v)
    s.doc().Update()
    return [_param_row(p[k]) for k in values]


@tool("analysis")
def set_analysis_parameters(s: Session, values: dict) -> dict:
    """Change Stress Analysis load values directly, with no dialog. The affected study needs solving again.

    values: {"sa_12": "1500 N", "sa_13": "Pull * sin(Ang)"} — expressions can use the part's own parameters
    """
    return {"set": _write(s, values), "note": "the results of the study using these are now out of date; solve it again"}


def _open_result(s: Session, pid: int, study: str, result: str) -> None:
    """Show one of a solved study's results in the viewport."""
    _activate(s, study)
    results = _child(_study(s, study), "Results")
    if results is None:
        raise InventorError(f"study {study!r} has no results yet")
    safe(lambda: setattr(results, "Expanded", True))
    node = _child(results, result)
    if node is None:
        raise InventorError(f"no result {result!r} in study {study!r}; it has {[_label(c) for c in items(results.BrowserNodes)]}")
    _pane(s).Activate()
    d = s.doc()
    d.SelectSet.Clear()
    safe(lambda: node.EnsureVisible())
    node.DoSelect()
    time.sleep(0.4)
    _run_command(s, "FeaActivateResultCmd")
    time.sleep(2.0)
    d.SelectSet.Clear()
    if _answer_results_prompt(pid):
        STALE.add(study)


STALE: set[str] = set()          # studies whose results Inventor said were out of date when opened


def _colour_bar(s: Session, pid: int) -> int:
    h = 0
    for _ in range(4):
        _run_command(s, "FeaColorChartCmd")
        try:
            h = U.wait_window(pid, "Color Bar", 8)
            break
        except InventorError:
            _answer_results_prompt(pid)
    if not h:
        raise InventorError("Inventor's colour bar dialog did not open")
    return h


def _read_max(s: Session, pid: int, study: str, result: str) -> float:
    """A solved study's peak value, taken from the colour bar's automatic maximum."""
    _open_result(s, pid, study, result)
    h = _colour_bar(s, pid)
    box = U.find(h, text="Maximum", cls="Button")
    auto = U.is_checked(box)
    shown = U.get_text(U.find_all(h, cls="Edit")[0])
    U.close(h)
    if not auto:
        raise InventorError(f"{study!r}: the colour bar has a manual maximum, so it doesn't show the true peak")
    value = U._leading_number(shown)
    if value is None:
        raise InventorError(f"could not read the colour bar value {shown!r}")
    return value


@tool("analysis")
def read_result_maximum(s: Session, study: str, result: str = "Von Mises Stress") -> dict:
    """Peak value of a solved study's result, without generating a report (seconds, against minutes for a file with
    many studies). Gives peaks only; use study_report for reactions and full results.

    result: as named in the study's Results folder, e.g. Von Mises Stress, Displacement, Safety Factor
    """
    t0 = time.time()
    STALE.discard(study)
    v = _read_max(s, _pid(s), study, result)
    out = {"study": study, "result": result, "maximum": v, "seconds": r(time.time() - t0, 1)}
    if study in STALE:
        out["warning"] = "Inventor said this study's results are out of date (the model changed since it was solved); solve it again"
    return out


def _picture(s: Session, study: str, result: str, view: str) -> str:
    from .docs import VIEWS
    stem = re.sub(r"\W+", "_", f"{study} {result}")
    png = os.path.join(s.workdir, "renders", f"{stem}.png")
    os.makedirs(os.path.dirname(png), exist_ok=True)
    cam = s.view().Camera
    if view:
        key = VIEWS.get(view.lower())
        if not key:
            raise InventorError(f"view must be one of {sorted(VIEWS)}")
        cam.ViewOrientationType = getattr(K, key)
    cam.Fit()
    cam.ApplyWithoutTransition()
    s.view().SaveAsBitmap(png, 1600, 1000)
    return png


@tool("analysis")
def show_result(s: Session, study: str, result: str = "Von Mises Stress", view: str = "iso") -> dict:
    """Show a solved study's result in Inventor and save a picture of it.

    result: as named in the study's Results folder, e.g. Von Mises Stress, Displacement, Safety Factor
    view: iso, front, top, right...
    """
    _open_result(s, _pid(s), study, result)
    return {"study": study, "result": result, "path": _picture(s, study, result, view)}


@tool("analysis")
def set_result_colors(s: Session, study: str, result: str = "Von Mises Stress", maximum: float | None = None,
                      minimum: float | None = None, gray: bool = False, auto: bool = False, view: str = "iso") -> dict:
    """Change how Inventor colours a solved study's result: a fixed scale (say, ending at yield so anything red is over it),
    greyscale, or back to automatic. No mouse needed. Returns a picture. A manual maximum stops read_result_maximum reading
    the true peak until auto is set again.

    result: as named in the study's Results folder
    maximum: top of the colour scale, in the result's units
    minimum: bottom of the colour scale
    gray: greyscale instead of colour
    auto: return the scale to the result's own minimum and maximum
    view: iso, front, top, right...
    """
    pid = _pid(s)
    _open_result(s, pid, study, result)
    h = _colour_bar(s, pid)
    other = U.find(h, cid=2)
    shown = {}
    U.real_focus(h)
    for label, cid, value in (("Maximum", 10005, maximum), ("Minimum", 10084, minimum)):
        box = U.find(h, text=label, cls="Button")
        if auto:
            U.set_check(box, True)
        elif value is not None:
            U.set_check(box, False)
            time.sleep(0.3)
            text = U.commit_text(U.find(h, cid=cid), f"{float(value):g}", other)
            if not U.same_value(text, f"{float(value):g}"):
                U.close(h)
                raise InventorError(f"Inventor did not accept {label} = {value} (the box shows {text!r})")
            shown[label.lower()] = float(value)
    U.click(U.find(h, cid=10088 if gray else 10087))
    time.sleep(0.4)
    ok = U.find(h, cid=1)
    if ok.enabled:
        U.click(ok)
        U.wait_gone(h, 10)
    else:                                     # nothing actually changed
        U.close(h)
    time.sleep(1.0)
    return {"study": study, "result": result, **shown, "scale": "automatic" if auto else "manual" if shown else "unchanged",
            "gray": gray, "path": _picture(s, study, result, view)}


def _bar_values(s: Session, pid: int) -> tuple[float, float, int]:
    h = _colour_bar(s, pid)
    lo = U._leading_number(U.get_text(U.find(h, cid=10084)))
    hi = U._leading_number(U.get_text(U.find(h, cid=10005)))
    n = U._leading_number(U.get_text(U.find(h, cid=10086))) or 0
    U.close(h)
    return lo, hi, int(n)


def _bar_max(s: Session, pid: int, maximum: float | None, minimum: float | None = None) -> None:
    """Set the colour scale's top and bottom (None = automatic) without moving the camera."""
    h = _colour_bar(s, pid)
    for label, cid, value in (("Maximum", 10005, maximum), ("Minimum", 10084, minimum)):
        box = U.find(h, text=label, cls="Button")
        if value is None:
            U.set_check(box, True)
        else:
            U.set_check(box, False)
            time.sleep(0.2)
            U.commit_text(U.find(h, cid=cid), f"{value:.6g}", U.find(h, cid=2))
    time.sleep(0.2)
    ok = U.find(h, cid=1)
    if ok.enabled:
        U.click(ok)
        U.wait_gone(h, 10)
    else:
        U.close(h)
    time.sleep(0.8)


@tool("analysis")
def result_at_point(s: Session, study: str, face, result: str = "Von Mises Stress", point: list[float] | None = None,
                    tolerance: float = 2.0, user_notified: bool = False) -> dict:
    """A solved result's value at one spot — the middle of a face, or a point on it — for example the stress a short
    way from a sharp corner whose peak is a singularity. Inventor has no API for this, so the value is read off the
    screen: the colour scale's top is moved until that spot just reaches the top colour. Inventor comes to the front
    and the pointer is moved out of the way, so this needs user_notified=true like the analyses.

    face: face index (part) or "Component:1/5" (assembly)
    point: a point on that face, mm (default: the middle of the face)
    tolerance: how close to find the value, in the result's units
    """
    from .fea import (_bring_to_front, _close, _notice_gate, _pixels, _run_command as run, _look_at_face, _wait_screen_settled,
                      face_ref)
    import win32api
    import win32gui
    gate = _notice_gate(user_notified, "result_at_point")
    if gate:
        return gate
    pid = _pid(s)
    f = face_ref(s, face)
    _bring_to_front(s)
    _open_result(s, pid, study, result)
    run(s, "FeaContourPlotCmd")                       # banded colours: the top band has a sharp edge
    time.sleep(1.0)
    lo, hi, bands = _bar_values(s, pid)
    if lo is None or hi is None:
        raise InventorError("could not read the colour scale")
    bands = bands or 5
    _look_at_face(s, f, at=point)
    hwnd = s.view().HWND
    l, t_, r_, b_ = win32gui.GetClientRect(hwnd)
    centre = win32gui.ClientToScreen(hwnd, ((r_ - l) // 2, (b_ - t_) // 2))
    win32api.SetCursorPos(win32gui.ClientToScreen(hwnd, (r_ - 5, b_ - 5)))   # pointer off the spot: hovering changes colours
    _wait_screen_settled(s)

    span = max(abs(hi), abs(lo), 1e-9)

    def edge(bottom: float) -> float:
        """With the scale's bottom fixed, the scale top m at which the spot just stops showing the top colour. The spot
        turns that colour once its value passes bottom + f (m - bottom), for some fraction f set by Inventor's shading."""
        def at_top(m: float) -> bool:
            _bar_max(s, pid, m, bottom)
            time.sleep(0.6)
            px = _pixels([centre], 4)
            hit = sum(1 for c in px if _close(c, top, 40)) >= 3
            if os.environ.get("INVENTOR_FORGE_DEBUG"):
                print("edge", bottom, round(m, 4), hit, px, flush=True)
            return hit
        a, b = bottom + span * 1e-4, bottom + 2.2 * span
        for _ in range(30):
            if b - a <= tolerance / 4:
                break
            m = (a + b) / 2
            if at_top(m):
                a = m
            else:
                b = m
        return (a + b) / 2

    try:
        top = (255, 0, 0)                               # the top of Inventor's colour scale is pure red
        _bar_max(s, pid, None, None)
        # two scales with different bottoms give two equations for the value and Inventor's fraction f:
        #   v = f m1 (bottom 0)   and   v = B + f (m2 - B) (bottom B, below v)   =>   v = m1 B / (m1 + B - m2)
        m1 = edge(0.0)
        bottom = 0.25 * m1                             # some results (von Mises) can't take a negative bottom
        m2 = edge(bottom)
        den = m1 + bottom - m2
        value = m1 * bottom / den if den > 1e-12 else m1
        if os.environ.get("INVENTOR_FORGE_DEBUG"):
            print("m1", m1, "bottom", bottom, "m2", m2, "top", top, flush=True)
    finally:
        _bar_max(s, pid, None, None)
    return {"study": study, "result": result, "value": r(value, 3), "tolerance": tolerance, "scale": [lo, hi],
            "how": "read from the screen with the colour scale; good to about the tolerance"}


@tool("analysis")
def mesh_convergence(s: Session, study: str, fractions: list[float] | None = None, result: str = "Von Mises Stress",
                     timeout: float = 3600.0) -> dict:
    """Check that a study's peak doesn't depend on the mesh: copies it at finer average element sizes, solves each and
    compares the peaks. No mouse needed. (analyze_stress's refinements= uses Inventor's own refinement instead.)
    Each size is solved, which is the only check that catches a degenerate element: Inventor meshes such a model
    without complaint and reports it at solve time, so a mesh preview alone can't vet a model.

    fractions: average element size as a fraction of the model size, coarse to fine (default 0.1, 0.07, 0.05)
    """
    from .fea import _mesh
    pid = _pid(s)
    rows = []
    for f in fractions or [0.1, 0.07, 0.05]:
        name = f"{study} mesh {f:g}"
        if name not in _study_nodes(s):
            _copy(s, pid, study, name)
        _activate(s, name)
        _mesh(s, pid, float(f), allow_typing=False)
        secs, warnings = _simulate(s, pid, timeout=timeout, front=False)
        rows.append({"study": name, "mesh_fraction": f, "peak": _read_max(s, pid, name, result), "solve_seconds": r(secs, 1),
                     "warnings": warnings})
    change = abs(rows[-1]["peak"] - rows[-2]["peak"]) / max(abs(rows[-1]["peak"]), 1e-12) if len(rows) > 1 else None
    return {"result": result, "runs": rows, "last_change_pct": r(100 * change, 2) if change is not None else None,
            "converged": change is not None and change < 0.05,
            "note": "a peak that keeps rising as the mesh gets finer sits on a singularity (a sharp corner or a point support); "
                    "judge the stress a short way from it, e.g. with result_at_point"}


@tool("analysis")
def refresh_analysis_display(s: Session) -> dict:
    """Clear arrows and labels left on screen from other studies by leaving Stress Analysis and coming back. Save first:
    switching environments repeatedly has been linked to an Inventor crash."""
    d = s.doc()
    base = d.EnvironmentManager.BaseEnvironment
    d.EnvironmentManager.SetCurrentEnvironment(base)
    time.sleep(1.5)
    _enter_analysis(s)
    time.sleep(1.5)
    return {"refreshed": True}


@tool("analysis")
def solve_study(s: Session, study: str, timeout: float = 3600.0) -> dict:
    """Solve an existing study, with no mouse or keyboard use. Don't use Inventor until it returns; solver warnings
    are returned.

    timeout: seconds to allow the solve
    """
    pid = _pid(s)
    _activate(s, study)
    secs, warnings = _simulate(s, pid, timeout=timeout, front=False)
    return {"study": study, "solve_seconds": r(secs, 1), "warnings": warnings}


@tool("analysis")
def study_report(s: Session, study: str = "", name: str = "", only_this_study: bool = False,
                 user_notified: bool = False) -> dict:
    """Generate Inventor's report and read one study from it: summary, reactions (the way to get reactions; Inventor's
    Reaction Forces dialog crashes it), loads, material. A full report covers every study, which takes many minutes on a
    file with several and has crashed Inventor on very large ones.

    study: study to read (default: the newest)
    name: report file name
    only_this_study: a short report of just this study's set-up (material, mesh, loads, supports). Inventor 2027.1
      leaves results and reactions out of any report limited to chosen studies, so use the full report for those.
      Choosing the study needs real mouse clicks, so this also needs user_notified=true after showing the user the notice
    """
    if only_this_study:
        from .fea import _notice_gate
        gate = _notice_gate(user_notified, "study_report")
        if gate:
            return gate
        if not study:
            raise InventorError("only_this_study needs the study name")
    tag = name or f"forge_report_{time.strftime('%H%M%S')}"
    if study:
        _activate(s, study)                   # Inventor only writes a study's results once they are loaded
        _open_result(s, _pid(s), study, "Von Mises Stress")
    path = _report(s, _pid(s), tag, front=only_this_study, only_study=study if only_this_study else "")
    out = {**parse_report(path, study), "report": path}
    if only_this_study and not out.get("summary"):
        out["note"] = "set-up only: Inventor leaves results out of reports limited to chosen studies; run without only_this_study for results and reactions"
    return out


def _save(s: Session) -> bool:
    d = s.doc()
    if not safe(lambda: d.FullFileName, ""):
        return False
    app = s.app
    was = app.SilentOperation
    app.SilentOperation = True
    try:
        d.Save()
    finally:
        app.SilentOperation = was
    return True


def _load_results(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except ValueError as e:
            raise InventorError(f"{path} is not valid JSON ({e}); move it aside to start again") from e
    return {"cases": {}}


def _store_results(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


@tool("analysis")
def run_study_cases(s: Session, template: str, cases: list[dict], results_path: str = "",
                    results: list[str] | None = None, timeout: float = 3600.0) -> dict:
    """Run a table of load cases from one template study, with no mouse or keyboard. Each case copies the template,
    writes its load values, solves, reads the peaks and saves the file. Finished cases are recorded, so after a crash
    the same call carries on where it stopped; in a private Inventor (connect(private=true)) a crashed Inventor is restarted
    and the run continues by itself. Save the document first, and keep runs short: long Inventor sessions are the usual
    cause of crashes.

    template: an existing study with its faces, supports and loads
    cases: [{"name": "Pull 15deg", "values": ["0 N", "1000 N", "0 N", ...]}]; values follow copy_study's parameter
      order, one per parameter (null keeps the copied value)
    results_path: JSON file of finished cases (default: next to the workdir, named after the document)
    results: result names to read (default Von Mises Stress and Displacement)
    timeout: seconds to allow each solve
    """
    want = results or ["Von Mises Stress", "Displacement"]
    pid = _pid(s)
    doc_name = re.sub(r"\W+", "_", s.doc().DisplayName)
    path = results_path or os.path.join(s.workdir, "studies", f"{doc_name}_cases.json")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = _load_results(path)
    names = [c.get("name", "") for c in cases]
    if not all(names) or len(set(names)) != len(names):
        raise InventorError("every case needs its own non-empty name")
    if template not in _study_nodes(s):
        raise InventorError(f"no study {template!r}; this document has {sorted(_study_nodes(s))}")
    ran = skipped = restarts = 0
    doc_path = safe(lambda: s.doc().FullFileName, "")
    private = s.private                       # read now: the session drops the flag the moment its Inventor dies
    reasons: list[str] = []
    for case in cases:
        name = case["name"]
        done = data["cases"].get(name, {})
        if done.get("status") == "done":
            if done.get("values", case.get("values")) != case.get("values") or done.get("template", template) != template:
                raise InventorError(f"case {name!r} was already run with different values or template; give it a new name")
            skipped += 1
            continue
        while True:
            try:
                _run_case(s, pid, template, case, want, timeout, data, path)
                ran += 1
                break
            except Exception as e:
                if not (private and doc_path and pid not in U.pids_named("Inventor.exe") and restarts < 2):
                    raise
                restarts += 1                       # this session's own Inventor died: start another and carry on
                reasons.append(f"{type(e).__name__}: {e}"[:200])
                U.end_orphaned_solvers(pid)
                s._app, s.private, s.pid = None, False, None
                s.start_private()
                s.set_current(s.app.Documents.Open(doc_path, True))
                pid = _pid(s)
                _enter_analysis(s)
    return {"ran": ran, "already_done": skipped, "inventor_restarts": restarts, "restart_reasons": reasons, "results_path": path,
            "cases": {n: data["cases"][n] for n in names if n in data["cases"]}}


def _run_case(s: Session, pid: int, template: str, case: dict, want: list[str], timeout: float, data: dict, path: str) -> None:
    if True:
        name = case["name"]
        t0 = time.time()
        if os.environ.get("INVENTOR_FORGE_TEST_EXIT_AT") == name:      # test hook: this session's Inventor exits mid-run
            os.environ.pop("INVENTOR_FORGE_TEST_EXIT_AT")
            s.app.SilentOperation = True
            s.app.Quit()
            t1 = time.time()
            while pid in U.pids_named("Inventor.exe") and time.time() - t1 < 60:
                time.sleep(0.5)
            raise InventorError("Inventor exited (test)")
        values = case.get("values") or []
        if name in _study_nodes(s):
            raise InventorError(f"a study named {name!r} already exists but isn't recorded as finished; delete or rename it")
        c = _copy(s, pid, template, name)
        written = []
        if values:
            # every set the copy holds gets the values (a transient set may be the one Inventor solves with), and
            # they are read back, so a case can never solve with the template's loads while looking finished
            written = _write_checked(s, _sets(c["parameters"], len(values), f"case {name!r}"), values)
        secs, warnings = _simulate(s, pid, timeout=timeout, front=False) if not _activate(s, name) else (0.0, [])
        row = {"status": "done", "solve_seconds": r(secs, 1), "warnings": warnings, "values": values, "template": template,
               "parameters_written": written, "duplicate_copies": c["duplicates"],
               "results": {res: _read_max(s, pid, name, res) for res in want}}
        row["saved"] = _save(s)
        row["total_seconds"] = r(time.time() - t0, 1)
        row["inventor_memory_gb"] = U.process_stats(pid)["memory_gb"]
        data["cases"][name] = row
        _store_results(path, data)


@tool("analysis")
def delete_study_item(s: Session, study: str, item: str = "") -> dict:
    """Delete a whole study, or one constraint or load inside it. This can't be undone. To move a load to other faces,
    delete it and add it again: picking in an edit dialog adds faces.

    item: constraint or load name; empty deletes the study and its results
    """
    pid = _pid(s)
    main = s.app.MainFrameHWND
    _select(s, study, item)
    known = {h for h, _c, _t in U.window_rows(pid)}
    _run_command(s, "FeaDeleteObjectCmd")
    time.sleep(1.0)
    for h, cls, _title in U.window_rows(pid):
        if cls == "#32770" and h not in known and h != main:
            for label in ("Yes", "OK"):
                b = next((c for c in U.find_all(h, cls="Button") if c.text.replace("&", "") == label), None)
                if b is not None:
                    U.click(b)
                    break
    time.sleep(1.0)
    still = study in _study_nodes(s)
    if not item and still:
        raise InventorError(f"Inventor did not delete study {study!r}")
    return {"deleted": item or study}


@tool("analysis", mutates=False, com=False)
def solve_state(s: Session) -> dict:
    """What every running Inventor is doing, read from Windows only (window titles, CPU, memory), so it is safe during a
    solve and while another session is using Inventor. Call it twice a few seconds apart: rising CPU means still working."""
    out = []
    for pid in U.pids_named("Inventor.exe"):
        rows = U.window_rows(pid)
        out.append({"process": pid, **U.process_stats(pid), "solving": U.solver_windows([pid]),
                    "windows": [f"{title} [{cls}]" for _h, cls, title in rows if title][:12]})
    solver = [{"process": p, **U.process_stats(p)} for p in U.pids_named("FEAComputeServer.exe")]
    return {"inventors": out, "solver_processes": solver, "this_session": s.pid}
