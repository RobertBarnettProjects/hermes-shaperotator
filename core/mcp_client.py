"""Minimal MCP stdio client (JSON-RPC 2.0, newline-delimited, no third-party dependencies).

hermes-shaperotator can drive a CAD program through *any* MCP server that speaks stdio: the server is a
child process started from config (``mcp_servers`` in the plugin settings). This is what makes the
plugin CAD-agnostic — swap the server, keep the workbench, plans, FEA checks, history and artifacts.
"""
from __future__ import annotations

import json
import math
import queue
import subprocess
import threading
import time
from typing import Any

PROTOCOL = "2025-06-18"


class McpError(RuntimeError):
    pass


class McpClient:
    def __init__(self, command: list[str], env: dict[str, str] | None = None, cwd: str | None = None,
                 timeout: float = 900.0):
        if not isinstance(command, list) or not command or any(not isinstance(c, str) or not c for c in command):
            raise McpError("MCP server command must be a non-empty argv array of strings")
        self.command = [str(c) for c in command]
        if env is not None and (not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items())):
            raise McpError("MCP env must be a string mapping")
        self.env = env or {}
        self.cwd = cwd
        self.timeout = float(timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise McpError("MCP timeout must be finite and positive")
        self.initialized = False
        self.uncertain = False
        self._write_lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.server_info: dict = {}
        self.instructions = ""
        self._tools: dict[str, dict] | None = None
        self._id = 0
        self._lock = threading.Lock()          # one request in flight (CAD programs are single-threaded)
        self._replies: dict[int, queue.Queue] = {}
        self._pending_calls: dict[int, threading.Event] = {}
        self._stderr: list[str] = []

    # ------------------------------------------------------------------ process
    def start(self) -> "McpClient":
        if self.initialized and self.proc and self.proc.poll() is None:
            return self
        if self.proc:
            self.close()
        self._tools = None
        self._write_lock = threading.Lock()
        from .paths import child_env
        # only what the OS needs plus the server's own configured env: never Hermes's provider keys
        env = child_env({**{k: str(v) for k, v in self.env.items()}, "PYTHONUNBUFFERED": "1"})
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=self.cwd or None, env=env,
                                     creationflags=flags)
        p = self.proc
        threading.Thread(target=self._read_stdout, args=(p,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(p,), daemon=True).start()
        try:
            init = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                               "clientInfo": {"name": "hermes-shaperotator", "version": "1"}}, timeout=min(120, self.timeout))
            self.server_info = init.get("serverInfo") or {}
            self.instructions = init.get("instructions") or ""
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.initialized = True
            return self
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        p, self.proc = self.proc, None
        self.initialized = False
        self._tools = None
        for event in list(self._pending_calls.values()):
            event.set()
        self._pending_calls.clear()
        for q in list(self._replies.values()):
            q.put({"error": {"message": "MCP transport closed"}})
        if p:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=10)
            # A descendant may still hold a pipe open. Pipe cleanup must never hold up a deadline.
            def close_pipes():
                for stream in (p.stdin, p.stdout, p.stderr):
                    if stream:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
            threading.Thread(target=close_pipes, daemon=True).start()

    def _send(self, msg: dict, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        p = self.proc
        if not p or p.poll() is not None:
            raise McpError(f"MCP server is not running; stderr: {self.stderr_tail()}")
        done = queue.Queue()
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        write_lock = self._write_lock
        def write():
            try:
                with write_lock:
                    p.stdin.write(data)
                    p.stdin.flush()
                done.put(None)
            except Exception as e:
                done.put(e)
        threading.Thread(target=write, daemon=True).start()
        try:
            error = done.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            if self.proc is p:
                self.close()
            raise McpError("MCP write timed out, transport closed") from None
        if error:
            raise McpError(f"MCP write failed: {error}")

    def _answer(self, msg: dict, p) -> None:
        if self.proc is not p:
            return
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        if msg["method"] == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": -32601, "message": f"Unsupported client method: {msg['method']}"}
        try:
            self._send(reply)
        except McpError:
            pass

    def _read_stdout(self, p) -> None:
        while self.proc is p:
            try:
                raw = p.stdout.readline()
            except (OSError, ValueError):
                break
            if not raw:
                break
            if self.proc is not p:
                return
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:
                threading.Thread(target=self._answer, args=(msg, p), daemon=True).start()
                continue
            event = self._pending_calls.get(msg.get("id"))
            if event is not None and ("result" in msg or "error" in msg):
                event.set()
            q = self._replies.get(msg.get("id"))
            if q is not None and ("result" in msg or "error" in msg):
                q.put(msg)
        if self.proc is not p:
            return
        for q in list(self._replies.values()):
            q.put({"error": {"message": f"MCP server exited; stderr: {self.stderr_tail()}"}})

    def _read_stderr(self, p) -> None:
        while self.proc is p:
            try:
                raw = p.stderr.readline()
            except (OSError, ValueError):
                break
            if not raw:
                break
            self._stderr.append(raw.decode("utf-8", "replace").rstrip())
            del self._stderr[:-50]

    def stderr_tail(self, n: int = 8) -> str:
        return " | ".join(self._stderr[-n:])

    # ------------------------------------------------------------------ protocol
    def request(self, method: str, params: dict | None = None, timeout: float | None = None, *, mutating: bool = False) -> dict:
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise McpError(f"MCP {method} timed out waiting for another request")
        try:
            if mutating and self.uncertain:
                raise McpError("Mutation outcome uncertain. Call connect to re-sync before further mutations.")
            self._id += 1
            rid, q = self._id, queue.Queue()
            self._replies[rid] = q
            event = threading.Event()
            if method == "tools/call":
                self._pending_calls[rid] = event
            try:
                self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, timeout=deadline - time.monotonic())
                try:
                    msg = q.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    raise McpError(f"MCP {method} timed out after {timeout or self.timeout:.0f} s") from None
            except McpError as e:
                if method == "tools/call" and "timed out" in str(e):
                    self.uncertain = True
                raise
            finally:
                self._replies.pop(rid, None)
                if event.is_set() or not self.uncertain:
                    self._pending_calls.pop(rid, None)
        finally:
            self._lock.release()
        if "error" in msg:
            err = msg["error"] or {}
            raise McpError(err.get("message") or json.dumps(err))
        return msg.get("result") or {}

    def wait_for_pending_calls(self) -> None:
        """A status read can re-sync only after previous server operations have settled."""
        deadline = time.monotonic() + self.timeout
        for rid, event in list(self._pending_calls.items()):
            if not event.wait(timeout=max(0, deadline - time.monotonic())):
                raise McpError("Previous tools/call is still unresolved. Outcome remains uncertain; connect again after it completes.")
            self._pending_calls.pop(rid, None)

    @property
    def tools(self) -> dict[str, dict]:
        if self._tools is None:
            self.start()
            out, cursor = {}, None
            while True:
                r = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=min(120, self.timeout))
                for t in r.get("tools", []):
                    out[t["name"]] = t
                cursor = r.get("nextCursor")
                if not cursor:
                    break
            self._tools = out
        return self._tools

    def call(self, name: str, args: dict | None = None, *, mutating: bool | None = None,
             timeout: float | None = None) -> dict:
        """Call a tool; returns its JSON result as a dict (text that isn't JSON comes back as {"text": ...})."""
        self.start()
        if mutating is None:
            mutating = not self.tools.get(name, {}).get("annotations", {}).get("readOnlyHint", False)
        if self.uncertain and mutating:
            raise McpError("Previous tools/call timed out, mutation outcome uncertain. Call connect to re-sync before further mutations.")
        r = self.request("tools/call", {"name": name, "arguments": args or {}}, timeout=timeout, mutating=mutating)
        texts = [c.get("text", "") for c in r.get("content", []) if c.get("type") == "text"]
        text = "\n".join(t for t in texts if t)
        if r.get("isError"):
            raise McpError(text or f"{name} failed")
        if isinstance(r.get("structuredContent"), dict):
            return r["structuredContent"]
        try:
            val = json.loads(text) if text else {}
        except ValueError:
            return {"text": text}
        return val if isinstance(val, dict) else {"result": val}
