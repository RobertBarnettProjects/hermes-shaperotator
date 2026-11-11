"""Swap the CAD program: drive a (fake) CAD MCP server through the same cad_* tools."""
import json
import os
import sys

from conftest import load_plugin

load_plugin()
from hermes_shaperotator.core.state import BACKENDS, STATE  # noqa: E402
from hermes_shaperotator.core.tools import HANDLERS  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fake_cad_mcp.py")


def call(_tool, **args):
    return json.loads(HANDLERS[_tool](args))


def test_swap_to_any_mcp_server():
    prev_name, prev_servers = STATE.backend_name, STATE.settings.get("mcp_servers")
    STATE.settings["mcp_servers"] = {"fake-cad": {
        "command": [sys.executable, "-u", FAKE],
        "ops": {"status": "get_status",
                "new_part": {"tool": "create_document", "args": {"name": "title"}, "fixed": {"kind": "part"}},
                "extrude": {"tool": "pad", "args": {"distance": "length"}}},
    }}
    try:
        assert "fake-cad" in BACKENDS
        st = call("cad_status")
        assert st["backends"]["fake-cad"]["available"], st
        r = call("cad_backend", backend="fake-cad")
        assert r["ok"], r
        assert r["detail"]["server"] == "fake-cad" and r["detail"]["tools"] == 3
        assert STATE.family == "generic"
        assert STATE.backend("fake-cad").call("new_part", name="Bracket")["document"] == "Bracket (part)"
        assert "+500" in STATE.backend("fake-cad").call("extrude", distance=5)["delta"]
        n = call("cad_native", tool="get_status", args={})
        assert n["ok"] and n["volume_mm3"] == 500.0, n
        bad = call("cad_native", tool="nope", args={})
        assert not bad["ok"] and "available" in bad["error"]
    finally:
        b = STATE.backends.pop("fake-cad", None)
        if b:
            b.shutdown()
        STATE.backend_name = prev_name
        STATE.settings["mcp_servers"] = prev_servers or {}


def test_invalid_configuration_isolated_in_status(monkeypatch, tmp_path):
    from hermes_shaperotator.backends.mcp.backend import make_mcp_backend
    import pytest
    invalid = {"command": '"C:/Program Files/CAD/server.exe" --stdio'}
    with pytest.raises(ValueError, match="argv array"):
        make_mcp_backend("bad", invalid, {})
    monkeypatch.setitem(STATE.settings, "mcp_servers", {"bad": invalid})
    monkeypatch.setattr(STATE, "job_dir", tmp_path)
    result = call("cad_status")
    assert result["ok"]
    assert not result["backends"]["bad"]["available"]
    assert "configuration error" in result["backends"]["bad"]["detail"]


def test_native_execution_requires_approval(monkeypatch):
    from types import SimpleNamespace
    from hermes_shaperotator.core.workbench import LIVE
    plugin = load_plugin()
    monkeypatch.setattr(LIVE, "enter", lambda: None)
    tools = {"innocent": {"description": "Execute Python script in the application"},
             "custom": {"description": "Custom operation"}, "status": {"description": "Status"}}
    backend = SimpleNamespace(client=SimpleNamespace(tools=tools), ops={"python": "custom"})
    monkeypatch.setattr(STATE, "backend", lambda *a: backend)
    for name in ("python", "run_code", "shell", "eval", "innocent", "custom"):
        decision = plugin._pre_tool_call(tool_name="cad_native", args={"tool": name, "args": {"code": "print(1)"}})
        assert decision["action"] == "approve"
