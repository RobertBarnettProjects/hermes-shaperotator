"""Connection to a running (or newly started) Inventor and helpers shared by every tool."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .com import CM2_TO_MM2, CM3_TO_MM3, MM, InventorError, K, items, late, safe

# DocumentTypeEnum values are looked up lazily through K, these are just names
DOC_KIND = {"kPartDocumentObject": "part", "kAssemblyDocumentObject": "assembly",
            "kDrawingDocumentObject": "drawing", "kPresentationDocumentObject": "presentation"}


def restore_environment() -> list[str]:
    """Put back the standard Windows variables a client stripped from this server's environment.

    Some MCP clients start servers with a trimmed environment (no COMPUTERNAME, ALLUSERSPROFILE, COMMONPROGRAMFILES
    and so on). An Inventor that COM starts from such a process inherits it and dies at start-up with a licensing
    error ("Server execution failed"). Variables already set are left alone; only missing ones are added, from the
    user's own environment as Windows would build it at log-on. Returns the names added."""
    try:
        import win32api
        import win32con
        import win32profile
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        full = win32profile.CreateEnvironmentBlock(token, False)
    except Exception:
        return []
    have = {k.upper() for k in os.environ}
    added = []
    for k, v in full.items():
        if k and not k.startswith("=") and k.upper() not in have:
            os.environ[k] = v
            added.append(k)
    return added


def body_volume(b) -> float:
    """A body's volume in mm³ (0 for surfaces)."""
    v = safe(lambda: b.Volume(0.0001))
    if v is None:
        v = safe(lambda: float(b.Volume), 0.0)
    return round(float(v or 0.0) * CM3_TO_MM3, 3)


def body_changes(before: dict, after: dict, tol: float = 1e-3) -> dict:
    """Per-body volume change (mm³) between two snapshots; new and removed bodies included."""
    out = {}
    for name in list(before) + [n for n in after if n not in before]:
        a, b = before.get(name), after.get(name)
        if a is None:
            out[name] = f"new ({b:.0f} mm³)"
        elif b is None:
            out[name] = "gone"
        elif abs(b - a) > tol:
            out[name] = round(b - a, 3)
    return out


def set_name(obj, name: str, what: str = "item") -> str:
    """Rename and read the name back. Inventor silently keeps the old name when the new one is already used (a body
    named like a feature, for one) or not allowed, so a rename that didn't take is reported, not assumed."""
    old = safe(lambda: obj.Name, "")
    try:
        obj.Name = name
    except Exception as e:  # noqa: BLE001
        raise InventorError(f"Inventor refused to rename {what} {old!r} to {name!r} ({e}); the name may be in use or "
                            "not allowed") from None
    got = safe(lambda: obj.Name, "")
    if got != name:
        raise InventorError(f"Inventor kept the name {got!r} for {what} {old!r} instead of {name!r}: names already "
                            "used by another feature, body or parameter, and some reserved names, are ignored "
                            "without an error. Choose another name")
    return got


@dataclass
class Snapshot:
    volume: float = 0.0
    faces: int = 0
    edges: int = 0
    bodies: int = 0
    per_body: dict | None = None          # body name -> volume mm³, for multi-body parts


