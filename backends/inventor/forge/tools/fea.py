"""Stress and modal analysis with Inventor's own Stress Analysis environment.

Inventor's Stress Analysis has no automation API, so this drives it the way a person would:
commands are started through the API, faces are picked with a real mouse click in the viewport
(the camera is first turned to look straight at the face, which then sits under the centre of the
view), values are typed into the dialogs with the keyboard where Inventor's value boxes require it,
and the results are read back from Inventor's own stress-analysis report.

While an analysis runs it takes over the mouse and keyboard for a few seconds at a time: Inventor
must be visible on screen and nobody should touch the mouse. Every click and keystroke is checked to
land on Inventor first; otherwise the run stops with an error instead of clicking somewhere else.
"""
from __future__ import annotations

import glob
import html
import math
import os
import re
import shutil
import time

from .. import uidrive as U
from ..com import InventorError, K, items, r, safe, typed_call, plain_name
from ..registry import tool
from ..session import Session
from .inspect import face_centroid, face_normal, find_planar_face

FEA_ENV = "FEA Environment Internal Name"


# --------------------------------------------------------------------------- input (mouse / keyboard)
def _pid(s: Session) -> int:
    import win32process
    return win32process.GetWindowThreadProcessId(s.app.MainFrameHWND)[1]


def _owned_by_inventor(hwnd: int, pid: int) -> bool:
    import win32process
    return bool(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid


def _bring_to_front(s: Session) -> None:
    import win32api
    import win32con
    import win32gui
    ready, why = U.mouse_ready()
    if not ready:
        raise InventorError(f"can't bring Inventor to the front: {why}")
    h = s.app.MainFrameHWND
    if win32gui.IsIconic(h):
        win32gui.ShowWindow(h, win32con.SW_RESTORE)
    try:     # Windows only lets the foreground process hand over focus; an Alt tap makes it allowed
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32gui.SetForegroundWindow(h)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception:
        pass
    time.sleep(0.4)


def _click_screen(x: int, y: int, pid: int, what: str) -> None:
    import win32api
    import win32con
    import win32gui
    ready, why = U.mouse_ready()
    if not ready:
        raise InventorError(f"cannot click {what}: {why}. Nothing was clicked")
    under = win32gui.WindowFromPoint((x, y))
    if not _owned_by_inventor(under, pid):
        raise InventorError(f"cannot click {what}: another window covers Inventor at that spot — bring Inventor "
                            "to the front, keep it unobstructed and don't use the mouse during the analysis")
    for _ in range(3):
        win32api.SetCursorPos((x, y))
        time.sleep(0.25)
        win32api.SetCursorPos((x + 1, y))       # a little movement so Inventor highlights what is under the cursor
        time.sleep(0.35)
        win32api.SetCursorPos((x, y))
        time.sleep(0.15)
        if win32api.GetCursorPos() == (x, y):
            break
    else:
        ready, why = U.mouse_ready()
        if not ready:
            raise InventorError(f"cannot click {what}: {why}")
        raise InventorError(f"cannot click {what}: the mouse keeps moving — someone is using it. Stopped; rerun when "
                            "the mouse can be left alone")
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
    time.sleep(0.4)


def _type_into(c: U.Ctrl, text: str, pid: int) -> None:
    """Inventor's value boxes ignore WM_SETTEXT, so click into the box and type like a person."""
    import win32api
    import win32con
    import win32gui
    left, top, right, bottom = win32gui.GetWindowRect(c.hwnd)
    _click_screen((left + right) // 2, (top + bottom) // 2, pid, "a dialog field")
    fg = win32gui.GetForegroundWindow()
    if not _owned_by_inventor(fg, pid):
        raise InventorError("keyboard focus left Inventor — stopping before typing anywhere else")
    keys = []
    keys += [(win32con.VK_CONTROL, 0), (ord("A"), 0), (ord("A"), 2), (win32con.VK_CONTROL, 2)]
    for ch in str(text):
        vk = win32api.VkKeyScan(ch)
        shift = (vk >> 8) & 1
        if shift:
            keys.append((win32con.VK_SHIFT, 0))
        keys += [(vk & 0xFF, 0), (vk & 0xFF, 2)]
        if shift:
            keys.append((win32con.VK_SHIFT, 2))
    keys += [(win32con.VK_TAB, 0), (win32con.VK_TAB, 2)]
    for vk, flag in keys:
        win32api.keybd_event(vk, 0, flag, 0)
        time.sleep(0.02)
    time.sleep(0.3)


def _leading(text) -> bool:
    return U._leading_number(text) is not None


def _set_value(dlg: int, c: U.Ctrl, text: str, pid: int, allow_typing: bool = True) -> None:
    """Enter a value and confirm the box took it. Done with window messages and a real focus change (typed
    keys are sometimes dropped); typing is only the fallback, and only where the user agreed to it."""
    _front(dlg)
    others = [o for o in U.find_all(dlg, cls="Edit") if o.hwnd != c.hwnd and o.enabled]
    other = others[0] if others else U.find(dlg, cid=2)
    shown = U.commit_text(c, text, other)
    if U.same_value(shown, text) and shown.strip() == str(text).strip() and _leading(text):
        # shown exactly as entered: Inventor reformats a value it has parsed (12.5 -> 12.500 N), so this one may
        # not have been taken yet; give it one more real focus change before trusting it
        shown = U.commit_text(c, text, other)
    if U.same_value(shown, text):
        return
    if not allow_typing:
        raise InventorError(f"Inventor did not accept the value {text!r}")
    _type_into(c, text, pid)
    shown = U.get_text(c)
    if not U.same_value(shown, text):
        raise InventorError(f"Inventor did not accept the value {text!r} (the box shows {shown!r})")


# --------------------------------------------------------------------------- picking faces
def _face_key(f) -> tuple:
    """Identity of a face, also for a face of a component seen from the assembly (a proxy)."""
    occ = safe(lambda: f.ContainingOccurrence.Name, "")
    native = safe(lambda: f.NativeObject) if occ else None
    return occ, safe(lambda: (native or f).TransientKey)


def _same_face(a, b) -> bool:
    return _face_key(a) == _face_key(b)


def _any_cdef(s: Session):
    return s.doc().ComponentDefinition


def _ray_first_hit_is(s: Session, face, origin, direction) -> bool | None:
    """True if a ray from origin along direction hits `face` first (None if the API can't tell)."""
    try:
        cd = _any_cdef(s)
        out = typed_call(cd, "FindUsingRay", s.tg.CreatePoint(*origin), s.tg.CreateUnitVector(*direction),
                         0.0001, None, None, True)
        objs = out[-2] if len(out) >= 2 else None
        if objs is None or objs.Count == 0:
            return None
        first = objs.Item(1)
        return _same_face(first, face)
    except Exception:
        return None


def face_ref(s: Session, ref):
    """A face to pick: an index (part) or "Component:1/5" = face 5 of that component (assembly; indices as in
    inspect_assembly(faces_of=...))."""
    if isinstance(ref, dict) and "occurrence" not in ref:      # {"face": n, "at": [x, y, z] mm}: aim the pick there
        f = s.face(int(ref["face"]))
        if ref.get("at"):
            _AIM[safe(lambda: f.TransientKey)] = [float(v) * 0.1 for v in ref["at"]]
        return f
    if isinstance(ref, dict):
        ref = f"{ref.get('occurrence')}/{ref.get('face')}"
    if isinstance(ref, str) and "/" in ref:
        occ_name, idx = ref.rsplit("/", 1)
        from .assembly import _occ
        o = _occ(s, occ_name.strip())
        faces = o.SurfaceBodies.Item(1).Faces
        i = int(idx)
        if not 1 <= i <= faces.Count:
            raise InventorError(f"{occ_name} has faces 1..{faces.Count}, not {i}")
        return faces.Item(i)
    if s.kind() == "assembly":
        raise InventorError(f"in an assembly give faces as \"Component:1/5\" (component name / face index), not {ref!r}")
    return s.face(int(ref))


_AIM: dict = {}          # face TransientKey -> preferred aim point (cm), from references that carry a point


def _normal(face) -> list[float]:
    """Outward normal at the face's aim point when one was given (curved faces: it differs across the face)."""
    given = _AIM.get(safe(lambda: face.TransientKey))
    if given:
        try:
            from ..com import typed_call as _tc
            out = _tc(face.Evaluator, "GetNormalAtPoint", list(given), [0.0, 0.0, 0.0])
            n = list(out[-1])
            if len(n) == 3 and any(abs(v) > 1e-12 for v in n):
                return n
        except Exception:
            pass
    return face_normal(face)


def _aim_point(s: Session, face, n) -> list[float]:
    """A point well inside the face to aim at, in cm. A point given with the face reference (e.g. where the user
    clicked in a viewer) wins; otherwise PointOnFace can sit right on the boundary, so try the middle of the face
    first and keep the first point a ray from outside really reaches on this face."""
    given = _AIM.get(safe(lambda: face.TransientKey))
    if given:
        return given
    p = face.PointOnFace
    fallback = [p.X, p.Y, p.Z]
    if face.SurfaceType != K.kPlaneSurface:
        return fallback
    q = [v * 0.1 for v in face_centroid(face)]
    if _ray_first_hit_is(s, face, [q[i] + n[i] * 100 for i in range(3)], [-n[0], -n[1], -n[2]]):
        return q
    return fallback


def _look_at_face(s: Session, face, at=None) -> None:
    """Point the camera straight at an interior point of the face from outside, zoomed in so the face
    fills the middle of the view and nothing else is under the centre pixel."""
    n = _normal(face)
    x0, y0, z0 = [v * 0.1 for v in at] if at else _aim_point(s, face, n)
    dirs = [n]
    # if something blocks the straight view (a face at the bottom of a pocket), try tilted views
    for tilt in (0.6, 1.5, 3.0):
        for ax in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            for sgn in (1, -1):
                t = [n[i] + tilt * sgn * ax[i] for i in range(3)]
                ln = math.sqrt(sum(v * v for v in t))
                if ln > 1e-6 and sum(t[i] * n[i] for i in range(3)) / ln > 0.2:
                    dirs.append([v / ln for v in t])
    chosen = n
    for d in dirs:
        eye = [x0 + d[0] * 100, y0 + d[1] * 100, z0 + d[2] * 100]
        hit = _ray_first_hit_is(s, face, eye, [-d[0], -d[1], -d[2]])
        if hit is None or hit:
            chosen = d
            break
    v = s.view()
    cam = v.Camera
    cam.Perspective = False
    cam.Target = s.tg.CreatePoint(x0, y0, z0)
    cam.Eye = s.tg.CreatePoint(x0 + chosen[0] * 100, y0 + chosen[1] * 100, z0 + chosen[2] * 100)
    up = (0, 0, 1) if abs(chosen[2]) < 0.9 else (0, 1, 0)
    cam.UpVector = s.tg.CreateUnitVector(*up)
    # zoom so roughly the face's smaller size spans a third of the view
    rb = face.Evaluator.RangeBox
    span = sorted([rb.MaxPoint.X - rb.MinPoint.X, rb.MaxPoint.Y - rb.MinPoint.Y, rb.MaxPoint.Z - rb.MinPoint.Z])
    # planar faces fill the middle of the view; a curved wall seen at an angle needs a much closer view
    size = max(span[1], 0.05) * 3 if face.SurfaceType == K.kPlaneSurface else max(span[0], 0.05) * 1.2
    safe(lambda: cam.SetExtents(size, size))
    cam.ApplyWithoutTransition()
    time.sleep(0.3)


OFFSETS = ((-0.08, 0.0), (0.08, 0.0), (0.0, -0.08), (0.0, 0.08))   # around the pick point, clear of glyphs


def _render_samples(s: Session) -> list[tuple[int, int, int]]:
    """Colours at the sample points in a small render Inventor makes of the current camera."""
    import struct
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "inventor_forge_pick.bmp")
    s.view().SaveAsBitmap(path, 101, 61)
    with open(path, "rb") as f:
        data = f.read()
    off = struct.unpack_from("<I", data, 10)[0]
    bw, bh = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0] // 8
    row = (bw * bpp + 3) & ~3
    out = []
    for fx, fy in OFFSETS:
        x, y = int(bw / 2 + fx * bw), int(abs(bh) / 2 + fy * abs(bh))
        if bh > 0:
            y = bh - 1 - y
        i = off + y * row + x * bpp
        out.append((data[i + 2], data[i + 1], data[i]))
    return out


