"""Connection to a running (or newly started) Inventor and helpers shared by every tool."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .com import CM2_TO_MM2, CM3_TO_MM3, MM, InventorError, K, items, late, safe

# DocumentTypeEnum values are looked up lazily through K, these are just names
DOC_KIND = {"kPartDocumentObject": "part", "kAssemblyDocumentObject": "assembly",
            "kDrawingDocumentObject": "drawing", "kPresentationDocumentObject": "presentation"}


@dataclass
class Snapshot:
    volume: float = 0.0
    faces: int = 0
    edges: int = 0
    bodies: int = 0


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
        try:
            raw = wc.GetActiveObject("Inventor.Application")
        except Exception:
            raw = wc.DispatchEx("Inventor.Application")
        app = late(raw)          # always late-bound, method results included: predictable across machines
        try:
            if self.visible and not app.Visible:
                app.Visible = True
        except Exception:
            pass
        self.pid = self._process_id(app)
        return app

    @staticmethod
    def _process_id(app) -> int | None:
        import win32process
        return safe(lambda: win32process.GetWindowThreadProcessId(app.MainFrameHWND)[1])

    def start_private(self):
        """Start a separate Inventor process that belongs to this session, so a crash or a stray command
        can't touch what the user has open. Stops if no new process appeared."""
        import time

        import win32com.client as wc

        from .uidrive import pids_named
        if self.private and self._app is not None:
            return self.app
        before = set(pids_named("Inventor.exe"))
        raw = wc.DispatchEx("Inventor.Application")
        t0 = time.time()
        while not set(pids_named("Inventor.exe")) - before and time.time() - t0 < 120:
            time.sleep(0.5)
        new = set(pids_named("Inventor.exe")) - before
        app = late(raw)
        pid = self._process_id(app) if new else None
        if pid not in new:
            raise InventorError("could not start a separate Inventor (no new process appeared); nothing was touched")
        app.Visible = True
        self._app, self.pid, self.private = app, pid, True
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

    def body(self, index: int = 1):
        bodies = self.cdef().SurfaceBodies
        if bodies.Count == 0:
            raise InventorError("the part has no solid body yet — sketch a profile and extrude/revolve it")
        if not 1 <= index <= bodies.Count:
            raise InventorError(f"body {index} out of range 1..{bodies.Count}")
        return bodies.Item(index)

    def faces(self) -> list:
        return list(items(self.body().Faces))

    def edges(self) -> list:
        return list(items(self.body().Edges))

    def face(self, index: int):
        fs = self.body().Faces
        if not 1 <= int(index) <= fs.Count:
            raise InventorError(f"face {index} out of range 1..{fs.Count} (use list_faces / find_face)")
        return fs.Item(int(index))

    def edge(self, index: int):
        es = self.body().Edges
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
        return out

    def area_mm2(self, face) -> float:
        return safe(lambda: face.Evaluator.Area * CM2_TO_MM2, 0.0)
