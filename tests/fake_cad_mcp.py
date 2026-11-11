"""A tiny stand-in CAD MCP server (stdio) for the backend-swap test: a 'box modeller' with three tools."""
import json
import sys

STATE = {"doc": None, "volume": 0.0}
TOOLS = [
    {"name": "get_status", "description": "status", "inputSchema": {"type": "object"}},
    {"name": "create_document", "description": "new doc", "inputSchema": {"type": "object"}},
    {"name": "pad", "description": "extrude a 10x10 square", "inputSchema": {"type": "object"}},
]


def call(name, a):
    if name == "get_status":
        return {"document": STATE["doc"], "volume_mm3": STATE["volume"]}
    if name == "create_document":
        STATE["doc"] = f"{a.get('title')} ({a.get('kind')})"
        return {"document": STATE["doc"]}
    if name == "pad":
        before = STATE["volume"]
        STATE["volume"] += 100.0 * float(a["length"])
        return {"delta": f"Pad | V {STATE['volume']:g} ({STATE['volume'] - before:+g} mm³)"}
    raise KeyError(name)


for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m, p = msg["method"], msg.get("params") or {}
    if m == "initialize":
        res = {"protocolVersion": p.get("protocolVersion"), "capabilities": {"tools": {}},
               "serverInfo": {"name": "fake-cad", "version": "0.1"}}
    elif m == "tools/list":
        res = {"tools": TOOLS}
    elif m == "tools/call":
        try:
            res = {"content": [{"type": "text", "text": json.dumps(call(p["name"], p.get("arguments") or {}))}],
                   "isError": False}
        except Exception as e:  # noqa: BLE001
            res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
    sys.stdout.flush()
