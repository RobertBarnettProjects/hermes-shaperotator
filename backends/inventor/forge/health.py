"""Keeping a long-running session healthy: hang detection, the start lock, the COM message filter, keeping the PC
awake during long calls, and the checks made when a session connects. Windows calls only (no COM), so everything
here is safe while Inventor is busy or solving."""
from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import tempfile
import threading
import time
from typing import Callable

from .com import InventorError


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------- hang detection
HANG_WARN = 120.0          # s stuck before a warning (INVENTOR_FORGE_HANG_WARN)
HANG_STOP = 600.0          # s stuck before the call is given up on (INVENTOR_FORGE_HANG_STOP; 0 = never)
IDLE_CPU = 0.02            # below this many CPU-seconds per second across the process tree counts as idle


def hang_limits() -> tuple[float, float]:
    return _env_float("INVENTOR_FORGE_HANG_WARN", HANG_WARN), _env_float("INVENTOR_FORGE_HANG_STOP", HANG_STOP)


def window_hung(hwnd: int) -> bool:
    """Windows' own "not responding" test for a top-level window (no message handled for about 5 s)."""
    try:
        return bool(ctypes.windll.user32.IsHungAppWindow(int(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def tree_cpu_seconds(pid: int) -> float | None:
    """CPU time used by a process and everything it started (solver children included). None if it has gone."""
    from . import uidrive as U
    try:
        total = U.process_stats(pid)["cpu_seconds"]
    except Exception:  # noqa: BLE001
        return None
    seen, todo = {pid}, [pid]
    while todo:
        parent = todo.pop()
        for child, _exe in U.child_processes(parent):
            if child in seen:
                continue
            seen.add(child)
            todo.append(child)
            total += _cpu(child)
    return total


def _cpu(pid: int) -> float:
    from . import uidrive as U
    try:
        return U.process_stats(pid)["cpu_seconds"]
    except Exception:  # noqa: BLE001
        return 0.0


class HangWatch:
    """Tells a busy Inventor from a stuck one while a call is waited on.

    Stuck means either Windows reports the main window as not responding, or Inventor and every process it started
    (the solver) together use almost no CPU: a responsive Inventor sitting on a modal prompt does nothing at all,
    while one that is computing uses CPU. ``sample`` is called every few seconds; it returns "ok", "warn" (once, when
    stuck for warn_s) or "stop" (stuck for stop_s)."""

    def __init__(self, pid: int, hwnd: int | None = None, warn_s: float | None = None, stop_s: float | None = None,
                 interval: float = 5.0, hung: Callable[[], bool] | None = None,
                 cpu: Callable[[], float | None] | None = None, clock: Callable[[], float] = time.time):
        w, st = hang_limits()
        self.warn_s = w if warn_s is None else warn_s
        self.stop_s = st if stop_s is None else stop_s
        self.interval = interval
        self._hung = hung or (lambda: window_hung(hwnd) if hwnd else False)
        self._cpu = cpu or (lambda: tree_cpu_seconds(pid))
        self._clock = clock
        self._last_t = clock()
        self._last_cpu = self._cpu()
        self.stuck_since: float | None = None
        self.reason = ""
        self.warned = False

    def sample(self) -> str:
        now = self._clock()
        if now - self._last_t < self.interval:
            return self.state(now)
        cpu = self._cpu()
        rate = None
        if cpu is not None and self._last_cpu is not None:
            rate = (cpu - self._last_cpu) / max(now - self._last_t, 1e-6)
        self._last_t, self._last_cpu = now, cpu
        hung = self._hung()
        idle = rate is not None and rate < IDLE_CPU
        if hung or idle:
            if self.stuck_since is None:
                self.stuck_since = now - self.interval        # it was already quiet over the last interval
            self.reason = ("Inventor's window is not responding" if hung else
                           "Inventor and its solver processes have used no CPU (it may be waiting on a prompt)")
        else:
            self.stuck_since, self.reason, self.warned = None, "", False
        return self.state(now)

    def stuck_for(self, now: float | None = None) -> float:
        return 0.0 if self.stuck_since is None else (now or self._clock()) - self.stuck_since

    def state(self, now: float | None = None) -> str:
        t = self.stuck_for(now)
        if self.stop_s and t >= self.stop_s:
            return "stop"
        if self.warn_s and t >= self.warn_s and not self.warned:
            self.warned = True
            return "warn"
        return "ok"


# --------------------------------------------------------------------------- start lock
LOCK_NAME = "inventor_forge_start.lock"
START_STAGGER = 45.0       # s: wait until any other Inventor is at least this old before starting one


def lock_path() -> str:
    return os.path.join(tempfile.gettempdir(), LOCK_NAME)


@contextlib.contextmanager
def start_lock(timeout: float = 300.0, path: str = ""):
    """One Inventor start at a time across every process on the PC: a byte lock on a file in the temp folder, held
    from launch until the new Inventor's process is identified. Two starts at once otherwise each see two new
    processes and can't tell which is theirs."""
    import msvcrt
    path = path or lock_path()
    f = open(path, "a+b")
    t0 = time.time()
    try:
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() - t0 > timeout:
                    raise InventorError(f"another program has been starting an Inventor for over {timeout:.0f} s "
                                        f"(lock file {path}); try again once it has finished") from None
                time.sleep(0.5)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        f.close()


def process_age(pid: int) -> float | None:
    """Seconds since a process started. None if Windows won't say."""
    import win32api
    import win32process
    try:
        hp = win32api.OpenProcess(0x1000, False, pid)
    except Exception:  # noqa: BLE001
        return None
    try:
        created = win32process.GetProcessTimes(hp)["CreationTime"]
        return max(0.0, time.time() - created.timestamp())
    except Exception:  # noqa: BLE001
        return None
    finally:
        win32api.CloseHandle(hp)


def wait_for_stagger(pids: list[int], gap: float | None = None, limit: float = 120.0) -> float:
    """Wait until every running Inventor is at least `gap` seconds old: starting two at nearly the same moment
    overloads the licence check and the disk. Returns how long it waited."""
    gap = _env_float("INVENTOR_FORGE_START_STAGGER", START_STAGGER) if gap is None else gap
    t0 = time.time()
    while time.time() - t0 < limit:
        ages = [a for a in (process_age(p) for p in pids) if a is not None]
        young = [a for a in ages if a < gap]
        if not young:
            break
        time.sleep(min(5.0, gap - max(young) + 0.5))
    return time.time() - t0


# --------------------------------------------------------------------------- COM message filter
SERVERCALL_ISHANDLED, SERVERCALL_REJECTED, SERVERCALL_RETRYLATER = 0, 1, 2
PENDINGMSG_WAITDEFPROCESS = 2
BUSY_RETRY_MS = 250
BUSY_LIMIT = 60.0          # s a refused call is retried for (INVENTOR_FORGE_BUSY_LIMIT)


def retry_delay(tick_ms: int, reject_type: int, limit_s: float | None = None) -> int:
    """IMessageFilter.RetryRejectedCall: milliseconds to wait before COM sends the call again, or -1 to give up.
    Inventor answers "busy" while it is computing or showing a dialog; the call was not run, so sending it again is
    safe."""
    limit_s = _env_float("INVENTOR_FORGE_BUSY_LIMIT", BUSY_LIMIT) if limit_s is None else limit_s
    if reject_type not in (SERVERCALL_REJECTED, SERVERCALL_RETRYLATER):
        return -1
    if tick_ms >= limit_s * 1000:
        return -1
    return BUSY_RETRY_MS


class _MessageFilter:
    """A minimal IMessageFilter written with ctypes (pywin32 doesn't expose CoRegisterMessageFilter)."""

    IID_IUNKNOWN = "{00000000-0000-0000-C000-000000000046}"
    IID_IMESSAGEFILTER = "{00000016-0000-0000-C000-000000000046}"

    def __init__(self):
        from ctypes import wintypes as wt
        HR = ctypes.c_long
        self._refs = 1
        QI = ctypes.WINFUNCTYPE(HR, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        REF = ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)
        INCOMING = ctypes.WINFUNCTYPE(wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p)
        RETRY = ctypes.WINFUNCTYPE(wt.DWORD, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD)
        PENDING = ctypes.WINFUNCTYPE(wt.DWORD, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD)
        guid = ctypes.c_byte * 16
        ole32 = ctypes.windll.ole32
        self._iids = []
        for text in (self.IID_IUNKNOWN, self.IID_IMESSAGEFILTER):
            g = guid()
            ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
            self._iids.append(bytes(g))

        def qi(this, riid, ppv):
            want = ctypes.string_at(riid, 16)
            if want in self._iids:
                ppv[0] = this
                self._refs += 1
                return 0
            ppv[0] = None
            return -2147467262            # E_NOINTERFACE

        def addref(this):
            self._refs += 1
            return self._refs

        def release(this):
            self._refs = max(1, self._refs - 1)
            return self._refs

        def incoming(this, call_type, caller, tick, info):
            return SERVERCALL_ISHANDLED

        def retry(this, callee, tick, reject_type):
            return retry_delay(int(tick), int(reject_type)) & 0xFFFFFFFF

        def pending(this, callee, tick, pending_type):
            return PENDINGMSG_WAITDEFPROCESS

        self._fns = (QI(qi), REF(addref), REF(release), INCOMING(incoming), RETRY(retry), PENDING(pending))

        class VTable(ctypes.Structure):
            _fields_ = [(n, type(f)) for n, f in zip(("qi", "addref", "release", "incoming", "retry", "pending"),
                                                       self._fns)]

        class Obj(ctypes.Structure):
            _fields_ = [("vtbl", ctypes.POINTER(VTable))]

        self._vtbl = VTable(*self._fns)
        self._obj = Obj(ctypes.pointer(self._vtbl))
        self.ptr = ctypes.cast(ctypes.pointer(self._obj), ctypes.c_void_p)


_FILTERS: dict[int, _MessageFilter] = {}     # thread id -> filter, kept alive for the thread's life


def register_message_filter() -> bool:
    """Install the busy-retry message filter on the calling (COM apartment) thread. True when installed."""
    tid = threading.get_ident()
    if tid in _FILTERS:
        return True
    try:
        mf = _MessageFilter()
        old = ctypes.c_void_p()
        hr = ctypes.windll.ole32.CoRegisterMessageFilter(mf.ptr, ctypes.byref(old))
    except Exception:  # noqa: BLE001
        return False
    if hr != 0:
        return False
    _FILTERS[tid] = mf
    return True


# --------------------------------------------------------------------------- keeping the PC awake
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


@contextlib.contextmanager
def keep_awake(on: bool = True):
    """Stop the PC going to sleep while a long call runs (a solve or a batch of them): Windows' idle timer counts
    only keyboard and mouse input, not work being done. Held by the calling thread for the duration."""
    if not on:
        yield
        return
    k = ctypes.windll.kernel32
    with contextlib.suppress(Exception):
        k.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            k.SetThreadExecutionState(ES_CONTINUOUS)        # this thread no longer needs the PC awake


# --------------------------------------------------------------------------- repeated failures
class FailureMemory:
    """Remembers the last failed call. The same call failing the same way twice in a row is stopped there: repeating
    it won't help, and runs of identical failing calls have come before Inventor crashes."""

    def __init__(self, limit: int = 2):
        self.limit = limit
        self.key: tuple | None = None
        self.count = 0

    @staticmethod
    def signature(name: str, args: dict) -> tuple:
        import json
        try:
            text = json.dumps(args, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            text = repr(sorted(args.items()))
        return name, text

    def blocked(self, name: str, args: dict) -> bool:
        return self.count >= self.limit and self.key is not None and self.key[:2] == self.signature(name, args)

    def failed(self, name: str, args: dict, error: str) -> int:
        """Record a failure; returns how many times in a row this call has now failed the same way."""
        key = self.signature(name, args) + (error,)
        self.count = self.count + 1 if key == self.key else 1
        self.key = key
        return self.count

    def succeeded(self) -> None:
        self.key, self.count = None, 0


# --------------------------------------------------------------------------- start-up checks
LOW_DISK_GB = 5.0          # warn below this (INVENTOR_FORGE_LOW_DISK_GB)
MIN_DISK_GB = 1.0          # refuse below this


def disk_free_gb(path: str) -> float | None:
    try:
        probe = path
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        return shutil.disk_usage(probe or ".").free / 2 ** 30
    except OSError:
        return None


def state_issues(project: str, expected_project: str, dirty: list[str], private: bool, folders: list[str],
                 low_gb: float | None = None, min_gb: float = MIN_DISK_GB) -> tuple[list[str], list[str]]:
    """(problems that should stop work, warnings) for the Inventor a session has just connected to."""
    low_gb = _env_float("INVENTOR_FORGE_LOW_DISK_GB", LOW_DISK_GB) if low_gb is None else low_gb
    stop, warn = [], []
    if expected_project:
        want = os.path.normcase(os.path.splitext(os.path.basename(expected_project))[0])
        have = os.path.normcase(os.path.splitext(os.path.basename(project or ""))[0])
        full_match = os.path.normcase(os.path.abspath(expected_project)) == os.path.normcase(os.path.abspath(project)) \
            if project and os.path.isabs(expected_project) else want == have
        if not full_match:
            stop.append(f"the active project is {project or '(none)'}, not {expected_project}; set_active_project "
                        "first (with no documents open)")
    if dirty:
        msg = (f"{len(dirty)} document(s) already have unsaved changes: {', '.join(dirty[:6])}"
               + (" ..." if len(dirty) > 6 else ""))
        (stop if private else warn).append(
            msg + ("; a new private Inventor should have none, so something else is using it" if private else
                   "; these may be the user's work in progress, so don't save or close them without asking"))
    for folder in dict.fromkeys(f for f in folders if f):
        free = disk_free_gb(folder)
        if free is None:
            continue
        if free < min_gb:
            stop.append(f"only {free:.1f} GB free for {folder}; saves and Stress Analysis results need room")
        elif free < low_gb:
            warn.append(f"{free:.1f} GB free for {folder}; each solved study stores about 0.1-0.5 GB of mesh and "
                        "results beside the model")
    return stop, warn
