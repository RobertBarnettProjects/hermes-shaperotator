"""Driving Inventor's own dialogs (for features that have no API, like Stress Analysis).

Inventor's Stress Analysis environment is only reachable through its commands and their Win32
dialogs. Commands are started through the API (with the faces pre-selected through the API); the
dialogs they open are then filled in and confirmed with window messages, the same as a person typing
into the fields and clicking the buttons. While this runs Inventor's window must not be used by
anyone else.
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from dataclasses import dataclass

import win32api
import win32con
import win32gui
import win32process

from .com import InventorError


def _dpi_aware() -> bool:
    """Work in physical pixels, like Inventor (otherwise Windows scales our coordinates on high-DPI
    screens and clicks / pixel reads land in the wrong place)."""
    import ctypes
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):   # per-monitor v2
            return True
    except Exception:
        pass
    try:
        return ctypes.windll.user32.IsProcessDPIAware() != 0 or bool(ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0)
    except Exception:
        return False


DPI_AWARE = _dpi_aware()


@dataclass
class Ctrl:
    hwnd: int
    cls: str
    text: str
    cid: int
    visible: bool
    enabled: bool


def _info(h: int) -> Ctrl:
    return Ctrl(h, win32gui.GetClassName(h), win32gui.GetWindowText(h), win32gui.GetDlgCtrlID(h),
                bool(win32gui.IsWindowVisible(h)), bool(win32gui.IsWindowEnabled(h)))


def process_windows(pid: int) -> list[int]:
    out: list[int] = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32process.GetWindowThreadProcessId(h)[1] == pid:
            out.append(h)
        return True
    win32gui.EnumWindows(cb, None)
    return out


def wait_window(pid: int, title: str | tuple[str, ...], timeout: float = 30.0, cls: str = "#32770") -> int:
    titles = (title,) if isinstance(title, str) else title
    t0 = time.time()
    while time.time() - t0 < timeout:
        for h in process_windows(pid):
            try:
                if win32gui.GetClassName(h) == cls and any(t.lower() in win32gui.GetWindowText(h).lower() for t in titles):
                    return h
            except Exception:
                continue
        time.sleep(0.15)
    raise InventorError(f"Inventor dialog {titles} did not appear within {timeout:.0f} s")


def wait_gone(h: int, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not win32gui.IsWindow(h) or not win32gui.IsWindowVisible(h):
            return True
        time.sleep(0.15)
    return False


def controls(h: int) -> list[Ctrl]:
    out: list[int] = []
    win32gui.EnumChildWindows(h, lambda c, _: out.append(c) or True, None)
    res = []
    for c in out:
        try:
            res.append(_info(c))
        except Exception:
            continue
    return res


def find(h: int, cid: int | None = None, text: str | None = None, cls: str | None = None,
         visible: bool = True) -> Ctrl:
    for c in controls(h):
        if visible and not c.visible:
            continue
        if cid is not None and c.cid != cid:
            continue
        if cls is not None and c.cls != cls:
            continue
        if text is not None and c.text.replace("&", "").lower() != text.replace("&", "").lower():
            continue
        return c
    raise InventorError(f"control not found in dialog {win32gui.GetWindowText(h)!r}: id={cid} text={text!r} cls={cls}")


def find_all(h: int, cls: str | None = None, visible: bool = True) -> list[Ctrl]:
    return [c for c in controls(h) if (not visible or c.visible) and (cls is None or c.cls == cls)]


def click(c: Ctrl) -> None:
    """Press a button (PostMessage so a modal step it triggers doesn't block us)."""
    win32gui.PostMessage(c.hwnd, win32con.BM_CLICK, 0, 0)
    time.sleep(0.25)


def set_text(c: Ctrl, value: str) -> None:
    """Type into an edit box and tell the dialog it changed (EN_CHANGE), like a keyboard edit."""
    win32gui.SendMessage(c.hwnd, win32con.WM_SETTEXT, 0, str(value))
    parent = win32gui.GetParent(c.hwnd)
    win32gui.SendMessage(parent, win32con.WM_COMMAND, (win32con.EN_CHANGE << 16) | (c.cid & 0xFFFF), c.hwnd)
    # some Inventor value boxes evaluate on focus loss
    win32gui.SendMessage(parent, win32con.WM_COMMAND, (win32con.EN_KILLFOCUS << 16) | (c.cid & 0xFFFF), c.hwnd)
    time.sleep(0.1)


def get_text(c: Ctrl) -> str:
    """The text a control shows. GetWindowText can't read an edit box in another process; WM_GETTEXT can."""
    n = win32gui.SendMessage(c.hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
    buf = win32gui.PyMakeBuffer((n + 1) * 2)
    win32gui.SendMessage(c.hwnd, win32con.WM_GETTEXT, n + 1, buf)
    return buf.tobytes().decode("utf-16-le", "ignore").split("\x00")[0]


def is_checked(c: Ctrl) -> bool:
    return win32gui.SendMessage(c.hwnd, win32con.BM_GETCHECK, 0, 0) == win32con.BST_CHECKED


def set_check(c: Ctrl, on: bool = True) -> None:
    if is_checked(c) != on:
        win32gui.SendMessage(c.hwnd, win32con.BM_CLICK, 0, 0)
        time.sleep(0.15)


def combo_items(c: Ctrl) -> list[str]:
    n = win32gui.SendMessage(c.hwnd, win32con.CB_GETCOUNT, 0, 0)
    out = []
    for i in range(max(n, 0)):
        ln = win32gui.SendMessage(c.hwnd, win32con.CB_GETLBTEXTLEN, i, 0)
        buf = win32gui.PyMakeBuffer((ln + 1) * 2)
        win32gui.SendMessage(c.hwnd, win32con.CB_GETLBTEXT, i, buf)
        out.append(buf.tobytes().decode("utf-16-le", "ignore").rstrip("\x00"))
    return out


def combo_select(c: Ctrl, text: str) -> str:
    items_ = combo_items(c)
    pick = next((i for i, t in enumerate(items_) if t.lower() == text.lower()), None)
    if pick is None:
        pick = next((i for i, t in enumerate(items_) if text.lower() in t.lower()), None)
    if pick is None:
        raise InventorError(f"{text!r} not in list {items_}")
    win32gui.SendMessage(c.hwnd, win32con.CB_SETCURSEL, pick, 0)
    parent = win32gui.GetParent(c.hwnd)
    win32gui.SendMessage(parent, win32con.WM_COMMAND, (win32con.CBN_SELCHANGE << 16) | (c.cid & 0xFFFF), c.hwnd)
    time.sleep(0.15)
    return items_[pick]


def dump(h: int) -> str:
    lines = [f"{win32gui.GetWindowText(h)!r}"]
    for c in controls(h):
        if c.visible:
            lines.append(f"  [{c.cid}] {c.cls} {c.text!r}{'' if c.enabled else ' (disabled)'}")
    return "\n".join(lines)


def press_enter(h: int) -> None:
    win32api.PostMessage(h, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
    win32api.PostMessage(h, win32con.WM_KEYUP, win32con.VK_RETURN, 0)


def close(h: int) -> None:
    """Cancel a dialog with WM_CLOSE (some Stress Analysis dialogs ignore a click on Cancel)."""
    win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
    time.sleep(0.4)


def window_rows(pid: int) -> list[tuple[int, str, str]]:
    """Every visible top-level window of a process: (hwnd, class, title). WPF prompts show up here too,
    as HwndWrapper[...] windows, although they have no child controls to read."""
    rows = []
    for h in process_windows(pid):
        try:
            rows.append((h, win32gui.GetClassName(h), win32gui.GetWindowText(h)))
        except Exception:
            continue
    return rows


def listbox_items(h: int) -> list[str]:
    """The lines of every list box in a dialog (the Simulate dialog lists solver warnings in one)."""
    LB_GETCOUNT, LB_GETTEXTLEN, LB_GETTEXT = 0x018B, 0x018A, 0x0189
    out = []
    for c in controls(h):
        if c.cls != "ListBox":
            continue
        for i in range(max(win32gui.SendMessage(c.hwnd, LB_GETCOUNT, 0, 0), 0)):
            n = win32gui.SendMessage(c.hwnd, LB_GETTEXTLEN, i, 0)
            buf = win32gui.PyMakeBuffer((n + 1) * 2)
            win32gui.SendMessage(c.hwnd, LB_GETTEXT, i, buf)
            out.append(buf.tobytes().decode("utf-16-le", "ignore").split("\x00")[0])
    return out


def real_focus(hwnd: int) -> None:
    """Give a control in another process real keyboard focus (no key is pressed)."""
    target = win32process.GetWindowThreadProcessId(hwnd)[0]
    me = win32api.GetCurrentThreadId()
    ctypes.windll.user32.AttachThreadInput(me, target, True)
    try:
        ctypes.windll.user32.SetFocus(hwnd)
    finally:
        ctypes.windll.user32.AttachThreadInput(me, target, False)
    time.sleep(0.1)


def commit_text(c: Ctrl, value: str, other: Ctrl) -> str:
    """Enter a value so Inventor stores it, without typing. Inventor's value boxes only parse on a genuine
    loss of focus: focus the box, set the text, move real focus to another enabled control. Returns the
    text the box then shows."""
    real_focus(c.hwnd)
    win32gui.SendMessage(c.hwnd, win32con.WM_SETTEXT, 0, str(value))
    time.sleep(0.1)
    real_focus(other.hwnd)
    time.sleep(0.25)
    return get_text(c)


def _leading_number(text: str) -> float | None:
    m = re.match(r"\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)", str(text).replace(",", ""))
    return float(m.group(1)) if m else None


def _unit(text: str) -> str:
    m = re.match(r"\s*-?[\d.,eE+-]+\s*(.*)$", str(text))
    return (m.group(1) if m else "").strip().lower()


def same_value(shown: str, wanted: str) -> bool:
    """Does what a value box shows match what was entered? Inventor reformats numbers (2 -> 2.000 MPa)."""
    a, b = _leading_number(shown), _leading_number(wanted)
    if a is not None and b is not None:
        ua, ub = _unit(shown), _unit(wanted)
        if ua and ub and ua.replace(" ", "") != ub.replace(" ", ""):      # 2 psi is not 2 MPa
            return False
        return abs(a - b) <= 1e-3 * max(abs(b), 1e-6) + 1e-9
    return str(shown).strip().lower() == str(wanted).strip().lower()


# --------------------------------------------------------------------------- processes (no COM)
def _image_name(pid: int) -> str:
    try:
        hp = win32api.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    except Exception:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_ulong(512)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(int(hp.handle), 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value)
    finally:
        win32api.CloseHandle(hp)


def pids_named(exe: str) -> list[int]:
    return [p for p in win32process.EnumProcesses() if _image_name(p).lower() == exe.lower()]


def solver_windows(pids: list[int] | None = None) -> list[str]:
    """Titles of Stress Analysis solve/report windows that are open. Inventor must not get COM calls while
    one is running: polling it during a solve has crashed it. Uses Windows calls only."""
    out = []
    for pid in (pids if pids is not None else pids_named("Inventor.exe")):
        for _, cls, title in window_rows(pid):
            if (cls == "#32770" and title == "Simulate") or "Stress Analysis Report" in title:
                out.append(title)
    return out


def process_stats(pid: int) -> dict:
    """Memory and CPU time of a process, without touching its COM interface."""
    hp = win32api.OpenProcess(0x1000, False, pid)
    try:
        mem = win32process.GetProcessMemoryInfo(hp)
        times = win32process.GetProcessTimes(hp)
        return {"memory_gb": round(mem["PagefileUsage"] / 2 ** 30, 2),
                "cpu_seconds": round((times["KernelTime"] + times["UserTime"]) / 1e7, 1)}
    finally:
        win32api.CloseHandle(hp)


# --------------------------------------------------------------------------- tab strips and tree views in Inventor
# These messages take a pointer to a structure, which has to live in the memory of the process that owns the
# control, so the structure is written into memory allocated there.
class _ForeignMemory:
    def __init__(self, hwnd: int, size: int = 4096):
        from ctypes import wintypes as wt
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wt.HANDLE
        k.VirtualAllocEx.restype = ctypes.c_void_p
        k.VirtualAllocEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD)
        k.VirtualFreeEx.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD)
        k.WriteProcessMemory.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)
        k.ReadProcessMemory.argtypes = (wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)
        k.CloseHandle.argtypes = (wt.HANDLE,)
        self.k = k
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        self.proc = k.OpenProcess(0x0008 | 0x0010 | 0x0020, False, pid)
        if not self.proc:
            raise InventorError("could not open Inventor's process to read a control")
        self.addr = k.VirtualAllocEx(self.proc, None, size, 0x3000, 0x04)
        if not self.addr:
            k.CloseHandle(self.proc)
            raise InventorError("could not allocate memory in Inventor's process")

    def write(self, offset: int, data: bytes) -> None:
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        self.k.WriteProcessMemory(self.proc, self.addr + offset, buf, len(data), None)

    def read(self, offset: int, n: int) -> bytes:
        buf = ctypes.create_string_buffer(n)
        self.k.ReadProcessMemory(self.proc, self.addr + offset, buf, n, None)
        return buf.raw

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.k.VirtualFreeEx(self.proc, self.addr, 0, 0x8000)
        self.k.CloseHandle(self.proc)


