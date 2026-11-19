"""CAD backends as separate MCP servers: progress keeps a long call alive, the server's cad profile answers the
plugin's questions, detection and the install flow (no download: the pip runs are replaced)."""
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

from conftest import load_plugin

P = load_plugin()
from hermes_shaperotator.core import installer, server_profile  # noqa: E402
from hermes_shaperotator.core.mcp_client import McpClient, McpError  # noqa: E402
from hermes_shaperotator.core.state import STATE  # noqa: E402

SERVER = r'''
import json, sys, time
def send(m): print(json.dumps(m), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    if req["method"] == "initialize":
        send({"jsonrpc": "2.0", "id": req["id"], "result": {"serverInfo": {"name": "fake"},
              "capabilities": {"experimental": {"cad/profile": {"version": 1, "progress_every_s": 0.05}}}}})
    elif req["method"] == "tools/call":
        tok = req["params"]["_meta"]["progressToken"]
        beats = req["params"]["arguments"].get("beats", 0)
        for i in range(beats):
            time.sleep(0.1)
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": tok, "progress": i, "message": f"step {i}"}})
        if req["params"]["arguments"].get("hang"):
            time.sleep(30)
        send({"jsonrpc": "2.0", "id": req["id"], "result": {"content": [{"type": "text", "text": "{\"ok\": true}"}]}})
'''


def _client(timeout):
    c = McpClient([sys.executable, "-u", "-c", SERVER], timeout=timeout)
    c.progress_grace_s = 0.4
    return c


def test_progress_keeps_a_call_alive_past_its_budget():
    c = _client(0.5)
    seen = []
    c.on_progress = seen.append
    try:
        c.start()
        assert c.profile["version"] == 1
        # 8 beats x 0.1 s = 0.8 s, past the 0.5 s budget but within twice it
        assert c.call("work", {"beats": 8}, mutating=False, timeout=0.5) == {"ok": True}
        assert seen and seen[-1] == "step 7"
    finally:
        c.close()


def test_silence_still_times_out():
    c = _client(0.3)
    try:
        c.start()
        with pytest.raises(McpError, match="timed out"):
            c.call("work", {"hang": True}, mutating=False, timeout=0.3)
    finally:
        c.close()


def test_progress_never_extends_past_twice_the_budget():
    c = _client(0.3)
    try:
        c.start()
        with pytest.raises(McpError, match="last progress"):
            c.call("work", {"beats": 40}, mutating=False, timeout=0.3)
    finally:
        c.close()


# --------------------------------------------------------------------------- profile questions
class _FakeClient:
    initialized = True
    profile = {"version": 1}
    server_info = {"name": "fake"}
    tools = {
        "extrude": {"_meta": {"cad/geometry": True, "cad/budget": {"floor_s": 1800}}},
        "list_faces": {"annotations": {"readOnlyHint": True}, "_meta": {}},
        "study_report": {"_meta": {"cad/takes_input": {"if_args": {"only_this_study": True}}}},
        "execute_python": {"_meta": {"cad/runs_code": True}},
        "export_pdf": {"_meta": {"cad/files": {"writes": ["path"]}}},
        "run_study_cases": {"_meta": {"cad/budget": {"floor_s": 1800, "timeout_arg": "timeout", "timeout_default": 1000,
                                                     "per_item_s": 600, "extra_s": 300,
                                                     "repeat_args": {"cases": None}}}},
    }


@pytest.fixture
def fake_server(monkeypatch):
    monkeypatch.setattr(STATE, "backend", lambda *a: SimpleNamespace(client=_FakeClient(), ops={}))


def test_profile_answers(fake_server):
    assert server_profile.geometry("extrude") and not server_profile.geometry("list_faces")
    assert server_profile.takes_input("study_report", {"only_this_study": True})
    assert not server_profile.takes_input("study_report", {})
    assert server_profile.runs_code("execute_python") and not server_profile.runs_code("extrude")
    assert server_profile.files("export_pdf") == {"writes": ["path"], "reads": [], "names": []}
    assert server_profile.files("extrude") == {"writes": [], "reads": [], "names": []}
    assert server_profile.files("not_a_tool") is None
    assert server_profile.budget("extrude", {}, 900) == 1800
    assert server_profile.budget("run_study_cases", {"cases": [1, 2]}, 900) == 2 * 1600 + 300


