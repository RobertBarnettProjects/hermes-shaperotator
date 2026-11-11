"""Transport regressions use real child pipes and JSON-RPC server requests."""
import sys
import time

import pytest

from conftest import load_plugin

load_plugin()
from hermes_shaperotator.core.mcp_client import McpClient, McpError


def client_for(script, timeout=1):
    return McpClient([sys.executable, "-u", "-c", script], timeout=timeout)


def test_init_failure_closes_and_reaps():
    client = client_for("import time; time.sleep(30)", timeout=0.15)
    with pytest.raises(McpError, match="timed out"):
        client.start()
    assert client.proc is None
    assert not client.initialized
    # A second start attempts initialization again.
    with pytest.raises(McpError, match="timed out"):
        client.start()
    assert client.proc is None


def test_write_deadline_closes_transport():
    script = '''
import json, sys, time
req = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {}}), flush=True)
sys.stdin.readline()
time.sleep(30)
'''
    client = client_for(script, timeout=0.4)
    try:
        client.start()
        proc = client.proc
        start = time.monotonic()
        with pytest.raises(McpError, match="write timed out"):
            client.request("tools/call", {"code": "x" * 8_000_000})
        assert time.monotonic() - start < 5
        assert client.uncertain
        assert client.proc is None and proc.poll() is not None
    finally:
        client.close()


def test_server_ping_and_unknown_requests_during_initialize():
    script = '''
import json, sys
req = json.loads(sys.stdin.readline())
for method in ("ping", "sampling/createMessage"):
    print(json.dumps({"jsonrpc": "2.0", "id": method, "method": method}), flush=True)
    reply = json.loads(sys.stdin.readline())
    assert reply["id"] == method
    if method == "ping":
        assert reply["result"] == {}
    else:
        assert reply["error"]["code"] == -32601
print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"serverInfo": {"name": "ping-tested"}}}), flush=True)
for line in sys.stdin:
    pass
'''
    client = client_for(script)
    try:
        client.start()
        assert client.initialized
        assert client.server_info["name"] == "ping-tested"
    finally:
        client.close()


def test_timeout_blocks_mutations_but_allows_reads():
    script = '''
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    if req["method"] == "tools/call" and req["params"]["name"] == "slow":
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {}}), flush=True)
'''
    client = client_for(script, timeout=1)
    try:
        client.start()
        client.timeout = 0.2
        with pytest.raises(McpError, match="timed out"):
            client.call("slow", mutating=True)
        assert client.uncertain
        assert client.call("status", mutating=False) == {}
        with pytest.raises(McpError, match="uncertain"):
            client.call("extrude", mutating=True)
    finally:
        client.close()


def test_resync_waits_for_late_tool_completion():
    script = '''
import json, sys, time
for line in sys.stdin:
    req = json.loads(line)
    if "id" not in req:
        continue
    if req["method"] == "tools/call" and req["params"]["name"] == "slow":
        time.sleep(0.6)
    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {}}), flush=True)
'''
    client = client_for(script, timeout=1)
    try:
        client.start()
        client.timeout = 0.15
        with pytest.raises(McpError, match="timed out"):
            client.call("slow", mutating=True)
        with pytest.raises(McpError, match="still unresolved"):
            client.wait_for_pending_calls()
        assert client.uncertain
        client.timeout = 2
        client.wait_for_pending_calls()
        assert not client._pending_calls
        assert client.call("status", mutating=False) == {}
        # Settlement alone does not authorize another mutation. The backend must complete connect's status sync.
        assert client.uncertain
    finally:
        client.close()
