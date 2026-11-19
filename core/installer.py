"""Which CAD programs are on this computer, and the add-on MCP servers that drive them.

The known backends are listed in ``backends/known.json``. A built-in backend (FreeCAD) ships with the plugin. Any
other is a separate MCP server, downloaded only when the user approves it (``cad_backend(action="install")`` asks
first, through Hermes's approval prompt): pinned to one commit of its repository, installed with pip into its own
virtual environment under the plugin's data folder, so nothing in Hermes's own Python changes. A plugin release
moves the pin; the next use then offers the update the same way.

For development, ``server_sources`` in the plugin settings maps a backend to a local checkout or a pip requirement
to install instead of the pinned commit.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .childenv import child_env
from .state import STATE, _data_dir

KNOWN_FILE = Path(__file__).resolve().parent.parent / "backends" / "known.json"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()
INSTALL_TIMEOUT_S = 900
# a proxy and its certificates still apply; pip's own settings (other indexes, config files) don't: pip runs isolated
NETWORK_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")
_APPROVED: dict[str, str] = {}          # backend -> the requirement the user was shown and approved


class InstallError(RuntimeError):
    pass


def known() -> dict[str, dict]:
    data = json.loads(KNOWN_FILE.read_text(encoding="utf-8"))
    return {k.lower(): v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def entry(name: str) -> dict:
    k = known()
    if name.lower() not in k:
        raise InstallError(f"unknown CAD backend {name!r} (known: {', '.join(k)})")
    return k[name.lower()]


# --------------------------------------------------------------------------- detection
def _registry_installs(spec: dict) -> list[str]:
    """Install folders under HKLM\\<key>\\<subkeys>*\\<value> (Windows), newest first."""
    if sys.platform != "win32":
        return []
    import winreg
    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, spec["key"])
    except OSError:
        return []
    try:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            if not sub.startswith(spec.get("subkeys", "")):
                continue
            try:
                with winreg.OpenKey(root, sub) as k:
                    loc, _ = winreg.QueryValueEx(k, spec.get("value", "Location"))
                if loc and os.path.isdir(loc):
                    out.append(str(loc))
            except OSError:
                continue
    finally:
        winreg.CloseKey(root)
    return sorted(set(out), reverse=True)


def _com_class(name: str) -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, name))
        return True
    except OSError:
        return False


def detect(name: str, spec: dict | None = None) -> dict:
    """{"found": bool, "version": "Inventor 2027" or "", "detail": ...} for one known backend's CAD program."""
    spec = spec or entry(name)
    plats = spec.get("platforms")
    if plats and sys.platform not in plats:
        return {"found": False, "detail": f"{spec.get('title', name)} runs on {', '.join(plats)} only"}
    d = spec.get("detect") or {}
    if d.get("builtin"):
        try:
            ok, why = STATE.backend(d["builtin"]).available()
        except Exception as e:  # noqa: BLE001
            ok, why = False, str(e)
        found = {"found": ok, "detail": why}
        if ok:
            import re
            m = re.search(r"FreeCAD[ _-]?\d+(\.\d+)*", why.split(" at ", 1)[-1], re.I)
            found["version"] = m.group(0) if m else spec.get("title", name)
        return found
    folders = _registry_installs(d["registry"]) if d.get("registry") else []
    com = _com_class(d["com_class"]) if d.get("com_class") else bool(folders)
    if not (folders or com):
        return {"found": False, "detail": f"{spec.get('title', name)} is not installed"}
    version = Path(folders[0]).name if folders else spec.get("title", name)
    return {"found": True, "version": version, "detail": folders[0] if folders else "registered"}


def detect_all() -> dict[str, dict]:
    return {name: {**detect(name, spec), "title": spec.get("title", name), "builtin": bool(spec.get("builtin")),
                   **({"server": server_status(name)} if spec.get("server") else {})}
            for name, spec in known().items()}


# --------------------------------------------------------------------------- installed servers
def server_dir(name: str) -> Path:
    return _data_dir() / "servers" / name.lower()


