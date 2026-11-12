"""COM plumbing for Autodesk Inventor: one apartment thread, constants from the type library,
unit conversion and a typed-invoke helper for methods with [out] parameters.

Inventor's API works in database units: centimetres, radians, cm², cm³, kg. Everything this
package exposes is in millimetres and degrees; conversion happens at the edge.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import queue
import threading
from typing import Any, Callable, Iterator

INVENTOR_TYPELIB = "{D98A091D-3A0F-4C3E-B36E-61F62068D488}"
PROGID = "Inventor.Application"

MM = 0.1            # 1 mm in cm
CM3_TO_MM3 = 1000.0
CM2_TO_MM2 = 100.0


class InventorError(RuntimeError):
    """A failure the caller should see verbatim (bad index, feature failed, no document...)."""


# --------------------------------------------------------------------------- apartment thread
class Apartment:
    """All COM traffic goes through one single-threaded apartment thread."""

    def __init__(self, name: str = "inventor-forge-sta"):
        self._q: queue.Queue = queue.Queue()
        self._ident: int | None = None
        self._t = threading.Thread(target=self._run, name=name, daemon=True)
        self._t.start()

    def _run(self) -> None:
        import pythoncom
        pythoncom.CoInitialize()
        self._ident = threading.get_ident()
        try:
            while True:
                fn, fut = self._q.get()
                if fn is None:
                    break
                if fut.set_running_or_notify_cancel():
                    try:
                        fut.set_result(fn())
                    except BaseException as e:  # noqa: BLE001 - re-raised in the caller
                        fut.set_exception(e)
        finally:
            pythoncom.CoUninitialize()

    def run(self, fn: Callable[[], Any], timeout: float | None = 1800) -> Any:
        if threading.get_ident() == self._ident:      # re-entrant call from inside the apartment
            return fn()
        fut: cf.Future = cf.Future()
        self._q.put((fn, fut))
        return fut.result(timeout=timeout)

    def close(self) -> None:
        self._q.put((None, None))


# --------------------------------------------------------------------------- constants
class _Constants:
    """Attribute access to every enum value in Inventor's type library (kJoinOperation, ...)."""

    def __init__(self):
        self._values: dict[str, int] | None = None

    def _load(self) -> dict[str, int]:
        if self._values is None:
            cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_constants_cache.json")
            values: dict[str, int] = {}
            try:
                import pythoncom
                tlb = pythoncom.LoadRegTypeLib(INVENTOR_TYPELIB, 1, 0, 0)
                for i in range(tlb.GetTypeInfoCount()):
                    t = tlb.GetTypeInfo(i)
                    a = t.GetTypeAttr()
                    if a.typekind != pythoncom.TKIND_ENUM:
                        continue
                    for j in range(a.cVars):
                        vd = t.GetVarDesc(j)
                        values[t.GetNames(vd.memid)[0]] = int(vd.value)
                try:
                    with open(cache, "w", encoding="utf-8") as f:
                        json.dump(values, f)
                except OSError:
                    pass
            except Exception:
                if os.path.exists(cache):
                    with open(cache, encoding="utf-8") as f:
                        values = json.load(f)
            self._values = values
        return self._values

    def __getattr__(self, name: str) -> int:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._load()[name]
        except KeyError:
            raise AttributeError(f"Inventor has no constant {name!r}") from None

    def get(self, name: str, default: int | None = None) -> int | None:
        return self._load().get(name, default)

    def name_of(self, value: int, prefix: str) -> str:
        for k, v in self._load().items():
            if v == value and k.startswith(prefix):
                return k
        return str(value)


K = _Constants()


# --------------------------------------------------------------------------- late binding
_LateClass = None


def _late_class():
    """A dynamic.CDispatch whose *method* results stay late-bound too.

    pywin32 builds late-bound methods (Documents.Open, .Item, .Add ...) with win32com.client.Dispatch, which
    consults the gen_py cache. On a PC where that cache holds Inventor's type library, a method declared to
    return Document comes back as the early-bound base Document class, with no ComponentDefinition even for a
    part, so everything after it fails. Properties are unaffected. This class swaps that Dispatch for one that
    always wraps late, so behaviour no longer depends on what else has run on the machine."""
    global _LateClass
    if _LateClass is None:
        import win32com.client.dynamic as dyn

        def _late_dispatch(ob, userName=None, *_a, **_k):
            return late(ob, userName)

        class LateDispatch(dyn.CDispatch):
            def _wrap_dispatch_(self, ob, userName=None, returnCLSID=None):
                return late(ob, userName)

            def _make_method_(self, name):
                m = super()._make_method_(name)
                if m is not None:
                    m.__func__.__globals__["Dispatch"] = _late_dispatch
                return m

        _LateClass = LateDispatch
    return _LateClass