def _send(h: int, msg: int, wp: int, lp: int) -> int:
    f = ctypes.windll.user32.SendMessageW
    f.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
    f.restype = ctypes.c_ssize_t
    return f(h, msg, wp, lp)


def tab_labels(tab: int) -> list[str]:
    """Captions of a tab strip (64-bit TCITEMW)."""
    out = []
    n = _send(tab, 0x1304, 0, 0)                     # TCM_GETITEMCOUNT
    with _ForeignMemory(tab) as m:
        for i in range(n):
            text = m.addr + 512
            item = (1).to_bytes(4, "little") + bytes(12) + text.to_bytes(8, "little") + (200).to_bytes(4, "little") + bytes(12)
            m.write(0, item)
            _send(tab, 0x133C, i, m.addr)            # TCM_GETITEMW
            out.append(m.read(512, 400).decode("utf-16-le", "ignore").split("\x00")[0])
    return out


def tab_select(tab: int, index: int) -> bool:
    """Switch page the way a click does (TCM_SETCURFOCUS sends the change notifications)."""
    _send(tab, 0x1330, index, 0)
    time.sleep(0.5)
    return _send(tab, 0x130B, 0, 0) == index        # TCM_GETCURSEL


def tree_items(tree: int) -> list[dict]:
    """Every row of a tree view: item handle, label, depth, tick state (None without checkboxes)."""
    out: list[dict] = []
    with _ForeignMemory(tree) as m:
        def read(h):
            text = m.addr + 512
            # TVITEMW (64-bit): mask, hItem@8, state@16, stateMask@20, pszText@24, cchTextMax@32
            item = (0x1 | 0x8 | 0x10).to_bytes(4, "little") + bytes(4) + int(h).to_bytes(8, "little") + bytes(4)
            item += (0xF000).to_bytes(4, "little") + text.to_bytes(8, "little") + (200).to_bytes(4, "little") + bytes(20)
            m.write(0, item)
            _send(tree, 0x113E, 0, m.addr)           # TVM_GETITEMW
            state = int.from_bytes(m.read(16, 4), "little")
            icon = (state & 0xF000) >> 12
            return m.read(512, 400).decode("utf-16-le", "ignore").split("\x00")[0], (None if icon == 0 else icon == 2)

        def walk(h, depth):
            while h:
                label, ticked = read(h)
                out.append({"item": h, "label": label, "depth": depth, "checked": ticked})
                walk(_send(tree, 0x110A, 4, h), depth + 1)     # TVGN_CHILD
                h = _send(tree, 0x110A, 1, h)                  # TVGN_NEXT
        walk(_send(tree, 0x110A, 0, 0), 0)                     # TVGN_ROOT
    return out


