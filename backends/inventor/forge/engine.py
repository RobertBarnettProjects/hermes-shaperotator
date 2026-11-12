"""Forge: runs registered tools on the COM apartment and turns failures into readable errors."""
from __future__ import annotations

import inspect
import time
from concurrent.futures import TimeoutError as FutureTimeout
import traceback
from typing import Any

from .standards import StandardError
from .com import Apartment, InventorError, com_error_text
from .registry import TOOLS, load_all
from .session import Session


def _plain(v: Any) -> Any:
    """Tool results must be plain data: a COM object that slips through becomes its string form."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


RPC_BUSY = (-2147418111, -2147417846)     # RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER
DEFAULT_WAIT = 1800.0                     # s: how long a call may hold the COM thread, unless the tool says otherwise
BATCH_ARGS = {"cases": None, "fractions": 3}   # list arguments that repeat a tool's own timeout (default count)


def call_budget(fn, args: dict, default: float | None = DEFAULT_WAIT) -> float | None:
    """How long to wait for a tool. A tool with its own timeout argument (a solve) gets that, once per case or mesh
    size for batch tools, plus time for the copying, reading and saving around it, so the wait never ends before
    the tool's own limit does (a solve allowed 3600 s used to be cut off by the 1800 s default)."""
    sig = inspect.signature(fn).parameters
    if "timeout" not in sig or default is None:
        return default
    own = args.get("timeout", sig["timeout"].default)
    try:
        own = float(own)
    except (TypeError, ValueError):
        return default
    n = 1
    for name, fallback in BATCH_ARGS.items():
        if name in sig:
            v = args.get(name)
            n = max(n, len(v) if isinstance(v, (list, tuple)) else (fallback or 1))
    return max(default, n * (own + 600.0) + 300.0)


def _is_busy(e: BaseException) -> bool:
    return type(e).__name__ == "com_error" and bool(e.args) and e.args[0] in RPC_BUSY


class Forge:
    def __init__(self, visible: bool = True):
        load_all()
        self.apartment = Apartment()
        self.session = Session(visible=visible)
        self.tools = TOOLS

    def call(self, name: str, args: dict | None = None, timeout: float | None = DEFAULT_WAIT) -> dict:
        t = self.tools.get(name)
        if t is None:
            raise InventorError(f"unknown tool {name!r}")
        args = dict(args or {})
        params = list(inspect.signature(t.fn).parameters)[1:]
        unknown = [k for k in args if k not in params]
        if unknown:
            raise InventorError(f"{name}: unknown argument(s) {unknown}; expected {params}")

        if not t.com:               # reads Windows state only: must not wait behind a running solve
            out = t.fn(self.session, **args)
            return _plain(out) if isinstance(out, dict) else {"result": _plain(out)}

        def attempt():
            """Inventor refuses calls while it is busy. A read can simply be asked again."""
            tries = 6 if not t.mutates else 1
            for i in range(tries):
                try:
                    return t.fn(self.session, **args)
                except Exception as e:  # noqa: BLE001
                    if not _is_busy(e):
                        raise
                    if i == tries - 1:
                        raise InventorError(f"{name}: Inventor is busy and refused the call (a dialog may be open, or it "
                                            "is still computing). Check what was done with list_features or inspect, "
                                            "then try again.") from e
                    time.sleep(1.5)

        def run():
            busy = self.session.solver_busy()
            if busy:
                raise InventorError(f"Inventor is running a solve or report ({busy[0]!r}); calling it now can crash it. "
                                    "Wait until that finishes (solve_state shows progress), or close the dialog if it is idle.")
            try:
                out = attempt()
            except InventorError:
                raise
            except StandardError as e:
                raise InventorError(str(e)) from None
            except TypeError as e:
                if "argument" in str(e):
                    raise InventorError(f"{name}: {e}") from e
                raise
            except Exception as e:
                if type(e).__name__ == "com_error":
                    raise InventorError(f"{name}: Inventor rejected the operation — {com_error_text(e)}") from e
                raise
            return _plain(out) if isinstance(out, dict) else {"result": _plain(out)}

        wait = call_budget(t.fn, args, timeout)
        try:
            return self.apartment.run(run, timeout=wait)
        except FutureTimeout:
            raise InventorError(f"{name}: no answer after {wait:.0f} s. The call may still be running in Inventor (a solve "
                                "carries on regardless); check solve_state before calling anything else") from None

    def safe_call(self, name: str, args: dict | None = None) -> dict:
        """Never raises: {"ok": False, "error": ...} on failure."""
        try:
            return {"ok": True, **self.call(name, args)}
        except InventorError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc(limit=4)[-1500:]}