def late(obj, user_name: str | None = None):
    """Late-bound wrapper for any COM object (raw PyIDispatch, early-bound or dynamic). Other values pass through."""
    import pythoncom
    import win32com.client.dynamic as dyn
    if obj is None:
        return None
    cls = _late_class()
    if isinstance(obj, cls):
        return obj
    raw = getattr(obj, "_oleobj_", obj)
    if type(raw).__name__ == "PyIUnknown":
        try:
            raw = raw.QueryInterface(pythoncom.IID_IDispatch)
        except pythoncom.com_error:
            return obj
    if type(raw).__name__ != "PyIDispatch":
        return obj
    return dyn.Dispatch(raw, user_name, createClass=cls)


# --------------------------------------------------------------------------- helpers
def items(collection) -> Iterator[Any]:
    """1-based COM collection -> Python iterator."""
    if collection is None:
        return
    if isinstance(collection, (list, tuple)):
        yield from collection
        return
    n = collection.Count
    for i in range(1, n + 1):
        yield collection.Item(i)


def typed_call(obj, method: str, *args):
    """Call a method that has [out] parameters (late binding can't), returning the out values.

    Out parameters are passed as placeholders; the result is a tuple of every out value
    (plus the return value first when the method returns something).
    """
    import pythoncom
    ti = obj._oleobj_.GetTypeInfo()
    attr = ti.GetTypeAttr()
    for j in range(attr.cFuncs):
        fd = ti.GetFuncDesc(j)
        if ti.GetNames(fd.memid)[0] != method:
            continue
        def conv(vt):
            if isinstance(vt, int):
                return vt
            head, inner = vt[0], vt[1]
            if head == pythoncom.VT_PTR:
                base = conv(inner)
                if isinstance(inner, tuple) and inner[0] == pythoncom.VT_USERDEFINED and base == pythoncom.VT_DISPATCH:
                    return base               # Interface* is just an interface pointer passed by value
                return base | pythoncom.VT_BYREF
            if head == pythoncom.VT_SAFEARRAY:
                return conv(inner) | pythoncom.VT_ARRAY
            if head == pythoncom.VT_USERDEFINED:
                try:
                    kind = ti.GetRefTypeInfo(inner).GetTypeAttr().typekind
                except Exception:
                    return pythoncom.VT_DISPATCH
                return pythoncom.VT_I4 if kind == pythoncom.TKIND_ENUM else pythoncom.VT_DISPATCH
            return pythoncom.VT_VARIANT

        argtypes = [(conv(a[0]), a[1]) for a in fd.args]
        full = list(args)
        for k in range(len(full), len(argtypes)):
            base = argtypes[k][0] & ~pythoncom.VT_BYREF
            full.append(0.0 if base in (pythoncom.VT_R8, pythoncom.VT_R4) else 0 if base in (
                pythoncom.VT_I4, pythoncom.VT_I2, pythoncom.VT_BOOL) else None)
        rt = fd.rettype[0] if not isinstance(fd.rettype[0], tuple) else pythoncom.VT_VARIANT
        res = obj._oleobj_.InvokeTypes(fd.memid, 0, fd.invkind, (rt, 0), tuple(argtypes), *full)
        if not isinstance(res, tuple):
            res = (res,)
        return tuple(late(v) if type(v).__name__ == "PyIDispatch" else v for v in res)
    raise InventorError(f"{method} not found on {ti.GetDocumentation(-1)[0]}")


def safe(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def com_error_text(e: BaseException) -> str:
    """Readable text for a pywintypes.com_error (Inventor puts little in them)."""
    args = getattr(e, "args", ())
    if len(args) >= 3 and isinstance(args[2], tuple) and args[2] and args[2][2]:
        return f"{args[2][2]} (hresult {args[0]})"
    if len(args) >= 2 and isinstance(args[1], str):
        return f"{args[1]} (hresult {args[0]})"
    return str(e)


def r(x: float, nd: int = 3) -> float:
    return round(float(x), nd)
