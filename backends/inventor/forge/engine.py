"""Forge: runs registered tools on the COM apartment and turns failures into readable errors."""
from __future__ import annotations

import inspect
import time
import traceback
from typing import Any

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


def _is_busy(e: BaseException) -> bool:
    return type(e).__name__ == "com_error" and bool(e.args) and e.args[0] in RPC_BUSY


class Forge:
    def __init__(self, visible: bool = True):
        load_all()
        self.apartment = Apartment()
        self.session = Session(visible=visible)
        self.tools = TOOLS

    def call(self, name: str, args: dict | None = None, timeout: float | None = 1800) -> dict:
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
            except TypeError as e:
                if "argument" in str(e):
                    raise InventorError(f"{name}: {e}") from e
                raise
            except Exception as e:
                if type(e).__name__ == "com_error":
                    raise InventorError(f"{name}: Inventor rejected the operation — {com_error_text(e)}") from e
                raise
            return _plain(out) if isinstance(out, dict) else {"result": _plain(out)}

        return self.apartment.run(run, timeout=timeout)

    def safe_call(self, name: str, args: dict | None = None) -> dict:
        """Never raises: {"ok": False, "error": ...} on failure."""
        try:
            return {"ok": True, **self.call(name, args)}
        except InventorError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc(limit=4)[-1500:]}