def _screen_samples(s: Session) -> list[tuple[int, int, int]]:
    """Colours at the same points as actually shown on screen."""
    import win32gui
    hwnd = s.view().HWND
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    w, h = right - left, bottom - top
    cx, cy = win32gui.ClientToScreen(hwnd, (w // 2, h // 2))
    hdc = win32gui.GetDC(0)
    try:
        out = []
        for fx, fy in OFFSETS:
            c = win32gui.GetPixel(hdc, int(cx + fx * w), int(cy + fy * h))
            out.append((c & 255, (c >> 8) & 255, (c >> 16) & 255))
        return out
    finally:
        win32gui.ReleaseDC(0, hdc)


def _close(a, b, tol: int = 60) -> bool:
    return sum(abs(a[k] - b[k]) for k in range(3)) <= tol


def _vivid(rgb) -> bool:
    """Selection colours (green / blue) are saturated; unselected faces are grey."""
    return max(rgb) - min(rgb) >= 55


def _wait_screen_settled(s: Session, min_wait: float = 0.9, timeout: float = 5.0) -> None:
    """Inventor redraws a moment after the camera moves; wait until the pixels around the pick point
    stop changing (and at least min_wait) so the click lands on the new view, not the old one."""
    t0 = time.time()
    prev = _screen_samples(s)
    while time.time() - t0 < timeout:
        time.sleep(0.25)
        cur = _screen_samples(s)
        if time.time() - t0 >= min_wait and all(_close(a, b, 12) for a, b in zip(prev, cur)):
            return
        prev = cur


def _faces_label(dlg: int) -> str:
    try:
        return U.get_text(U.find(dlg, cid=10000))
    except InventorError:
        return ""


def _pick_face(s: Session, face, pid: int, dlg: int, attempts: int = 4) -> None:
    """Click the face in the viewport and confirm Inventor selected that face.

    Wait for the redraw after turning the camera, click the centre, move the cursor away (so the hover
    highlight disappears) and require the selection colour on screen around the pick point. A click
    that only activated Inventor's window selects nothing and is repeated; if something else got
    selected instead (the dialog says Faces but this face isn't highlighted), stop rather than add
    more faces."""
    import win32api
    import win32gui
    dl, dt, dr, db = win32gui.GetWindowRect(dlg)
    park = ((dl + dr) // 2, dt + 12)                 # the dialog's title bar
    label_before = _faces_label(dlg).lower()
    last = None
    first = None                                     # the face as it looked before any click
    for attempt in range(attempts):
        _look_at_face(s, face)
        safe(lambda: s.view().Update())
        # Inventor only repaints once the mouse is over the viewport: without this the click lands on the old view
        hwnd = s.view().HWND
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        win32api.SetCursorPos(win32gui.ClientToScreen(hwnd, (left + 40, top + 40)))
        time.sleep(0.25)
        win32api.SetCursorPos(win32gui.ClientToScreen(hwnd, (left + 44, top + 44)))
        _wait_screen_settled(s)
        win32api.SetCursorPos(park)
        time.sleep(0.3)
        before = _screen_samples(s)
        if first is None:
            first = before
        # the previous click registered late: the face now differs from how it looked before any click (comparing
        # with the original view, not "is it vivid", so a red or orange part can't fake a selection)
        if attempt and sum(1 for a, b in zip(first, before) if not _close(a, b, 45)) >= 3:
            return
        hwnd = s.view().HWND
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        cx, cy = win32gui.ClientToScreen(hwnd, ((right - left) // 2, (bottom - top) // 2))
        under = win32gui.WindowFromPoint((cx, cy))
        if under != hwnd and not win32gui.IsChild(hwnd, under):
            raise InventorError("the viewport centre is covered (by a dialog or another window) — cannot pick the face")
        _click_screen(cx, cy, pid, "the face in the viewport")
        win32api.SetCursorPos(park)
        time.sleep(0.8)
        last = _screen_samples(s)
        if os.environ.get("INVENTOR_FORGE_DEBUG"):
            print("pick", _faces_label(dlg), before, last)
        if sum(1 for a, b in zip(before, last) if not _close(a, b, 45)) >= 3:
            return                                   # the face changed to the selection colour
        if label_before.startswith("location") and not _faces_label(dlg).lower().startswith("location"):
            raise _WrongFace("a different face than the requested one got selected — stopped so the results "
                             "can't be wrong; rerun the analysis")
    raise InventorError(f"could not confirm the face pick after {attempts} clicks (screen colours {last})")


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _common_view(s: Session, faces) -> tuple | None:
    """A direction from which every face is the first thing seen at its aim point, or None."""
    data = [(_normal(f), f) for f in faces]
    pts = [(_aim_point(s, f, n), n, f) for n, f in data]
    cands = [_norm([sum(n[i] for n, _ in data) for i in range(3)])] + [n for n, _ in data]
    cands += [_norm([a, b, 1]) for a in (1, -1) for b in (1, -1)] + [_norm([a, b, -1]) for a in (1, -1) for b in (1, -1)]
    cands += [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]]
    for d in cands:
        if any(sum(n[i] * d[i] for i in range(3)) < 0.3 for _p, n, _f in pts):
            continue
        if all(_ray_first_hit_is(s, f, [p[i] + d[i] * 100 for i in range(3)], [-d[0], -d[1], -d[2]]) for p, _n, f in pts):
            return d, [p for p, _n, _f in pts]
    return None


def _set_view(s: Session, d, pts) -> tuple:
    """Camera along -d over all the points; returns a function mapping a model point (cm) to a screen pixel."""
    import win32gui
    target = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
    up0 = [0, 0, 1] if abs(d[2]) < 0.9 else [0, 1, 0]
    f = _norm([-d[0], -d[1], -d[2]])
    right = _norm(_cross(f, up0))
    up = _cross(right, f)
    xs = [sum((p[i] - target[i]) * right[i] for i in range(3)) for p in pts]
    ys = [sum((p[i] - target[i]) * up[i] for i in range(3)) for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0) * 1.6 + 2.0
    v = s.view()
    cam = v.Camera
    cam.Perspective = False
    safe(lambda: cam.SetExtents(span, span))          # zoom first: setting it later re-centres the camera
    cam.Target = s.tg.CreatePoint(*target)
    cam.Eye = s.tg.CreatePoint(*[target[i] + d[i] * 100 for i in range(3)])
    cam.UpVector = s.tg.CreateUnitVector(*up0)
    cam.ApplyWithoutTransition()
    time.sleep(0.4)
    cam = v.Camera
    ext = typed_call(cam, "GetExtents", 0.0, 0.0)
    w_cm, h_cm = float(ext[-2]), float(ext[-1])
    hwnd = v.HWND
    left, top, rgt, bot = win32gui.GetClientRect(hwnd)
    W, H = rgt - left, bot - top
    scale = min(W / max(w_cm, 1e-9), H / max(h_cm, 1e-9))
    cx, cy = win32gui.ClientToScreen(hwnd, (W // 2, H // 2))

    def to_px(p):
        q = [p[i] - target[i] for i in range(3)]
        return (int(round(cx + sum(q[i] * right[i] for i in range(3)) * scale)),
                int(round(cy - sum(q[i] * up[i] for i in range(3)) * scale)))
    return to_px


def _pixels(points, dx: int = 5) -> list[tuple[int, int, int]]:
    import win32gui
    hdc = win32gui.GetDC(0)
    try:
        out = []
        for x, y in points:
            for ox, oy in ((-dx, 0), (dx, 0), (0, -dx), (0, dx)):
                c = win32gui.GetPixel(hdc, x + ox, y + oy)
                out.append((c & 255, (c >> 8) & 255, (c >> 16) & 255))
        return out
    finally:
        win32gui.ReleaseDC(0, hdc)


def _pick_faces(s: Session, faces: list, pid: int, dlg: int) -> None:
    """Pick several faces: all from one view when there is one that sees them all (one camera move, much faster),
    checking on screen that each click took; otherwise one face at a time."""
    import win32api
    import win32gui
    if len(faces) < 2:
        for f in faces:
            _pick_face(s, f, pid, dlg)
        return
    view = _common_view(s, faces)
    if view is None:
        for f in faces:
            _pick_face(s, f, pid, dlg)
        return
    d, pts = view
    to_px = _set_view(s, d, pts)
    hwnd = s.view().HWND
    dl, dt, dr, _db = win32gui.GetWindowRect(dlg)
    park = ((dl + dr) // 2, dt + 12)
    left, top, _r, _b = win32gui.GetClientRect(hwnd)
    win32api.SetCursorPos(win32gui.ClientToScreen(hwnd, (left + 40, top + 40)))
    time.sleep(0.25)
    win32api.SetCursorPos(win32gui.ClientToScreen(hwnd, (left + 44, top + 44)))
    _wait_screen_settled(s)
    win32api.SetCursorPos(park)
    time.sleep(0.4)
    pix = [to_px(p) for p in pts]
    missed = []
    for f, xy in zip(faces, pix):
        under = win32gui.WindowFromPoint(xy)
        if under != hwnd and not win32gui.IsChild(hwnd, under):
            missed.append(f)
            continue
        ok = False
        first = None
        for _attempt in range(3):
            before = _pixels([xy])
            first = first or before
            _click_screen(xy[0], xy[1], pid, "a face in the viewport")
            win32api.SetCursorPos(park)
            time.sleep(0.7)
            after = _pixels([xy])
            if sum(1 for a, b in zip(before, after) if not _close(a, b, 45)) >= 3:
                ok = True
                break
            # an earlier click registered late: it differs from the original view (not "is it vivid": red parts)
            if sum(1 for a, b in zip(first, after) if not _close(a, b, 45)) >= 3:
                ok = True
                break
        if not ok:
            missed.append(f)
    for f in missed:                                   # anything not confirmed from the shared view: one at a time
        _pick_face(s, f, pid, dlg)


# --------------------------------------------------------------------------- command + dialog steps
def _run_command(s: Session, name: str) -> None:
    s.front()                                          # commands act on the active document
    s.app.CommandManager.ControlDefinitions.Item(name).Execute2(False)


def _front(h: int) -> None:
    import win32api
    import win32con
    import win32gui
    try:
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32gui.SetForegroundWindow(h)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception:
        pass
    time.sleep(0.3)


def _dialog(pid: int, title, timeout: float = 20.0, front: bool = True) -> int:
    """Wait for an Inventor dialog, park it at the left edge (clear of the viewport centre where faces
    are picked) and bring it to the front (not for steps that need no focus: front=False)."""
    import win32con
    import win32gui
    h = U.wait_window(pid, title, timeout)
    if not front:
        return h
    try:
        owner = win32gui.GetWindow(h, win32con.GW_OWNER) or h
        ml, mt, mr, mb = win32gui.GetWindowRect(win32gui.GetAncestor(owner, 2))
        win32gui.SetWindowPos(h, 0, ml + 8, mt + 160, 0, 0, win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)
    except Exception:
        pass
    _front(h)
    return h


def _ok(pid: int, h: int, what: str, timeout: float = 15.0) -> None:
    t0 = time.time()
    ok = U.find(h, cid=1)
    while not ok.enabled and time.time() - t0 < timeout:
        time.sleep(0.2)
        ok = U.find(h, cid=1)
    if not ok.enabled:
        U.click(U.find(h, cid=2))
        raise InventorError(f"{what}: Inventor did not accept the input (OK stayed disabled) — the face pick may "
                            "have missed; make sure Inventor is unobstructed on screen")
    U.click(ok)
    if not U.wait_gone(h, timeout):
        raise InventorError(f"{what}: dialog did not close")


STRESS_ADDIN = "{B3D04494-EDD2-4FDC-9EC2-30BAF8D6B77B}"


def _enter_analysis(s: Session) -> None:
    env = None
    for _ in range(20):          # the add-in loads in the background after Inventor starts; switch it on if needed
        env = safe(lambda: s.app.UserInterfaceManager.Environments.Item(FEA_ENV))
        if env is not None:
            break
        addin = safe(lambda: s.app.ApplicationAddIns.ItemById(STRESS_ADDIN))
        if addin is not None and not safe(lambda: addin.Activated, True):
            safe(lambda: addin.Activate())
        time.sleep(0.5)
    if env is None:
        raise InventorError("Inventor's Stress Analysis environment isn't available (it needs Inventor Professional "
                            "and its Stress Analysis add-in)")
    s.front().EnvironmentManager.SetCurrentEnvironment(env)
    time.sleep(0.8)


def _create_study(s: Session, pid: int, modal_modes: int = 0, study_name: str = "", contact_type: str = "",
                  model_state: str = "") -> None:
    _run_command(s, "FeaCreateSimCmd")
    h = _dialog(pid, "Create New Study")
    if modal_modes:
        U.click(U.find(h, cid=1401))                   # Modal Analysis
        time.sleep(0.4)
        if os.environ.get("INVENTOR_FORGE_DEBUG"):
            print("modal", [(c.cid, c.text, c.enabled, U.is_checked(c)) for c in U.controls(h) if c.cid in (1405, 1406)],
                  [(c.cid, c.enabled) for c in U.controls(h) if c.cid in (1409, 1410, 1411)])
        nm = U.find(h, cid=1405)                       # Number of Modes
        U.set_check(nm, True)
        time.sleep(0.3)
        _set_value(h, U.find(h, cid=1409, cls="Edit"), str(int(modal_modes)), pid)
    else:
        U.click(U.find(h, cid=1402))                   # Static Analysis
    if study_name:                                     # after the type: choosing a type resets the default name
        import win32gui
        time.sleep(0.3)
        win32gui.SendMessage(U.find(h, cid=1413).hwnd, 0x000C, 0, study_name)   # WM_SETTEXT; a plain text box
    combos = [c for c in U.controls(h) if c.cls == "ComboBox"]
    if contact_type:
        box = next((c for c in combos if contact_type.lower() in [x.lower() for x in U.combo_items(c)]), None)
        if box is None:
            raise InventorError(f"unknown contact type {contact_type!r}")
        U.combo_select(box, contact_type)
    if model_state:                                    # last: switching the model state can rebuild the dialog
        box = next((c for c in U.controls(h) if c.cls == "ComboBox" and model_state in U.combo_items(c)), None)
        if box is None:
            U.close(h)
            raise InventorError(f"model state {model_state!r} isn't offered for a study (see list_model_states)")
        U.combo_select(box, model_state)
        time.sleep(2.0)
        import win32gui
        if not (win32gui.IsWindow(h) and win32gui.IsWindowVisible(h)):
            time.sleep(1.0)
            return
    _ok(pid, h, "Create Study")
    time.sleep(0.8)


class _WrongFace(InventorError):
    pass


def _with_restart(pid: int, title: str, step) -> None:
    """A wrong pick can't be undone inside the dialog, so cancel it and do the step again from scratch."""
    for attempt in range(2):
        try:
            step()
            return
        except _WrongFace:
            for w in U.process_windows(pid):
                if title.lower() in win32gui_text(w).lower():
                    U.click(U.find(w, cid=2))
                    U.wait_gone(w, 10)
            time.sleep(0.5)
            if attempt:
                raise


def win32gui_text(h: int) -> str:
    import win32gui
    return safe(lambda: win32gui.GetWindowText(h), "") or ""


def _support(s: Session, pid: int, faces: list, kind: str = "Fixed") -> None:
    """A Fixed, Frictionless or Pin constraint on the faces."""
    title = f"{kind} Constraint"

    def step():
        _run_command(s, f"FeaCreateConstr{kind}Cmd")
        h = _dialog(pid, title)
        _pick_faces(s, faces, pid, h)
        _ok(pid, h, title)
    _with_restart(pid, title, step)


def _vector_load(s: Session, pid: int, cmd: str, title: str, faces: list, vec, unit: str, point=None) -> None:
    """Force, bearing, moment or remote-force load given by X/Y/Z components on picked faces."""
    def pick_step():
        nonlocal h
        _run_command(s, cmd)
        h = _dialog(pid, title)
        _pick_faces(s, faces, pid, h)
    h = 0
    _with_restart(pid, title, pick_step)
    U.set_check(U.find(h, cid=1225), True)              # Use Vector Components
    time.sleep(0.4)
    for cid, v in zip((1216, 1215, 1220), vec):
        _set_value(h, U.find(h, cid=cid), f"{float(v):g} {unit}", pid)
    if point is not None:                               # remote force: where the force acts
        for cid, v in zip((10067, 10068, 10069), point):
            _set_value(h, U.find(h, cid=cid), f"{float(v):g} mm", pid)
    _ok(pid, h, title)


def _local_mesh(s: Session, pid: int, faces: list, size_mm: float) -> None:
    def step():
        _run_command(s, "FeaCreateLocalMeshControlCmd")
        h = _dialog(pid, "Local Mesh Control")
        _pick_faces(s, faces, pid, h)
        _set_value(h, U.find(h, cid=10005), f"{float(size_mm):g} mm", pid)
        _ok(pid, h, "Local Mesh Control")
    _with_restart(pid, "Local Mesh Control", step)


def _convergence(s: Session, pid: int, refinements: int, stop_percent: float) -> None:
    """Inventor's own h-refinement: re-mesh where stress changes most until the peak settles."""
    _run_command(s, "FeaConvergenceCmd")
    h = _dialog(pid, "Convergence Settings")
    _set_value(h, U.find(h, cid=10007), str(int(refinements)), pid)
    _set_value(h, U.find(h, cid=10135), f"{float(stop_percent):g}", pid)
    _ok(pid, h, "Convergence Settings")


def _auto_contacts(s: Session, pid: int) -> None:
    _run_command(s, "FeaCreateContactAutoCmd")
    t0 = time.time()
    while time.time() - t0 < 120:                      # large assemblies take a while; a small dialog may report it
        time.sleep(1.0)
        if not U.solver_windows([pid]) and not any(c == "#32770" and "contact" in t.lower() for _h, c, t in U.window_rows(pid)):
            break
    for h, c, t in U.window_rows(pid):
        if c == "#32770" and h != s.app.MainFrameHWND and t in ("Stress Analysis", "Automatic Contacts"):
            ok = next((x for x in U.find_all(h, cls="Button") if x.text.replace("&", "") in ("OK", "Close", "Yes")), None)
            if ok:
                U.click(ok)


def _force(s: Session, pid: int, faces: list, vec) -> None:
    def pick_step():
        nonlocal h
        _run_command(s, "FeaCreateLoadForceCmd")
        h = _dialog(pid, "Force")
        _pick_faces(s, faces, pid, h)
    h = 0
    _with_restart(pid, "Force", pick_step)
    U.set_check(U.find(h, cid=1225), True)              # Use Vector Components
    time.sleep(0.4)
    for cid, v in zip((1216, 1215, 1220), vec):
        _set_value(h, U.find(h, cid=cid), f"{float(v):g}", pid)
    _ok(pid, h, "Force")


def _pressure(s: Session, pid: int, faces: list, mpa: float) -> None:
    def pick_step():
        nonlocal h
        _run_command(s, "FeaCreateLoadPressureCmd")
        h = _dialog(pid, "Pressure")
        _pick_faces(s, faces, pid, h)
    h = 0
    _with_restart(pid, "Pressure", pick_step)
    _set_value(h, U.find(h, cid=10015), f"{float(mpa):g} MPa", pid)
    _ok(pid, h, "Pressure")


def _gravity(s: Session, pid: int) -> None:
    """Self-weight in -Z. Inventor's dialog aims gravity into the face you pick, so pick one looking up (+Z)."""
    top = find_planar_face(s, "Z") if s.kind() == "part" else _top_face_any(s)
    if top is None:
        raise InventorError("gravity needs a flat face looking straight up (+Z) to set Inventor's direction")

    def pick_step():
        nonlocal h
        _run_command(s, "FeaCreateLoadGravityCmd")
        h = _dialog(pid, "Gravity")
        _pick_face(s, top, pid, h)
    h = 0
    _with_restart(pid, "Gravity", pick_step)
    _set_value(h, U.find(h, cid=10015), "9810 mm/s^2", pid)
    _ok(pid, h, "Gravity")


def _top_face_any(s: Session):
    """Highest flat face looking straight up, in any component of an assembly."""
    best, z = None, -1e99
    for o in items(s.doc().ComponentDefinition.Occurrences.AllLeafOccurrences):
        for f in items(safe(lambda o=o: o.SurfaceBodies.Item(1).Faces) or []):
            if f.SurfaceType != K.kPlaneSurface:
                continue
            n = face_normal(f)
            if n[2] > 0.999:
                c = f.PointOnFace.Z
                if c > z:
                    best, z = f, c
    return best


def _mesh(s: Session, pid: int, avg_fraction: float, allow_typing: bool = True) -> None:
    _run_command(s, "FeaMeshSettingCmd")
    h = _dialog(pid, "Mesh Settings")
    _set_value(h, U.find(h, cid=10081), f"{avg_fraction:g}", pid, allow_typing)
    if not U.find(h, cid=1).enabled:                  # already that size: Inventor leaves OK disabled
        U.close(h)
        U.wait_gone(h, 10)
        return
    _ok(pid, h, "Mesh Settings")


def _simulate(s: Session, pid: int, timeout: float = 3600.0, front: bool = True) -> tuple[float, list[str]]:
    """Run the active study and wait for it using Windows calls only (Inventor can crash if it gets COM calls
    during a solve). Returns seconds taken and any solver warnings."""
    _run_command(s, "FeaSimulateCmd")
    h = _dialog(pid, "Simulate", front=front)
    t0 = time.time()
    U.click(U.find(h, cid=1))                          # Run
    warnings: list[str] = []
    finished_at = 0.0
    while True:
        if pid not in U.pids_named("Inventor.exe"):
            raise InventorError("Inventor closed during the solve; nothing from this run was stored")
        open_ = [w for w, cls, title in U.window_rows(pid) if cls == "#32770" and title == "Simulate"]
        if not open_:
            break
        try:
            for w, cls, title in U.window_rows(pid):         # never let a stray press cancel a running solve
                if cls == "#32770" and title == "Stress Analysis" and any("cancel study" in c.text.lower() for c in U.controls(w)):
                    U.click(U.find(w, text="No"))
            buttons = {c.text.replace("&", "").lower(): c for c in U.find_all(open_[0], cls="Button") if c.text}
            status = " ".join(c.text.lower() for c in U.find_all(open_[0], cls="Static"))
            if "close" in buttons and "cancel" not in buttons and "finished" in status:
                # with no warnings the dialog closes itself in a moment; with warnings it waits to be closed
                finished_at = finished_at or time.time()
                if time.time() - finished_at > 6:
                    warnings = U.listbox_items(open_[0])
                    U.click(buttons["close"])
                    U.wait_gone(open_[0], 30)
                    break
        except Exception as e:                               # the dialog closed while being read
            if type(e).__name__ != "error":
                raise
        if time.time() - t0 > timeout:
            raise InventorError(f"the solve is still running after {timeout:.0f} s; leave Inventor alone until it ends")
        time.sleep(0.5)
    time.sleep(1.0)
    fatal = [w for w in warnings if any(k in w.lower() for k in FATAL_SOLVE)]
    if fatal:
        raise InventorError("the solve reported a broken mesh, so its results can't be used: " + "; ".join(fatal[:3]) +
                            ". Only the solver finds these (meshing alone passes); look for slivers, tiny faces or "
                            "near-tangent contacts, or change the element size")
    return time.time() - t0, warnings


FATAL_SOLVE = ("degenerate element", "negative jacobian", "distorted element")


def _answer_results_prompt(pid: int) -> int:
    """Inventor's "results are out of date, continue?" prompt is a WPF window with no controls to read."""
    import win32con
    import win32gui
    n = 0
    for h, cls, title in U.window_rows(pid):
        if title == "Stress Analysis" and cls.startswith("HwndWrapper"):
            _front(h)
            win32gui.PostMessage(h, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
            win32gui.PostMessage(h, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
            n += 1
    return n


def _tick_only_study(s: Session, pid: int, h: int, study: str) -> None:
    """In the Report dialog: Custom, Studies page, untick everything, tick one study with a real click (the tree ignores
    ticks set by messages, and an empty selection gives an empty report)."""
    import win32api
    U.click(U.find(h, cid=10107))                      # Custom
    time.sleep(0.8)
    tab = next(c for c in U.controls(h) if c.cls == "SysTabControl32" and "Studies" in U.tab_labels(c.hwnd))
    U.tab_select(tab.hwnd, U.tab_labels(tab.hwnd).index("Studies"))
    U.click(U.find(h, cid=10117))                      # Select All: ticks Inventor takes in full, results included
    time.sleep(0.5)
    tree = U.find(h, cid=10120)
    roots = [it for it in U.tree_items(tree.hwnd) if it["depth"] == 0]
    if not any(it["label"].strip() == study for it in roots):
        raise InventorError(f"study {study!r} is not in the report's list")
    _front(h)

    def state(item):
        return next(it for it in U.tree_items(tree.hwnd) if it["item"] == item)["checked"]

    def tick(item, want: bool) -> bool:
        x0, y0, _x1, y1 = U.tree_item_rect(tree.hwnd, item)
        y = (y0 + y1) // 2
        for x in (x0 - 9, x0 - 11, x0 - 7):            # the tick box sits just left of the label
            _click_screen(x, y, pid, "a tick box in the report's study list")
            win32api.SetCursorPos((x0 + 120, y1 + 60))
            time.sleep(0.4)
            if state(item) == want:
                return True
        return False

    for it in roots:                                   # untick every other study
        if it["label"].strip() != study and state(it["item"]) and not tick(it["item"], False):
            raise InventorError(f"could not untick study {it['label']!r} in the report's list")
    if os.environ.get("INVENTOR_FORGE_DEBUG"):
        print("report tree", [(it["depth"], it["label"], it["checked"]) for it in U.tree_items(tree.hwnd)][:40], flush=True)


def _report(s: Session, pid: int, name: str, timeout: float = 1800.0, front: bool = True, only_study: str = "") -> str:
    """Generate Inventor's report and return its path. Covers every study in the file, so it takes minutes on a
    file with many; read_result_maximum is the quick way to get peak values."""
    import win32gui
    # everything that needs COM happens before the report starts
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    folders = [os.path.join(docs, "Inventor"), docs]
    proj = safe(lambda: s.app.DesignProjectManager.ActiveDesignProject.WorkspacePath, "")
    if proj:
        folders.insert(0, proj)
    part_dir = os.path.dirname(safe(lambda: s.doc().FullFileName, "") or "")
    if part_dir:
        folders.insert(0, part_dir)
    main = s.app.MainFrameHWND
    _enter_analysis(s)
    _run_command(s, "FeaExResultsCmd")                # the report won't start while a result is on show
    time.sleep(1.0)
    _run_command(s, "FeaGenerateReportCmd")
    t0 = time.time()
    h = 0
    while not h:                                       # slow to appear on files with many studies
        _answer_results_prompt(pid)
        try:
            h = _dialog(pid, "Report", timeout=3, front=front)
        except InventorError:
            if time.time() - t0 > 180:
                raise
    U.set_text(U.find(h, cid=10110), plain_name(name, "report"))      # file name (a plain edit box)
    if only_study:
        _tick_only_study(s, pid, h, only_study)
    started = time.time()
    prompts_before = _openwith_pids()
    U.click(U.find(h, cid=1))
    U.wait_gone(h, 30)
    time.sleep(2.0)
    while any("report" in win32gui_text(w).lower() for w in U.process_windows(pid) if w != main):
        if pid not in U.pids_named("Inventor.exe"):
            raise InventorError("Inventor closed while writing the report")
        if time.time() - started > timeout:
            raise InventorError(f"the report is still running after {timeout:.0f} s; leave Inventor alone until it ends")
        time.sleep(1.0)
    _close_open_with_prompts(prompts_before)
    t0 = time.time()
    timeout = 120.0
    while time.time() - t0 < timeout:
        for fo in folders:
            for p in glob.glob(os.path.join(fo, "**", name + ".htm*"), recursive=True) if os.path.isdir(fo) else []:
                if os.path.getmtime(p) >= started - 2:
                    time.sleep(1.0)
                    return p
        time.sleep(0.5)
    raise InventorError("Inventor's stress report was not found after generating it")


def _openwith_pids() -> set[int]:
    return set(U.pids_named("OpenWith.exe"))


def _close_open_with_prompts(existing: set[int], wait: float = 6.0) -> int:
    """Inventor opens the finished report with the .html handler; when Windows has none it starts an
    'Open with' chooser (OpenWith.exe) that sits over Inventor and would block the next clicks. End only
    choosers that appeared during this step (not ones that were already there)."""
    import win32api
    import win32process
    closed = 0
    t0 = time.time()
    while time.time() - t0 < wait:
        for pid in _openwith_pids() - existing:
            try:
                hp = win32api.OpenProcess(0x0001, False, pid)    # PROCESS_TERMINATE
                win32process.TerminateProcess(hp, 0)
                win32api.CloseHandle(hp)
                closed += 1
            except Exception:
                pass
        if closed:
            break
        time.sleep(0.5)
    return closed


# --------------------------------------------------------------------------- report parsing
def _report_text(path: str) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        t = f.read()
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<img[^>]*>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</tr>|</p>|</h\d>|</div>", "\n", t, flags=re.I)
    t = re.sub(r"</td>|</th>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return [re.sub(r"\s+", " ", ln).strip() for ln in t.splitlines() if ln.strip()]


def _num(text: str) -> float | None:
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)", text.replace(",", ""))
    return float(m.group(1)) if m else None


def _parse_own(path: str, study_name: str) -> dict:
    """The study this call created: by its name when it was given one, else the newest in the report."""
    if study_name:
        try:
            return parse_report(path, study_name)
        except InventorError:
            pass
    return parse_report(path)


def parse_report(path: str, study: str = "") -> dict:
    """Parse one study of an Inventor stress report (the report lists every study in the document;
    by default the last one, i.e. the study just run)."""
    lines = _report_text(path)
    heads = [i for i, ln in enumerate(lines)            # a study's heading comes right before its settings table
             if i + 1 < len(lines) and lines[i + 1].lower().startswith("general objective and settings")]
    if heads:
        pick = [i for i in heads if lines[i].lower() == study.lower()] if study else []
        if study and not pick:
            raise InventorError(f"study {study!r} is not in this report; it has {[lines[i] for i in heads]}")
        start = pick[0] if pick else heads[-1]
        nxt = [i for i in heads if i > start]
        name = lines[start]
        lines = lines[start:nxt[0] if nxt else len(lines)]
    else:
        name = ""
    out: dict = {"study": name, "summary": {}, "reactions": [], "loads": [], "material": {}}
    if "Avg. Element Size (fraction of model diameter)" in lines:
        k = lines.index("Avg. Element Size (fraction of model diameter)")
        out["mesh_avg_size_fraction"] = _num(lines[k + 1])
    try:
        i = lines.index("Result Summary")
    except ValueError:
        i = -1
    if i >= 0:
        j = i + 1
        while j < len(lines) and lines[j] in ("Name", "Minimum", "Maximum"):
            j += 1
        while j + 2 < len(lines) and lines[j] not in ("Figures",):
            name, lo, hi = lines[j], lines[j + 1], lines[j + 2]
            if _num(lo) is None or _num(hi) is None:
                if _num(lo) is not None:            # single-value rows (Volume, Mass)
                    out["summary"][name] = {"value": _num(lo), "unit": lo.split()[-1] if " " in lo else ""}
                    j += 2
                    continue
                j += 1
                continue
            unit = hi.split()[-1] if " " in hi else ""
            out["summary"][name] = {"min": _num(lo), "max": _num(hi), "unit": unit}
            j += 3
    # material block
    for key, label in (("Yield Strength", "yield_MPa"), ("Young's Modulus", "E"), ("Poisson's Ratio", "poisson"),
                       ("Mass Density", "density")):
        if key in lines:
            k = lines.index(key)
            out["material"][label] = lines[k + 1]
    if "Name" in lines:
        k = lines.index("Name")
        out["material"]["name"] = lines[k + 1] if k + 1 < len(lines) else ""
    # reactions: rows start with "<constraint name>:" then magnitude
    if "Reaction Force and Moment on Constraints" in lines:
        k = lines.index("Reaction Force and Moment on Constraints") + 1
        while k < len(lines) and lines[k] != "Result Summary":
            if re.match(r".+:\d+$", lines[k]) and k + 1 < len(lines) and lines[k + 1].endswith(" N"):
                vals = lines[k + 1:k + 9]
                forces = [_num(v) for v in vals if v.endswith(" N")]
                moments = [_num(v) for v in vals if v.endswith(" N m")]
                out["reactions"].append({"constraint": lines[k], "force_N": forces[0] if forces else None,
                                         "force_components_N": forces[1:4], "moment_Nm": moments[0] if moments else None,
                                         "moment_components_Nm": moments[1:4]})
                k += 9
            else:
                k += 1
    # loads echo
    for k, ln in enumerate(lines):
        if ln == "Load Type" and k + 1 < len(lines):
            entry = {"type": lines[k + 1]}
            for m in range(k + 2, min(k + 14, len(lines) - 1)):
                if lines[m] in ("Magnitude", "Vector X", "Vector Y", "Vector Z"):
                    entry[lines[m].lower().replace(" ", "_")] = lines[m + 1]
            out["loads"].append(entry)
    # modal frequencies (rows like "F1" / "Frequency 1" followed by "123.4 Hz")
    freqs = []
    for k, ln in enumerate(lines[:-1]):
        if re.match(r"^(F\d+|Frequency\s*\d+|Mode\s*\d+)$", ln) and lines[k + 1].endswith("Hz"):
            freqs.append(_num(lines[k + 1]))
    if freqs:
        out["frequencies_Hz"] = freqs
    return out


# --------------------------------------------------------------------------- tools
def _check_ready(s: Session) -> None:
    d = s.doc()
    if s.kind(d) == "assembly":
        generic = []
        for o in items(d.ComponentDefinition.Occurrences.AllLeafOccurrences):
            md = safe(lambda o=o: o.Definition.Document)
            if md is not None and safe(lambda md=md: md.ActiveMaterial.DisplayName, "Generic") in ("Generic", "", None):
                generic.append(o.Name)
        if generic:
            raise InventorError(f"components with Inventor's Generic material (no strength data): {generic[:10]} — set a material in each part first")
    else:
        d = s.part()
        if s.cdef().SurfaceBodies.Count == 0:
            raise InventorError("the part has no solid body")
        if safe(lambda: d.ActiveMaterial.DisplayName, "Generic") in ("Generic", "", None):
            raise InventorError("assign a material first (set_material) — Inventor's Generic material has no strength data")
    free_gb = shutil.disk_usage(os.path.expanduser("~")).free / 2 ** 30
    if free_gb < 2:
        raise InventorError(f"only {free_gb:.1f} GB free on the system drive; a solved study can use hundreds of MB")


KBM_NOTICE = (
    "Heads-up: Inventor's Stress Analysis has no automation API, so this analysis is run by TAKING OVER YOUR "
    "MOUSE AND KEYBOARD for about 30-60 seconds. Inventor will come to the front, its camera will move, faces "
    "will be clicked and dialogs filled in automatically. Please save your work in other apps, keep Inventor "
    "visible and unobstructed, and don't touch the mouse or keyboard until it finishes. Any click that would "
    "land outside Inventor stops the run. Reply 'go' (or similar) when ready."
)


def _notice_gate(user_notified: bool, what: str) -> dict | None:
    if user_notified:
        return None
    return {"status": "needs_user_go_ahead", "notice": KBM_NOTICE,
            "next": f"Nothing has run yet. Show the notice to the user word for word, wait for their go-ahead, then "
                    f"call {what} again with the same arguments plus user_notified=true."}


@tool("analysis")
def analyze_stress(s: Session, fixed_faces: list | None = None, loads: list[dict] | None = None, gravity: bool = False,
                   mesh_fraction: float = 0.0, name: str = "", user_notified: bool = False,
                   frictionless_faces: list | None = None, pin_faces: list | None = None,
                   study_name: str = "", contact_type: str = "", contacts: list[dict] | None = None,
                   local_mesh: list[dict] | None = None, refinements: int = 0, convergence_percent: float = 10.0,
                   model_state: str = "") -> dict:
    """Static stress analysis in Inventor's own Stress Analysis environment. TAKES OVER THE MOUSE AND
    KEYBOARD for the duration (faces are picked in the viewport; Inventor must be visible and nobody should
    touch the mouse). Creates a study, fixes the faces, applies the loads, simulates, and reads Inventor's
    stress report: max von Mises and principal stresses, max displacement, min safety factor, reactions
    (equilibrium check against the applied loads), plus a picture of the stress plot.

    fixed_faces: face indices (list_faces) held fixed
    loads: [{"face": 5, "force": [0, 0, -200]} (N, total)  or  {"face": 5, "pressure": 2.0} (MPa)]; use
      "faces": [5, 6] for several faces (a force is then the total, shared across them)
    frictionless_faces: face indices that can slide but not lift (they hold in tension too)
    pin_faces: cylindrical faces free to rotate about their axis
    study_name: name for the study in Inventor's browser
    Assemblies: faces are "Component:1/5" (face 5 of that component, numbered as in inspect_assembly(faces_of=...)).
    contact_type: default type for the automatic contacts between touching components: Bonded (default), Separation,
      Sliding / No Separation, Separation / No Sliding, Shrink Fit / Sliding, Shrink Fit / No Sliding, Spring
    contacts: change particular contacts afterwards: [{"between": ["Base:1", "Beam:1"], "type": "Separation"}]
    loads also take {"face": f, "moment": [Mx, My, Mz]} (N·m), {"face": f, "bearing": [Fx, Fy, Fz]} (N, on a round face),
      {"face": f, "remote_force": [Fx, Fy, Fz], "point": [x, y, z]} (N, acting at a point in mm, e.g. a tow hook)
    local_mesh: finer mesh on chosen faces: [{"faces": [5], "size": 2.0}] (mm)
    refinements: Inventor's own mesh convergence: up to this many automatic refinements where stress changes most
    convergence_percent: stop refining when the peak changes less than this
    model_state: analyse the assembly in this model state
    gravity: add self-weight in -Z
    mesh_fraction: Inventor's average element size as a fraction of the bounding box (default 0.1;
      0.05 is finer and slower) — run twice with two values to check convergence
    user_notified: must be true to run. Without it nothing happens and the tool returns a notice that you
      must show the user (mouse + keyboard takeover) and get their go-ahead for first.
    """
    gate = _notice_gate(user_notified, "analyze_stress")
    if gate:
        return gate
    fixed_faces, frictionless_faces, pin_faces = fixed_faces or [], frictionless_faces or [], pin_faces or []
    if not (fixed_faces or frictionless_faces or pin_faces):
        raise InventorError("give at least one supported face (fixed_faces, frictionless_faces or pin_faces)")
    loads = loads or []
    if not loads and not gravity:
        raise InventorError("no loads — give loads=[...] and/or gravity=true")
    _check_ready(s)
    fixed = [face_ref(s, i) for i in fixed_faces]
    sliding = [face_ref(s, i) for i in frictionless_faces]
    pinned = [face_ref(s, i) for i in pin_faces]
    load_faces = [([face_ref(s, i) for i in (ld.get("faces") or [ld["face"]])], ld) for ld in loads]
    meshes = [([face_ref(s, i) for i in m.get("faces") or [m["face"]]], float(m["size"])) for m in local_mesh or []]
    is_asm = s.kind() == "assembly"
    pid = _pid(s)
    applied = [0.0, 0.0, 0.0]
    tag = plain_name(name, "") or f"forge_{time.strftime('%H%M%S')}"
    t0 = time.time()
    _bring_to_front(s)
    _enter_analysis(s)
    _create_study(s, pid, study_name=study_name, contact_type=contact_type, model_state=model_state)
    contact_rows = []
    if is_asm:
        _auto_contacts(s, pid)
        from .studies import _contact_rows, _set_contact
        for c in contacts or []:
            _set_contact(s, pid, study_name or None, c.get("between") or [], c["type"])
        contact_rows = safe(lambda: _contact_rows(s, study_name or None), [])
    if mesh_fraction:
        _mesh(s, pid, mesh_fraction)
    for mfaces, size in meshes:
        _local_mesh(s, pid, mfaces, size)
    if refinements:
        _convergence(s, pid, refinements, convergence_percent)
    if fixed:
        _support(s, pid, fixed)
    if sliding:
        _support(s, pid, sliding, "Frictionless")
    if pinned:
        _support(s, pid, pinned, "Pin")
    for faces, ld in load_faces:
        if ld.get("force"):
            vec = [float(v) for v in ld["force"]]
            _force(s, pid, faces, vec)
            applied = [applied[i] + vec[i] for i in range(3)] if applied is not None else None
        elif ld.get("bearing"):
            vec = [float(v) for v in ld["bearing"]]
            _vector_load(s, pid, "FeaCreateLoadBearingCmd", "Bearing Load", faces, vec, "N")
            applied = [applied[i] + vec[i] for i in range(3)] if applied is not None else None
        elif ld.get("remote_force"):
            vec = [float(v) for v in ld["remote_force"]]
            _vector_load(s, pid, "FeaCreateLoadRemoteForceCmd", "Remote Force", faces, vec, "N", point=ld.get("point") or [0, 0, 0])
            applied = [applied[i] + vec[i] for i in range(3)] if applied is not None else None
        elif ld.get("moment"):
            vec = [float(v) * 1000.0 for v in ld["moment"]]          # N·m -> N mm, the dialog's unit
            _vector_load(s, pid, "FeaCreateLoadMomentCmd", "Moment", faces, vec, "N mm")
        elif ld.get("pressure"):
            p = float(ld["pressure"])
            _pressure(s, pid, faces, p)
            for face in faces:
                if face.SurfaceType != K.kPlaneSurface:          # curved: resultant not tracked
                    applied = None
                elif applied is not None:
                    n = face_normal(face)
                    area = face.Evaluator.Area * 100
                    applied = [applied[i] - p * area * n[i] for i in range(3)]
        else:
            raise InventorError(f"load needs force, pressure, bearing, moment or remote_force: {ld}")
    if gravity:
        _gravity(s, pid)
        w = _any_cdef(s).MassProperties.Mass * 9.81
        if applied is not None:
            applied[2] -= w
    solve_s, solver_warnings = _simulate(s, pid)
    report_error = None
    try:
        report = _report(s, pid, tag)
        res = _parse_own(report, study_name)
    except InventorError as e:              # the study is solved: read the peaks directly rather than lose it
        report_error, report = str(e), ""
        for w, _cls, title in U.window_rows(pid):
            if _cls == "#32770" and title in ("Report", "Stress Analysis Report"):
                safe(lambda w=w: U.click(U.find(w, cid=2)))       # Cancel a half-open report dialog
        from .studies import _read_max
        from .inspect import material_data
        sm = {}
        for key in ("Von Mises Stress", "Displacement", "Safety Factor"):
            try:
                v = _read_max(s, pid, study_name, key)
                sm[key] = {"max": v, "min": v if key == "Safety Factor" else None}
            except Exception:
                pass
        md = safe(lambda: material_data(s), {}) or {}
        res = {"summary": sm, "reactions": [], "loads": [], "study": study_name, "mesh_avg_size_fraction": None,
               "material": {"name": md.get("name", ""), "yield_MPa": str(md.get("yield_MPa") or "")}}
    sm = res["summary"]

    def mx(key, field="max"):
        return (sm.get(key) or {}).get(field)

    out = {"engine": "Autodesk Inventor Stress Analysis", "study": res.get("study") or "Static Analysis",
           "mesh_avg_size_fraction": res.get("mesh_avg_size_fraction"),
           "max_von_mises_MPa": mx("Von Mises Stress"),
           "max_principal_MPa": mx("1st Principal Stress"), "min_principal_MPa": mx("3rd Principal Stress", "min"),
           "max_displacement_mm": mx("Displacement"),
           "safety_factor_min": mx("Safety Factor", "min"),
           "reactions": res["reactions"], "loads_echo": res["loads"], "material": res["material"],
           "report": report, "solve_seconds": r(solve_s, 1), "total_seconds": r(time.time() - t0, 1)}
    if report_error:
        out["report_error"] = (f"Inventor's report did not open ({report_error}); peaks were read from the results "
                               "directly, so there are no reactions and equilibrium is unverified")
    if solver_warnings:
        out["solver_warnings"] = solver_warnings
    if contact_rows:
        out["contacts"] = contact_rows
    y = _num(res["material"].get("yield_MPa", "") or "")
    if y and out["max_von_mises_MPa"]:
        out["safety_factor_yield"] = r(y / out["max_von_mises_MPa"], 3)
    if applied is not None and res["reactions"]:
        rf = [sum((rc.get("force_components_N") or [0, 0, 0])[k] or 0 for rc in res["reactions"]) for k in range(3)]
        mag = math.sqrt(sum(a * a for a in applied)) or 1e-9
        out["applied_load_N"] = [r(v, 3) for v in applied]
        out["reaction_N"] = [r(v, 3) for v in rf]
        out["equilibrium_error_pct"] = r(100 * math.sqrt(sum((rf[k] + applied[k]) ** 2 for k in range(3))) / mag, 3)
    # independent check that the right faces were picked: the reaction moment at a single fixed face
    # must equal the moment of the applied forces about that face's centre
    force_loads = [(fs[0], ld) for fs, ld in load_faces if ld.get("force") and len(fs) == 1]
    if (len(fixed) == 1 and not sliding and not pinned and force_loads and len(force_loads) == len(load_faces)
            and not gravity and res["reactions"] and not is_asm):
        c0 = face_centroid(fixed[0])
        m = [0.0, 0.0, 0.0]
        for f, ld in force_loads:
            c = face_centroid(f)
            rv = [(c[i] - c0[i]) / 1000.0 for i in range(3)]          # m
            fv = [float(v) for v in ld["force"]]
            m = [m[0] + rv[1] * fv[2] - rv[2] * fv[1], m[1] + rv[2] * fv[0] - rv[0] * fv[2],
                 m[2] + rv[0] * fv[1] - rv[1] * fv[0]]
        expected = math.sqrt(sum(v * v for v in m))
        got = res["reactions"][0].get("moment_Nm") or 0.0
        out["moment_check"] = {"expected_Nm": r(expected, 4), "reaction_Nm": r(got, 4)}
        if abs(got - expected) > 0.05 * max(expected, 1e-6) + 0.01:
            out["warning"] = ("the reaction moment does not match the loads on the faces you asked for — a face pick "
                              "probably went to the wrong face; do not use these results, rerun the analysis")
            out["moment_check"]["ok"] = False
        else:
            out["moment_check"]["ok"] = True
    png = os.path.join(s.workdir, "renders", f"{tag}_von_mises.png")
    os.makedirs(os.path.dirname(png), exist_ok=True)
    safe(lambda: _iso_result_view(s))
    safe(lambda: s.view().SaveAsBitmap(png, 1600, 1000))
    if os.path.exists(png):
        out["path"] = png
    out["note"] = ("Inventor's default mesh is coarse; rerun with mesh_fraction=0.05 and compare the peak to "
                   "check convergence. The results stay in Inventor's Stress Analysis environment.")
    return out


def _iso_result_view(s: Session) -> None:
    cam = s.view().Camera
    cam.ViewOrientationType = K.kIsoTopRightViewOrientation
    cam.Fit()
    cam.ApplyWithoutTransition()


@tool("analysis")
def analyze_modes(s: Session, fixed_faces: list, modes: int = 6, name: str = "",
                  user_notified: bool = False, study_name: str = "") -> dict:
    """Natural frequencies with Inventor's Stress Analysis (modal study). TAKES OVER THE MOUSE AND KEYBOARD
    while it runs (faces are picked in the viewport). Returns the frequencies from Inventor's report.
    Needs user_notified=true (see analyze_stress)."""
    gate = _notice_gate(user_notified, "analyze_modes")
    if gate:
        return gate
    if not fixed_faces:
        raise InventorError("give at least one fixed face")
    _check_ready(s)
    fixed = [face_ref(s, i) for i in fixed_faces]
    pid = _pid(s)
    tag = plain_name(name, "") or f"forge_modal_{time.strftime('%H%M%S')}"
    _bring_to_front(s)
    _enter_analysis(s)
    _create_study(s, pid, modal_modes=int(modes), study_name=study_name)
    if s.kind() == "assembly":
        _auto_contacts(s, pid)
    _support(s, pid, fixed)
    solve_s, _ = _simulate(s, pid)
    report = _report(s, pid, tag)
    res = _parse_own(report, study_name)
    freqs = res.get("frequencies_Hz", [])
    return {"engine": "Autodesk Inventor Stress Analysis", "study": res.get("study") or "Modal Analysis",
            "frequencies_Hz": freqs[:int(modes)] if len(freqs) >= int(modes) else freqs,
            "all_frequencies_Hz": freqs, "mesh_avg_size_fraction": res.get("mesh_avg_size_fraction"),
            "report": report, "solve_seconds": r(solve_s, 1)}


@tool("analysis")
def exit_analysis(s: Session) -> dict:
    """Leave Inventor's Stress Analysis environment and return to modelling."""
    d = s.doc()
    base = d.EnvironmentManager.BaseEnvironment
    d.EnvironmentManager.SetCurrentEnvironment(base)
    return {"environment": safe(lambda: base.InternalName, "base")}


@tool("analysis", mutates=False)
def read_stress_report(s: Session, path: str) -> dict:
    """Parse an Inventor Stress Analysis HTML report (results summary, reactions, loads, material)."""
    if not os.path.exists(path):
        raise InventorError(f"no such report: {path}")
    return parse_report(path)