def _py_in(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _venv_python(name: str) -> Path:
    """The interpreter of the backend's active environment (the one its installed.json names)."""
    rec = _record(name)
    venv = server_dir(name) / (rec.get("venv") or "venv")
    return _py_in(venv)


def redact(text: str) -> str:
    """A source shown or logged without any user:password@ it carries."""
    import re
    return re.sub(r"(?<=://)[^/@\s]+@", "***@", str(text))


def _record(name: str) -> dict:
    try:
        return json.loads((server_dir(name) / "installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _source(name: str, srv: dict) -> tuple[str, str]:
    """(pip requirement, what it is) for this backend's server: a configured override, else the pinned commit."""
    override = (STATE.settings.get("server_sources") or {}).get(name.lower())
    if override:
        o = str(override)
        return (str(Path(o).resolve()) if os.path.isdir(o) else o), f"configured source {o}"
    commit = str(srv.get("commit") or "")
    if len(commit) != 40:
        raise InstallError(f"{name}: no pinned commit for {srv['package']} in this plugin release; set "
                           f"server_sources.{name} in the plugin settings to a local checkout")
    if shutil.which("git"):
        return f"{srv['package']} @ git+{srv['repo']}@{commit}", f"{srv['repo']} at commit {commit[:10]}"
    digest = str(srv.get("archive_sha256") or "")
    if len(digest) != 64:
        raise InstallError(f"{name}: installing {srv['package']} needs git (https://git-scm.com), or an archive "
                           f"checksum in this plugin release")
    # without git, GitHub's archive of the same commit; pip refuses it unless its sha256 matches the pinned one
    return (f"{srv['package']} @ {srv['repo']}/archive/{commit}.zip#sha256={digest}",
            f"{srv['repo']} at commit {commit[:10]} (archive, sha256 checked)")


def server_status(name: str) -> dict:
    """{"installed", "current", "version", "detail"} of a backend's add-on server."""
    srv = entry(name).get("server") or {}
    rec = _record(name)
    py = _venv_python(name)
    if not rec or not py.exists():
        return {"installed": False, "current": False, "package": srv.get("package"), "version": srv.get("version"),
                "detail": f"{srv.get('package')} is not installed (cad_backend(action='install', backend='{name}'))"}
    try:
        want = _source(name, srv)[0]
    except InstallError:
        want = rec.get("requirement")
    current = rec.get("requirement") == want
    rec = {**rec, "source": redact(rec.get("source", ""))}
    return {"installed": True, "current": current, "package": srv.get("package"), "version": rec.get("version"),
            "pinned_version": srv.get("version"), "at": rec.get("at"),
            "detail": "up to date" if current else
            f"an update is available ({rec.get('version')} -> {srv.get('version')}): cad_backend(action='install', "
            f"backend='{name}')"}


def install_notice(name: str) -> str:
    """What the approval prompt tells the user before a download."""
    spec = entry(name)
    srv = spec.get("server") or {}
    req, what = _source(name, srv)
    _APPROVED[name.lower()] = req          # install() installs exactly this, or asks again
    what = redact(what)
    return (f"hermes-shaperotator wants to download and install {srv.get('package')} {srv.get('version', '')} "
            f"({srv.get('licence', '')} licence), the add-on that drives {spec.get('title', name)}: {what}, plus "
            f"{srv.get('also_installs', 'its dependencies')}; about {srv.get('size_mb', '?')} MB. It goes into its own "
            f"Python environment in {server_dir(name)}; nothing else on this computer changes, and deleting that "
            "folder removes it. Approve only if you want this CAD program used.")


def _run(cmd: list[str], log, timeout: float, cwd: Path | None = None) -> None:
    env = child_env({k: os.environ[k] for k in NETWORK_ENV if k in os.environ} | {"PIP_DISABLE_PIP_VERSION_CHECK": "1",
                                                                                 "PYTHONUNBUFFERED": "1"})
    log.write(f"$ {redact(' '.join(cmd))}\n")
    log.flush()
    # run from the backend's own folder, so nothing in Hermes's working folder can stand in for a module
    p = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=timeout, cwd=str(cwd) if cwd else None,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode:
        raise InstallError(f"{cmd[2] if len(cmd) > 2 else cmd[0]} failed (exit {p.returncode}); see {log.name}")


def _base_python() -> str:
    for cand in (getattr(sys, "_base_executable", ""), sys.executable):
        if cand and os.path.exists(cand) and "pythonw" not in os.path.basename(cand).lower():
            return cand
    return sys.executable


def install(name: str) -> dict:
    """Install the backend's server into a new environment of its own (the user approved exactly this source
    first). The environment in use is left alone until the new one has installed and imported cleanly; only then
    does installed.json switch to it, and the previous one is removed."""
    import shutil
    spec = entry(name)
    srv = spec.get("server")
    if not srv:
        return {"installed": True, "detail": f"{spec.get('title', name)} is built in"}
    req, what = _source(name, srv)
    if _APPROVED.get(name.lower()) != req:
        raise InstallError("the source to install isn't the one shown in the approval prompt (it changed, or no "
                           "prompt was shown); ask again with cad_backend(action='install')")
    with _LOCK:
        d = server_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        import uuid
        stamp = time.strftime("%Y%m%d-%H%M%S")
        venv = d / f"venv-{stamp}-{uuid.uuid4().hex[:6]}"      # never the name of the environment in use
        prev = _record(name)
        try:
            with open(d / f"install-{stamp}.log", "w", encoding="utf-8") as log:
                _run([_base_python(), "-I", "-m", "venv", str(venv)], log, 300, cwd=d)
                py = str(_py_in(venv))
                pip = [py, "-I", "-m", "pip", "--isolated", "install", "--no-input"]
                _run(pip + ["--no-deps", req], log, INSTALL_TIMEOUT_S, cwd=d)
                _run(pip + [req], log, INSTALL_TIMEOUT_S, cwd=d)              # its dependencies
                probe = subprocess.run([py, "-I", "-c",
                                        f"import importlib.metadata as m; print(m.version({srv['package']!r}))"],
                                       capture_output=True, text=True, timeout=120, env=child_env(), cwd=str(d),
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                version = probe.stdout.strip()
                if probe.returncode or not version:
                    raise InstallError(f"{srv['package']} installed but can't be imported: "
                                       f"{probe.stderr.strip()[-400:]}")
        except BaseException:
            if venv.name != prev.get("venv"):
                shutil.rmtree(venv, ignore_errors=True)                # the environment in use is untouched
            raise
        old = STATE.backends.pop(name.lower(), None)                   # its server runs from the old environment
        if old is not None:
            try:
                old.shutdown()
            except Exception:
                pass
        rec = {"package": srv["package"], "version": version, "requirement": req, "source": redact(what),
               "venv": venv.name, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0)}
        tmp = d / "installed.json.tmp"
        tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
        tmp.replace(d / "installed.json")                              # the switch: one atomic rename
        _APPROVED.pop(name.lower(), None)
        old_venv = d / (prev.get("venv") or "venv")
        if prev and old_venv != venv:
            shutil.rmtree(old_venv, ignore_errors=True)
    return {"installed": True, **{k: v for k, v in rec.items() if k != "requirement"}, "folder": str(d)}


def server_config(name: str, values: dict[str, str] | None = None) -> dict:
    """The MCP server command for an installed backend: {"command", "env"} with {placeholders} filled in."""
    srv = entry(name).get("server") or {}
    if not server_status(name)["installed"]:
        raise InstallError(server_status(name)["detail"])
    vals = {"logo": str(PLUGIN_ROOT / "assets" / "shaperotator-logo.png"), **(values or {})}
    env = {}
    for k, v in (srv.get("env") or {}).items():
        try:
            filled = str(v).format(**vals)
        except KeyError:
            continue
        if filled:
            env[k] = filled
    return {"command": [str(_venv_python(name)), "-m", srv["module"]], "env": env}


# --------------------------------------------------------------------------- the chat's choice
def _choice_file() -> Path:
    return _data_dir() / "setup.json"


def chosen() -> str:
    """The backend the user picked (kept across restarts), or "" when they haven't been asked yet."""
    try:
        return str(json.loads(_choice_file().read_text(encoding="utf-8")).get("backend") or "")
    except (OSError, ValueError):
        return ""


def remember(name: str) -> None:
    tmp = _choice_file().with_suffix(".tmp")
    tmp.write_text(json.dumps({"backend": name, "at": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
    tmp.replace(_choice_file())


def setup_question(found: dict[str, dict] | None = None) -> str:
    """What the agent should ask when no CAD program has been chosen yet ("" when there is nothing to ask)."""
    found = found if found is not None else detect_all()
    here = {k: v for k, v in found.items() if v.get("found")}
    if not here:
        return ("No supported CAD program was found on this computer. Tell the user ShapeRotator can drive "
                + ", ".join(v["title"] for v in found.values()) + " and stop until one is installed.")
    parts = []
    for k, v in here.items():
        if v.get("builtin"):
            state = "ready"
        elif (v.get("server") or {}).get("installed"):
            state = "ready" if v["server"].get("current") else "ready, add-on update available"
        else:
            state = f"needs its add-on ({(v.get('server') or {}).get('package')}): a one-time download the user approves"
        parts.append(f"{v.get('version') or v['title']} [{k}: {state}]")
    return ("CAD program not chosen yet. Found on this computer: " + "; ".join(parts) + ". Ask the user once which "
            "to use (the clarify tool if you have it). Then call cad_backend(action='use', backend=<name>); for one "
            "that needs its add-on, call cad_backend(action='install', backend=<name>) first (if its approval is "
            "refused or can't be shown, tell the user and stop; never install it another way). The choice is kept.")