def test_hooks_follow_the_profile(fake_server, monkeypatch):
    from hermes_shaperotator.core.workbench import LIVE
    monkeypatch.setattr(LIVE, "enter", lambda: None)
    d = P._pre_tool_call(tool_name="cad_native", args={"tool": "study_report", "args": {"only_this_study": True}})
    assert d and "MOUSE" in d["message"]
    d = P._pre_tool_call(tool_name="cad_native", args={"tool": "execute_python", "args": {"code": "1"}})
    assert d and "Python" in d["message"]
    assert P._geometry_changed("cad_native", {"tool": "extrude"})
    assert not P._geometry_changed("cad_native", {"tool": "list_faces"})


# --------------------------------------------------------------------------- detection and install
@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_data_dir", lambda: tmp_path)
    return tmp_path


def test_known_backends_parse():
    k = installer.known()
    assert {"freecad", "inventor"} <= set(k)
    srv = k["inventor"]["server"]
    assert srv["module"] == "inventor_forge.server" and srv["package"] == "inventor-forge"


def test_setup_question_names_what_was_found(data, monkeypatch):
    found = {"freecad": {"found": True, "title": "FreeCAD", "version": "FreeCAD 1.1", "builtin": True},
             "inventor": {"found": True, "title": "Autodesk Inventor", "version": "Inventor 2027", "builtin": False,
                          "server": {"installed": False, "package": "inventor-forge"}}}
    q = installer.setup_question(found)
    assert "FreeCAD 1.1 [freecad: ready]" in q and "Inventor 2027" in q and "inventor-forge" in q
    assert "action='install'" in q
    none = installer.setup_question({"freecad": {"found": False, "title": "FreeCAD"}})
    assert "No supported CAD program" in none


def test_install_uses_the_configured_source_and_records_it(data, monkeypatch, tmp_path):
    src = tmp_path / "forge-checkout"
    src.mkdir()
    monkeypatch.setitem(STATE.settings, "server_sources", {"inventor": str(src)})
    ran = []

    def fake_run(cmd, log, timeout, cwd=None):
        ran.append(cmd)
        assert cwd == installer.server_dir("inventor")
        if "venv" in cmd:
            py = installer._py_in(pathlib.Path(cmd[-1]))
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("")
        else:
            assert "--isolated" in cmd and "-I" in cmd
    monkeypatch.setattr(installer, "_run", fake_run)
    monkeypatch.setattr(installer.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="0.2.0\n", stderr=""))
    assert not installer.server_status("inventor")["installed"]
    with pytest.raises(installer.InstallError, match="approval prompt"):
        installer.install("inventor")                      # nothing shown, nothing installed
    notice = installer.install_notice("inventor")
    assert "inventor-forge" in notice and str(src) in notice and "approve" in notice.lower()
    rec = installer.install("inventor")
    assert rec["version"] == "0.2.0" and any(str(src.resolve()) in c for c in ran[-1])
    first_env = installer._venv_python("inventor")
    assert first_env.exists()
    st = installer.server_status("inventor")
    assert st["installed"] and st["current"]
    cfg = installer.server_config("inventor", {"job_dir": "C:/jobs/x"})
    assert cfg["command"][1:] == ["-m", "inventor_forge.server"]
    assert cfg["env"]["INVENTOR_FORGE_WORKDIR"] == "C:/jobs/x" and cfg["env"]["INVENTOR_FORGE_LOGO"].endswith(".png")
    # a different source later (a plugin release moved the pin) shows as an update
    monkeypatch.setitem(STATE.settings, "server_sources", {"inventor": str(tmp_path)})
    assert not installer.server_status("inventor")["current"]
    # an update that fails part-way leaves the working environment in use
    installer.install_notice("inventor")

    def failing_run(cmd, log, timeout, cwd=None):
        if "venv" in cmd:
            fake_run(cmd, log, timeout, cwd)
        elif "--no-deps" not in cmd:
            raise installer.InstallError("dependency download failed")
    monkeypatch.setattr(installer, "_run", failing_run)
    with pytest.raises(installer.InstallError):
        installer.install("inventor")
    assert installer._venv_python("inventor") == first_env and first_env.exists()
    assert len([p for p in installer.server_dir("inventor").iterdir() if p.name.startswith("venv-")]) == 1


def test_credentials_in_a_source_are_hidden():
    assert installer.redact("git+https://user:secret@example.com/r.git") == "git+https://***@example.com/r.git"
    assert installer.redact("C:/src/forge") == "C:/src/forge"


def test_unpinned_release_refuses_to_guess(data, monkeypatch):
    monkeypatch.setitem(STATE.settings, "server_sources", {})
    monkeypatch.setattr(installer, "known", lambda: {"inventor": {"title": "Autodesk Inventor", "server": {
        "package": "inventor-forge", "repo": "https://example.invalid/x", "commit": "PENDING"}}})
    with pytest.raises(installer.InstallError, match="no pinned commit"):
        installer.install_notice("inventor")