def tree_item_rect(tree: int, item: int) -> tuple[int, int, int, int]:
    """Screen rectangle of a row's text (scrolled into view first)."""
    _send(tree, 0x1114, 0, item)                      # TVM_ENSUREVISIBLE
    time.sleep(0.1)
    with _ForeignMemory(tree) as m:
        m.write(0, int(item).to_bytes(8, "little") + bytes(8))
        _send(tree, 0x1104, 1, m.addr)               # TVM_GETITEMRECT, text part only
        left, top, right, bottom = (int.from_bytes(m.read(i * 4, 4), "little", signed=True) for i in range(4))
    x0, y0 = win32gui.ClientToScreen(tree, (left, top))
    x1, y1 = win32gui.ClientToScreen(tree, (right, bottom))
    return x0, y0, x1, y1


def child_processes(parent: int) -> list[tuple[int, str]]:
    """(pid, exe name) of processes started by `parent` (Toolhelp snapshot; works after the parent has exited)."""
    from ctypes import wintypes as wt

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    out = []
    try:
        e = Entry()
        e.dwSize = ctypes.sizeof(Entry)
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.th32ParentProcessID == parent:
                out.append((e.th32ProcessID, e.szExeFile))
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def end_orphaned_solvers(dead_inventor: int) -> int:
    """After this session's Inventor died, end the solver processes it started: left running, they keep the model's
    analysis files locked and the next Inventor hangs opening the model."""
    if dead_inventor in pids_named("Inventor.exe"):
        return 0
    return end_solvers([pid for pid, exe in child_processes(dead_inventor) if exe.lower().startswith("feacomputeserver")])


