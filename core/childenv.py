"""The minimal environment child processes get (no imports from the plugin, so any module can use it)."""
from __future__ import annotations

import os
import sys

# --------------------------------------------------------------------------- child process environment
_BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP")
_WINDOWS_ENV = ("USERPROFILE", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA",
                "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
                "COMMONPROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME",
                "USERDOMAIN", "COMPUTERNAME", "PUBLIC", "HOMEDRIVE", "HOMEPATH")


def child_env(extra: dict[str, str] | None = None, prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """A minimal environment for a child process: what the OS needs, variables starting with one of `prefixes`
    (the tool's own settings), and `extra`. Provider keys and anything else in Hermes's environment are left out."""
    names = _BASE_ENV + (_WINDOWS_ENV if sys.platform == "win32" else ())
    upper = {k.upper(): k for k in os.environ}
    env = {upper[n]: os.environ[upper[n]] for n in names if n in upper}
    for k, v in os.environ.items():
        if prefixes and k.upper().startswith(tuple(p.upper() for p in prefixes)):
            env[k] = v
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    return env