def test_pinned_commit_is_installed_from_its_repository(data, monkeypatch):
    sha = "a" * 40
    monkeypatch.setitem(STATE.settings, "server_sources", {})
    monkeypatch.setattr(installer, "known", lambda: {"inventor": {"title": "Autodesk Inventor", "server": {
        "package": "inventor-forge", "repo": "https://github.com/o/r", "commit": sha}}})
    monkeypatch.setattr(installer.shutil, "which", lambda exe: None)
    req, what = installer._source("inventor", installer.entry("inventor")["server"])
    assert req == f"inventor-forge @ https://github.com/o/r/archive/{sha}.zip" and sha[:10] in what
    monkeypatch.setattr(installer.shutil, "which", lambda exe: "git")
    assert installer._source("inventor", installer.entry("inventor")["server"])[0].endswith(f"r@{sha}")


def test_install_needs_approval_and_choice_is_remembered(data, monkeypatch):
    from hermes_shaperotator.core.tools import HANDLERS
    monkeypatch.setitem(STATE.settings, "server_sources", {"inventor": str(data)})
    d = P._pre_tool_call(tool_name="cad_backend", args={"action": "install", "backend": "inventor"})
    assert d["action"] == "approve" and "inventor-forge" in d["message"]
    out = json.loads(HANDLERS["cad_backend"]({"action": "list"}))
    assert out["ok"] and "freecad" in out["found"]
    installer.remember("freecad")
    assert installer.chosen() == "freecad"


# --------------------------------------------------------------------------- after the code review
class _Plain:
    """A server without a cad profile."""
    initialized = True
    profile = None
    server_info = {"name": "plain"}
    tools = {"macro": {"description": "Do a thing", "annotations": {}},
             "peek": {"description": "Look", "annotations": {"readOnlyHint": True}}}


def test_deleting_calls_ask_first(fake_server, monkeypatch):
    from hermes_shaperotator.core.workbench import LIVE
    monkeypatch.setattr(LIVE, "enter", lambda: None)
    _FakeClient.tools["purge_studies"] = {"_meta": {"cad/deletes": {"if_args": {"confirm": True}}}}
    try:
        d = P._pre_tool_call(tool_name="cad_native", args={"tool": "purge_studies", "args": {"keep": ["A"], "confirm": True}})
        assert d and d["action"] == "approve" and "permanently" in d["message"]
        assert P._pre_tool_call(tool_name="cad_native", args={"tool": "purge_studies", "args": {"keep": ["A"]}}) is None
    finally:
        _FakeClient.tools.pop("purge_studies")


def test_unprofiled_server_tools_ask_unless_read_only(monkeypatch):
    from hermes_shaperotator.core.workbench import LIVE
    monkeypatch.setattr(LIVE, "enter", lambda: None)
    monkeypatch.setattr(STATE, "backend", lambda *a: SimpleNamespace(client=_Plain(), ops={}))
    d = P._pre_tool_call(tool_name="cad_native", args={"tool": "macro", "args": {}})
    assert d and d["action"] == "approve" and "doesn't describe" in d["message"]
    assert P._pre_tool_call(tool_name="cad_native", args={"tool": "peek", "args": {}}) is None


def test_user_notified_only_for_tools_that_take_it(fake_server):
    _FakeClient.tools["analyze_stress"] = {"_meta": {"cad/takes_input": True},
                                           "inputSchema": {"properties": {"user_notified": {}}}}
    _FakeClient.tools["other_mouse"] = {"_meta": {"cad/takes_input": True}, "inputSchema": {"properties": {}}}
    try:
        assert server_profile.accepts("analyze_stress", "user_notified")
        assert not server_profile.accepts("other_mouse", "user_notified")
    finally:
        _FakeClient.tools.pop("analyze_stress")
        _FakeClient.tools.pop("other_mouse")


def test_status_never_starts_a_cad_program(data, monkeypatch):
    from hermes_shaperotator.core import tools as T
    started = []
    fake = SimpleNamespace(client=SimpleNamespace(initialized=False), available=lambda: (True, "ok"),
                           call=lambda *a, **k: started.append(a) or {})
    monkeypatch.setattr(STATE, "backend", lambda *a: fake)
    monkeypatch.setitem(STATE.backends, STATE.backend_name, fake)
    monkeypatch.setattr(STATE, "backend_chosen", STATE.backend_name)
    out = json.loads(T.HANDLERS["cad_status"]({}))
    assert out["ok"] and out["document"] is None and not started