def orphaned_solvers() -> list[int]:
    """Stress Analysis solver processes whose Inventor has gone (they keep analysis files locked)."""
    alive = set(pids_named("Inventor.exe"))
    solvers = set(pids_named("FEAComputeServer.exe"))
    parents = {}
    # map every process to its parent with one snapshot
    import ctypes as _c

    class Entry(_c.Structure):
        _fields_ = [("dwSize", _c.c_ulong), ("cntUsage", _c.c_ulong), ("th32ProcessID", _c.c_ulong),
                    ("th32DefaultHeapID", _c.c_void_p), ("th32ModuleID", _c.c_ulong), ("cntThreads", _c.c_ulong),
                    ("th32ParentProcessID", _c.c_ulong), ("pcPriClassBase", _c.c_long), ("dwFlags", _c.c_ulong),
                    ("szExeFile", _c.c_wchar * 260)]
    k = _c.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = _c.c_void_p
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    try:
        e = Entry()
        e.dwSize = _c.sizeof(Entry)
        ok = k.Process32FirstW(_c.c_void_p(snap), _c.byref(e))
        while ok:
            parents[e.th32ProcessID] = e.th32ParentProcessID
            ok = k.Process32NextW(_c.c_void_p(snap), _c.byref(e))
    finally:
        k.CloseHandle(_c.c_void_p(snap))
    return [p for p in solvers if parents.get(p) not in alive]


def end_solvers(pids: list[int]) -> int:
    n = 0
    for pid in pids:
        try:
            hp = win32api.OpenProcess(0x0001, False, pid)
            win32process.TerminateProcess(hp, 1)
            win32api.CloseHandle(hp)
            n += 1
        except Exception:
            pass
    return n