class Session:
    """Holds the Inventor application object and per-document working state."""

    def __init__(self, visible: bool = True):
        self.visible = visible
        self._app = None
        self._sketch: dict[str, Any] = {}      # doc key -> last active sketch
        self._txn = None                       # open Transaction (undo group)
        self._current = None                   # working document (see doc())
        self._current_key = None
        self.pid: int | None = None            # Inventor's process id, once connected
        self.pinned_pid: int | None = None     # the Inventor this session chose at connect; never silently another
        self.hwnd: int | None = None           # its main window, for hang checks made without COM
        self.private = False                   # True when this session started its own Inventor process
        self.workdir = os.path.join(os.path.expanduser("~"), "Documents", "inventor-forge")
        self.standard: str | None = None       # chosen by the user for this session (set_standard); None = not asked yet
        self.drawing_units = "mm"

    def standard_key(self, standard: str = "") -> str:
        """The standard a call should follow: the one it names, else the session's choice, else AS 1100."""
        from .standards import DEFAULT, key
        return key(standard) if standard else (self.standard or DEFAULT)

    # ------------------------------------------------------------------ connection
    @property
    def app(self):
        if self._app is not None:
            try:
                _ = self._app.Caption
                return self._app
            except Exception:
                self._app = None
                if self.private:               # never fall through to attaching to the user's own Inventor
                    self.private, self.pid = False, None
                    raise InventorError("the private Inventor this session started has closed; "
                                        "connect(private=true) starts another") from None
        self._app = self._connect()
        return self._app

    def _connect(self):
        import win32com.client as wc
        pinned = self.pinned_pid
        try:
            raw = wc.GetActiveObject("Inventor.Application")
        except Exception:
            if pinned:
                raise InventorError(f"the Inventor this session was working with (process {pinned}) has closed; "
                                    "nothing else was attached. connect(reattach=true) attaches to another Inventor "
                                    "or starts one") from None
            restore_environment()        # a new Inventor inherits this environment: it must be a complete one
            raw = wc.DispatchEx("Inventor.Application")
        app = late(raw)          # always late-bound, method results included: predictable across machines
        pid = self._process_id(app)
        if pinned and pid != pinned:
            raise InventorError(f"the Inventor this session was working with (process {pinned}) is gone or no longer "
                                f"registered; the one answering now is process {pid}, which may be someone else's. "
                                "Nothing was done in it. connect(reattach=true) switches to it deliberately")
        try:
            if self.visible and not app.Visible:
                app.Visible = True
        except Exception:
            pass
        self.pid = pid
        self.pinned_pid = pid
        self.hwnd = safe(lambda: int(app.MainFrameHWND))
        return app

    def unpin(self) -> None:
        """Forget the Inventor chosen at connect, so the next call may attach to whichever one is running."""
        self._app, self.pid, self.pinned_pid, self.hwnd, self._current = None, None, None, None, None

    @staticmethod
    def _process_id(app) -> int | None:
        import win32process
        return safe(lambda: win32process.GetWindowThreadProcessId(app.MainFrameHWND)[1])

    def start_private(self):
        """Start a separate Inventor process that belongs to this session, so a crash or a stray command
        can't touch what the user has open. Starts are serialised across processes by a lock file, so two sessions
        starting at once can each tell which new Inventor is theirs. If the new process can't be identified, the
        Inventor this call created is quit again before refusing."""
        import time

        import win32com.client as wc

        from .health import start_lock, wait_for_stagger
        from .uidrive import pids_named
        if self.private and self._app is not None:
            return self.app
        with start_lock():
            wait_for_stagger(pids_named("Inventor.exe"))
            before = set(pids_named("Inventor.exe"))
            restore_environment()            # a new Inventor inherits this environment: it must be a complete one
            raw = wc.DispatchEx("Inventor.Application")
            try:
                t0 = time.time()
                while not set(pids_named("Inventor.exe")) - before and time.time() - t0 < 120:
                    time.sleep(0.5)
                new = set(pids_named("Inventor.exe")) - before
                app = late(raw)
                pid = self._process_id(app) if new else None
                if pid not in new:
                    raise InventorError("could not start a separate Inventor (its process could not be identified); "
                                        "the Inventor this call started was closed again and nothing else was touched")
                app.Visible = True
            except BaseException:
                safe(lambda: setattr(late(raw), "SilentOperation", True))
                safe(lambda: late(raw).Quit())
                raise
        self._app, self.pid, self.private = app, pid, True
        self.pinned_pid = pid
        self.hwnd = safe(lambda: int(app.MainFrameHWND))
        self._current = None
        return app

    def solver_busy(self) -> list[str]:
        """Open Stress Analysis solve/report windows on the Inventor this session uses (Windows calls only)."""
        from .uidrive import solver_windows
        return solver_windows([self.pid] if self.pid else None)

    def stock_template(self, filename: str) -> str:
        """Autodesk's own metric template by file name. FileManager.GetTemplateFile follows the active
        Inventor project, which may hold someone else's templates; this doesn't. "" if it isn't installed."""
        import glob
        year = str(safe(lambda: self.app.SoftwareVersion.DisplayVersion, ""))[:4]
        root = r"C:\Users\Public\Documents\Autodesk"
        found = glob.glob(os.path.join(root, f"Inventor {year}", "Templates", "*", "Metric", filename))
        found += glob.glob(os.path.join(root, f"Inventor {year}", "Templates", "*", "English", filename))
        found.sort(key=lambda p: 0 if os.sep + "en-US" + os.sep in p else 1)
        return found[0] if found else ""

    def connected(self) -> bool:
        return self._app is not None

    # ------------------------------------------------------------------ documents
    def set_current(self, d) -> None:
        """The document this session is working on (created, opened or activated through a tool)."""
        d = late(d)
        self._current = d
        self._current_key = self.key(d) if d is not None else None

    def _working(self):
        cur = getattr(self, "_current", None)
        if cur is None:
            return None
        try:
            if self.key(cur) == self._current_key and cur.Views.Count >= 0:
                return cur
        except Exception:
            pass
        self._current = None
        return None

    def doc(self):
        """The session's working document, else Inventor's active document. In a private Inventor the working
        document is also brought back to the front if something took focus; attached to the user's Inventor it is
        left where it is (most API work doesn't need it in front). Steps that do need it, commands and the active
        view, go through front() / view()."""
        cur = self._working()
        if cur is not None:
            if self.private:
                self._raise(cur)
            return cur
        d = late(self.app.ActiveDocument)
        if d is None:
            raise InventorError("no active document — create_part / create_assembly / open_document first")
        return d

    def _raise(self, cur) -> bool:
        act = self.app.ActiveDocument
        if act is None or self.key(act) != self._current_key:
            try:
                cur.Activate()
                return True
            except Exception:
                pass
        return False

    def focus_note(self) -> str:
        """When attached to the user's Inventor: a note if the session worked on a different document from the one
        the user has in front (so neither side is surprised by what changed where). Empty otherwise."""
        if self.private or self._app is None:
            return ""
        cur = self._working()
        act = safe(lambda: self._app.ActiveDocument)
        if cur is None or act is None or self.key(act) == self.key(cur):
            return ""
        return (f"worked on {safe(lambda: cur.DisplayName, '?')}; the document in front in Inventor is "
                f"{safe(lambda: act.DisplayName, '?')}")

    def front(self):
        """The working document, made Inventor's active document: for commands, selections and the active view,
        which always act on whatever is active. In the user's Inventor this switches documents only when needed."""
        cur = self._working()
        if cur is None:
            return self.doc()
        if self._raise(cur) and not self.private:
            self.switched_documents = getattr(self, "switched_documents", 0) + 1
        return cur

    def view(self):
        """The active view of the working document (brought to the front first)."""
        self.front()
        return self.app.ActiveView

    def kind(self, d=None) -> str:
        d = d or self.doc()
        t = d.DocumentType
        for name, kind in DOC_KIND.items():
            if K.get(name) == t:
                return kind
        return str(t)

    def part(self):
        d = self.doc()
        if self.kind(d) != "part":
            raise InventorError(f"active document is a {self.kind(d)}, this needs a part")
        return d

    def assembly(self):
        d = self.doc()
        if self.kind(d) != "assembly":
            raise InventorError(f"active document is a {self.kind(d)}, this needs an assembly")
        return d

    def drawing(self):
        d = self.doc()
        if self.kind(d) != "drawing":
            raise InventorError(f"active document is a {self.kind(d)}, this needs a drawing")
        return d

    def cdef(self):
        return self.part().ComponentDefinition

    def is_sheet_metal(self) -> bool:
        return safe(lambda: self.cdef().Type == K.kSheetMetalComponentDefinitionObject, False)

    @property
    def tg(self):
        return self.app.TransientGeometry

    @property
    def to(self):
        return self.app.TransientObjects

    def key(self, d=None) -> str:
        d = d or self.doc()
        return safe(lambda: d.InternalName, "") or safe(lambda: d.FullDocumentName, "") or str(id(d))

    # ------------------------------------------------------------------ geometry helpers
    def p2(self, x_mm: float, y_mm: float):
        return self.tg.CreatePoint2d(x_mm * MM, y_mm * MM)

    def p3(self, x_mm: float, y_mm: float, z_mm: float):
        return self.tg.CreatePoint(x_mm * MM, y_mm * MM, z_mm * MM)

    def collection(self, objs=()):
        c = self.to.CreateObjectCollection()
        for o in objs:
            c.Add(o)
        return c

    def edge_collection(self, edges):
        c = self.to.CreateEdgeCollection()
        for e in edges:
            c.Add(e)
        return c

    def face_collection(self, faces):
        c = self.to.CreateFaceCollection()
        for f in faces:
            c.Add(f)
        return c

    def body(self, index=1):
        """A solid body by 1-based index or by name (body names stay with the body as features are added; rename
        bodies to give them meaningful, stable names). "" or None is the first body."""
        bodies = self.cdef().SurfaceBodies
        if bodies.Count == 0:
            raise InventorError("the part has no solid body yet — sketch a profile and extrude/revolve it")
        if index in (None, "", 0, "0"):
            return bodies.Item(1)
        if isinstance(index, int) or str(index).strip().isdigit():
            i = int(index)
            if not 1 <= i <= bodies.Count:
                raise InventorError(f"body {i} out of range 1..{bodies.Count} (list_bodies)")
            return bodies.Item(i)
        want = str(index).strip().lower()
        for b in items(bodies):
            if safe(lambda b=b: b.Name, "").lower() == want:
                return b
        raise InventorError(f"no body {index!r}; bodies are {[safe(lambda b=b: b.Name, '?') for b in items(bodies)]}")

    def bodies_named(self, spec) -> list:
        """Bodies for a body= argument: one name or index, a list of them, or "all"."""
        if spec in (None, ""):
            return []
        if isinstance(spec, str) and spec.strip().lower() == "all":
            return list(items(self.cdef().SurfaceBodies))
        specs = spec if isinstance(spec, (list, tuple)) else [spec]
        return [self.body(x) for x in specs]

    def faces(self, body=None) -> list:
        return list(items(self.body(body).Faces))

    def edges(self, body=None) -> list:
        return list(items(self.body(body).Edges))

    def face(self, index: int, body=None):
        fs = self.body(body).Faces
        if not 1 <= int(index) <= fs.Count:
            raise InventorError(f"face {index} out of range 1..{fs.Count} (use list_faces / find_face)")
        return fs.Item(int(index))

    def edge(self, index: int, body=None):
        es = self.body(body).Edges
        if not 1 <= int(index) <= es.Count:
            raise InventorError(f"edge {index} out of range 1..{es.Count} (use list_edges / find_edge)")
        return es.Item(int(index))

    def work_plane(self, name: str):
        """'XY'/'XZ'/'YZ' (any case) or a work plane name -> WorkPlane."""
        planes = self.cdef().WorkPlanes
        n = name.strip().upper()
        idx = {"YZ": 1, "XZ": 2, "XY": 3}.get(n)
        if idx:
            return planes.Item(idx)
        for wp in items(planes):
            if wp.Name.lower() == name.strip().lower():
                return wp
        raise InventorError(f"no work plane {name!r} (use XY, XZ, YZ or a work plane name)")

    def work_axis(self, name: str):
        axes = self.cdef().WorkAxes
        idx = {"X": 1, "Y": 2, "Z": 3}.get(name.strip().upper())
        if idx:
            return axes.Item(idx)
        for wa in items(axes):
            if wa.Name.lower() == name.strip().lower():
                return wa
        raise InventorError(f"no work axis {name!r} (use X, Y, Z or a work axis name)")

    # ------------------------------------------------------------------ sketches
    def set_sketch(self, sk) -> None:
        self._sketch[self.key()] = sk

    def sketch(self, index: int = 0):
        """index 0 -> the sketch this session last created/selected, else the 1-based sketch."""
        sks = self.cdef().Sketches
        if index:
            if not 1 <= index <= sks.Count:
                raise InventorError(f"sketch {index} out of range 1..{sks.Count}")
            return sks.Item(index)
        sk = self._sketch.get(self.key())
        if sk is not None:
            try:
                _ = sk.Name
                return sk
            except Exception:
                pass
        if sks.Count == 0:
            raise InventorError("no sketch — call create_sketch first")
        return sks.Item(sks.Count)

    # ------------------------------------------------------------------ measuring
    def snapshot(self) -> Snapshot:
        try:
            cd = self.cdef()
        except InventorError:
            return Snapshot()
        s = Snapshot()
        s.bodies = safe(lambda: cd.SurfaceBodies.Count, 0)
        if s.bodies:
            s.volume = safe(lambda: self.part().ComponentDefinition.MassProperties.Volume * CM3_TO_MM3, 0.0)
            s.faces = sum(safe(lambda b=b: b.Faces.Count, 0) for b in items(cd.SurfaceBodies))
            s.edges = sum(safe(lambda b=b: b.Edges.Count, 0) for b in items(cd.SurfaceBodies))
            if s.bodies > 1:
                s.per_body = {safe(lambda b=b: b.Name, f"body {i}"): body_volume(b)
                              for i, b in enumerate(items(cd.SurfaceBodies), 1)}
        return s

    def delta(self, label: str, before: Snapshot) -> dict:
        after = self.snapshot()
        dv = after.volume - before.volume
        text = (f"{label} | V {after.volume:.0f} ({dv:+.0f} mm³) | F{after.faces} E{after.edges}"
                + (f" | bodies {after.bodies}" if after.bodies > 1 else ""))
        out = {"delta": text, "volume_mm3": round(after.volume, 3), "volume_change_mm3": round(dv, 3),
               "faces": after.faces, "edges": after.edges}
        quiet = ("thread", "work", "param", "unsuppress", "suppress", "abort", "undo")
        if abs(dv) < 1e-6 and not label.lower().startswith(quiet):
            out["warning"] = "volume did not change — check the feature did what you intended"
        if before.per_body is not None or after.per_body is not None:
            changes = body_changes(before.per_body or {}, after.per_body or {})
            if changes:
                out["bodies_changed"] = changes
        return out

    def area_mm2(self, face) -> float:
        return safe(lambda: face.Evaluator.Area * CM2_TO_MM2, 0.0)
