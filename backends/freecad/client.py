"""Talk to the FreeCAD worker process (FreeCAD's own Python, headless, JSON lines over stdio)."""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
from typing import Any

try:
    from ...core.childenv import child_env as _child_env
except ImportError:                     # loaded on its own (tests, the worker's folder)
    from core.childenv import child_env as _child_env

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")


class WorkerError(RuntimeError):
    pass


def _candidates() -> list[str]:
    out: list[str] = []
    if sys.platform == "win32":
        roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", "")]
        for r in filter(None, roots):
            out += sorted(glob.glob(os.path.join(r, "Programs", "FreeCAD *")), reverse=True)
            out += sorted(glob.glob(os.path.join(r, "FreeCAD *")), reverse=True)
    elif sys.platform == "darwin":
        out += ["/Applications/FreeCAD.app/Contents/Resources"]
    else:
        out += ["/usr/lib/freecad", "/usr/lib/freecad-python3", "/usr/local/lib/freecad"]
    return out


def find_freecad_python(configured: str) -> str | None:
    """Return a Python executable that can `import FreeCAD`, or None.

    `configured` may be the FreeCAD install folder, its bin folder, or the python executable.
    """
    exe = "python.exe" if sys.platform == "win32" else "python"
    paths = [configured] if configured else _candidates()
    for p in paths:
        if not p:
            continue
        if os.path.isfile(p):
            return p
        for sub in ("bin", ""):
            cand = os.path.join(p, sub, exe)
            if os.path.isfile(cand) and (
                os.path.isfile(os.path.join(p, sub, "FreeCAD.pyd")) or glob.glob(os.path.join(p, sub, "FreeCAD*"))
                or glob.glob(os.path.join(p, "lib", "FreeCAD*"))
            ):
                return cand
    if not configured:
        cmd = shutil.which("freecadcmd") or shutil.which("FreeCADCmd")
        if cmd:  # AppImage / distro builds: sibling python is usually the right one
            sib = os.path.join(os.path.dirname(cmd), exe)
            if os.path.isfile(sib):
                return sib
    return None


class FreeCADClient:
    """One long-lived worker. Requests are serialised; the worker is restarted after a crash."""

    def __init__(self, python: str, timeout: float = 300.0):
        self.python = python
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()
        self.stderr_tail: list[str] = []

    # -- process ---------------------------------------------------------
    def _start(self) -> None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            [self.python, "-I", "-u", WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, creationflags=flags,
            cwd=os.path.dirname(self.python), env=_child_env(prefixes=("FREECAD", "HERMES_SHAPEROTATOR_DEBUG")),
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        hello = self._read_reply(0)
        if not hello.get("ok"):
            raise WorkerError("FreeCAD worker failed to start")

    def _drain_stderr(self) -> None:
        proc = self._proc
        for line in proc.stderr:  # type: ignore[union-attr]
            self.stderr_tail.append(line.rstrip())
            del self.stderr_tail[:-50]

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        if self.alive():
            try:
                self._proc.stdin.write(json.dumps({"op": "__exit__"}) + "\n")  # type: ignore[union-attr]
                self._proc.stdin.flush()  # type: ignore[union-attr]
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()  # type: ignore[union-attr]
        self._proc = None

    # -- protocol --------------------------------------------------------
    def _read_reply(self, rid: int) -> dict:
        result: dict = {}

        def reader():
            for line in self._proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == rid:
                    result.update(msg)
                    return

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(self.timeout)
        if t.is_alive():
            self._proc.kill()  # type: ignore[union-attr]
            self._proc = None
            raise WorkerError(f"FreeCAD did not answer within {self.timeout:.0f} s — worker restarted")
        if not result:
            tail = "\n".join(self.stderr_tail[-5:])
            self._proc = None
            raise WorkerError(f"FreeCAD worker exited unexpectedly. Last output:\n{tail}")
        return result

    def request(self, op: str, **args: Any) -> dict:
        with self._lock:
            if not self.alive():
                self._start()
            self._id += 1
            rid = self._id
            self._proc.stdin.write(json.dumps({"id": rid, "op": op, "args": args}) + "\n")  # type: ignore[union-attr]
            self._proc.stdin.flush()  # type: ignore[union-attr]
            reply = self._read_reply(rid)
        if not reply.get("ok"):
            msg = reply.get("error", "unknown error")
            if reply.get("trace") and os.environ.get("HERMES_SHAPEROTATOR_DEBUG"):
                msg += "\n" + reply["trace"]
            raise WorkerError(msg)
        return reply.get("result", {})
