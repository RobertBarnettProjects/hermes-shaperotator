"""Connection, documents, views, screenshots, export, undo groups."""
from __future__ import annotations

import contextlib
import glob
import os
import tempfile
import threading
import time

from ..com import InventorError, K, items, late, plain_name, safe
from ..registry import tool
from ..session import Session

SHEET_METAL_SUBTYPE = "{9C464203-9BAE-11D3-8BAD-0060B0CE6BB4}"

VIEWS = {"iso": "kIsoTopRightViewOrientation", "iso_left": "kIsoTopLeftViewOrientation",
         "iso_bottom": "kIsoBottomRightViewOrientation", "front": "kFrontViewOrientation",
         "back": "kBackViewOrientation", "top": "kTopViewOrientation", "bottom": "kBottomViewOrientation",
         "left": "kLeftViewOrientation", "right": "kRightViewOrientation", "home": "kDefaultViewOrientation"}

EXPORT_EXT = {"STEP": ".stp", "STP": ".stp", "IGES": ".igs", "IGS": ".igs", "STL": ".stl", "SAT": ".sat",
              "PDF": ".pdf", "DWG": ".dwg", "DXF": ".dxf", "OBJ": ".obj", "JT": ".jt", "X_T": ".x_t",
              "PARASOLID": ".x_t", "3MF": ".3mf", "IPT": ".ipt", "IAM": ".iam", "IDW": ".idw", "PNG": ".png",
              "BMP": ".bmp", "JPG": ".jpg", "DWF": ".dwf"}


def _doc_info(s: Session, d) -> dict:
    info = {"name": safe(lambda: d.DisplayName, ""), "type": s.kind(d),
            "path": safe(lambda: d.FullFileName, "") or "(unsaved)",
            "dirty": safe(lambda: bool(d.Dirty), None)}
    if info["type"] == "part":
        cd = d.ComponentDefinition
        info["sheet_metal"] = safe(lambda: cd.Type == K.kSheetMetalComponentDefinitionObject, False)
        info["features"] = safe(lambda: cd.Features.Count, 0)
        info["sketches"] = safe(lambda: cd.Sketches.Count, 0)
        info["bodies"] = safe(lambda: cd.SurfaceBodies.Count, 0)
        if info["bodies"]:
            info["volume_mm3"] = round(safe(lambda: cd.MassProperties.Volume * 1000, 0.0), 2)
        info["material"] = safe(lambda: d.ActiveMaterial.DisplayName, "")
    elif info["type"] == "assembly":
        info["occurrences"] = safe(lambda: d.ComponentDefinition.Occurrences.Count, 0)
    elif info["type"] == "drawing":
        info["sheets"] = safe(lambda: d.Sheets.Count, 0)
        info["active_sheet"] = safe(lambda: d.ActiveSheet.Name, "")
    return info


@tool("session", mutates=False)
def connect(s: Session, private: bool = False) -> dict:
    """Attach to the running Inventor (or start it) and report the version, project and open documents.

    private: start a separate Inventor process just for this session, leaving any Inventor the user has open
      untouched (close it with close_private_inventor). Use it for long jobs and whenever the user is working.
    """
    from .. import uidrive as U
    cleaned = U.end_solvers(U.orphaned_solvers()) if private else 0      # left by an Inventor that crashed or was closed
    app = s.start_private() if private else s.app
    out = {"version": safe(lambda: app.SoftwareVersion.DisplayVersion, "?"),
           "product": safe(lambda: app.SoftwareVersion.ProductName, "Inventor"),
           "private": s.private, "process": s.pid, "orphaned_solvers_ended": cleaned,
           "project": safe(lambda: app.DesignProjectManager.ActiveDesignProject.FullFileName, ""),
           "documents": [_doc_info(s, d) for d in items(app.Documents) if safe(lambda d=d: d.Views.Count, 1)]}
    others = [p for p in U.pids_named("Inventor.exe") if p != s.pid]
    if others:
        out["other_inventors"] = others
        if not s.private:
            out["warning"] = ("more than one Inventor is running and this attached to the first one registered, which "
                              "may be the user's; connect(private=true) gives this session its own")
    return out


@tool("session")
def close_private_inventor(s: Session) -> dict:
    """Close the separate Inventor started by connect(private=true), discarding anything unsaved in it.
    Never touches an Inventor this session did not start."""
    if not s.private:
        raise InventorError("this session did not start a private Inventor, so there is nothing to close")
    from .. import uidrive as U
    app, pid = s.app, s.pid
    app.SilentOperation = True
    for d in list(items(app.Documents.VisibleDocuments)):
        safe(lambda d=d: d.Close(True))
    s._app, s.private, s.pid, s._current = None, False, None, None
    app.Quit()
    import time
    t0 = time.time()
    while pid in U.pids_named("Inventor.exe") and time.time() - t0 < 60:
        time.sleep(0.5)
    return {"closed": pid, "solvers_ended": U.end_orphaned_solvers(pid)}


@tool("session", mutates=False)
def list_projects(s: Session) -> dict:
    """Inventor projects known to this machine and which one is active."""
    m = s.app.DesignProjectManager
    active = safe(lambda: m.ActiveDesignProject.FullFileName, "")
    return {"active": active, "projects": [{"name": p.Name, "file": p.FullFileName, "workspace": safe(lambda p=p: p.WorkspacePath, ""),
                                            "active": p.FullFileName == active} for p in items(m.DesignProjects)]}


@tool("session")
def set_active_project(s: Session, project: str, other_inventors_ok: bool = False) -> dict:
    """Switch Inventor's active project (by name or .ipj path). Inventor only allows this with no documents open.
    The active project is a per-user setting shared by every Inventor on the PC, so this refuses while another
    Inventor is running (its open files would start resolving against the new project).

    project: project name or full path to the .ipj file
    other_inventors_ok: switch anyway with other Inventors running, after the user has agreed
    """
    from .. import uidrive as U
    app = s.app
    if safe(lambda: app.Documents.VisibleDocuments.Count, 0):
        raise InventorError("Inventor can only change project with no documents open; close them first")
    others = [p for p in U.pids_named("Inventor.exe") if p != s.pid]
    if others and not other_inventors_ok:
        raise InventorError(f"another Inventor is running (process {others}); the active project is shared with it, so "
                            "changing it here changes it there too. Close it, or ask the user and pass other_inventors_ok=true")
    m = app.DesignProjectManager
    p = safe(lambda: m.DesignProjects.ItemByName(project))
    if p is None and os.path.exists(project):
        p = m.DesignProjects.AddExisting(os.path.abspath(project))
    if p is None:
        raise InventorError(f"no project {project!r} (see list_projects)")
    p.Activate(True)
    return {"active": m.ActiveDesignProject.FullFileName}


@tool("session", mutates=False)
def status(s: Session) -> dict:
    """Summary of the document this session works on (type, path, feature/sketch/body counts, volume, material):
    the working document, else Inventor's active one. user_active names the document the user has in front when that
    is a different one."""
    app = s.app
    work = s._working()
    active = late(app.ActiveDocument)
    d = work if work is not None else active
    out = {"connected": True, "version": safe(lambda: app.SoftwareVersion.DisplayVersion, "?"),
           "open_documents": safe(lambda: app.Documents.VisibleDocuments.Count, 0)}
    if d is None:
        out["active"] = None
    else:
        out["active"] = _doc_info(s, d)
        if work is not None and active is not None and s.key(active) != s.key(work):
            out["user_active"] = safe(lambda: active.DisplayName, "")
    return out


@tool("session", mutates=False)
def list_documents(s: Session) -> dict:
    """Every open (visible) document, with the active one marked."""
    active = s.app.ActiveDocument
    akey = s.key(active) if active is not None else None
    docs = []
    for d in items(s.app.Documents.VisibleDocuments):
        info = _doc_info(s, d)
        info["active"] = s.key(d) == akey
        docs.append(info)
    return {"documents": docs}


@tool("documents")
def create_part(s: Session, name: str = "", sheet_metal: bool = False) -> dict:
    """New metric part document (optionally a sheet-metal part) and make it active.

    name: display name (used as the file name when saved without a path)
    sheet_metal: create a sheet-metal part (enables sheet_metal_face / flange / cut / flat_pattern)
    """
    app = s.app
    sub = SHEET_METAL_SUBTYPE if sheet_metal else None
    tpl = s.stock_template("Sheet Metal (mm).ipt" if sheet_metal else "Standard (mm).ipt")
    try:
        tpl = tpl or app.FileManager.GetTemplateFile(K.kPartDocumentObject, K.kMetricSystemOfMeasure,
                                              K.kDefault_DraftingStandard, sub) if sub else \
            app.FileManager.GetTemplateFile(K.kPartDocumentObject, K.kMetricSystemOfMeasure)
    except Exception:
        tpl = ""
    d = app.Documents.Add(K.kPartDocumentObject, tpl, True)
    if sheet_metal and safe(lambda: d.SubType, "") != SHEET_METAL_SUBTYPE:
        d.SubType = SHEET_METAL_SUBTYPE
    if name:
        safe(lambda: setattr(d, "DisplayName", name))
    d.Activate()
    s.set_current(d)
    return {"created": _doc_info(s, d), "units": "mm (Inventor stores cm internally; all tools take mm)"}


@tool("documents")
def create_assembly(s: Session, name: str = "") -> dict:
    """New metric assembly document and make it active."""
    app = s.app
    try:
        tpl = s.stock_template("Standard (mm).iam") or app.FileManager.GetTemplateFile(K.kAssemblyDocumentObject, K.kMetricSystemOfMeasure)
    except Exception:
        tpl = ""
    d = app.Documents.Add(K.kAssemblyDocumentObject, tpl, True)
    if name:
        safe(lambda: setattr(d, "DisplayName", name))
    d.Activate()
    s.set_current(d)
    return {"created": _doc_info(s, d)}


def _outside_project(s: Session, path: str) -> bool:
    ws = safe(lambda: s.app.DesignProjectManager.ActiveDesignProject.WorkspacePath, "") or ""
    if not ws:
        return False
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(ws))
    return not (a == b or a.startswith(b.rstrip("\\/") + os.sep))


@tool("documents")
def open_document(s: Session, path: str) -> dict:
    """Open an Inventor file (.ipt .iam .idw .dwg) or import STEP/IGES/SAT, and activate it. Opens in Inventor's
    silent mode, so a file outside the active project, or with missing references, can't stop at a prompt."""
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(path):
        raise InventorError(f"file not found: {path}")
    with _silent(s), _dialog_watchdog(s) as dog:
        d = s.app.Documents.Open(path, True)
    d.Activate()
    s.set_current(d)
    out = {"opened": _doc_info(s, d)}
    if _outside_project(s, path):
        out["note"] = ("the file is outside the active project's workspace; references are resolved from its own "
                       "folder (list_projects / set_active_project to change project)")
    if dog.cancelled:
        out["dialogs_cancelled"] = dog.cancelled
    missing = safe(lambda: [os.path.basename(x.FullFileName) for x in items(d.File.ReferencedFileDescriptors)
                            if safe(lambda x=x: bool(x.ReferenceMissing), False)], [])
    if missing:
        out["missing_references"] = missing[:20]
    return out


def _find_doc(s: Session, name: str):
    for d in items(s.app.Documents.VisibleDocuments):
        names = {safe(lambda: d.DisplayName, ""), os.path.basename(safe(lambda: d.FullFileName, "") or ""),
                 safe(lambda: d.FullFileName, "")}
        if name in names or any(n and n.lower().startswith(name.lower()) for n in names):
            return d
    raise InventorError(f"no open document matches {name!r} (see list_documents)")


@tool("documents")
def activate_document(s: Session, name: str) -> dict:
    """Bring an open document to the front by display name, file name or path."""
    d = _find_doc(s, name)
    d.Activate()
    s.set_current(d)
    return {"active": _doc_info(s, d)}


def _release_path(s: Session, d, path: str) -> None:
    """Saving over a file Inventor still holds (e.g. loaded in the background as a reference of a
    closed drawing) fails; close such a copy when nothing visible uses it, else explain."""
    target = os.path.normcase(os.path.abspath(path))
    for other in list(items(s.app.Documents)):
        if s.key(other) == s.key(d):
            continue
        if os.path.normcase(safe(lambda o=other: o.FullFileName, "") or "") != target:
            continue
        visible = safe(lambda o=other: o.Views.Count, 0) > 0
        refs = safe(lambda o=other: o.ReferencingDocuments.Count, 0)
        if not visible and not refs:
            safe(lambda o=other: o.ReleaseReference())
            safe(lambda o=other: o.Close(True))
            still = any(os.path.normcase(safe(lambda x=x: x.FullFileName, "") or "") == target and s.key(x) != s.key(d)
                        for x in items(s.app.Documents))
            if still:
                raise InventorError(f"{path} is still loaded in Inventor (held by another document or the "
                                    "Inventor cache); close the documents using it, or save under a different name")
        else:
            raise InventorError(f"{path} is open in Inventor ({'visible' if visible else 'referenced by another document'}); "
                                "close it first or save under a different name")


@tool("documents")
def save_document(s: Session, path: str = "") -> dict:
    """Save the active document; with a path, Save As (folders are created). Relative paths go
    under ~/Documents/inventor-forge."""
    d = s.doc()
    if path:
        if not os.path.isabs(path):
            path = os.path.join(s.workdir, path)
        ext = {"part": ".ipt", "assembly": ".iam", "drawing": ".idw"}.get(s.kind(d), "")
        if ext and not os.path.splitext(path)[1]:
            path += ext
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _release_path(s, d, path)
        try:
            d.SaveAs(path, False)
        except Exception:
            # drawings started from a DWG template can only be saved as Inventor DWG
            if s.kind(d) == "drawing" and path.lower().endswith(".idw"):
                path = path[:-4] + ".dwg"
                d.SaveAs(path, False)
                return {"saved": safe(lambda: d.FullFileName, path),
                        "note": "this drawing uses a DWG template, so it was saved as Inventor .dwg"}
            raise
    else:
        if not safe(lambda: d.FullFileName, ""):
            name = plain_name(safe(lambda: d.DisplayName, "Untitled").split(".")[0], "Untitled")
            return save_document(s, os.path.join(s.workdir, name))
        d.Save()
    return {"saved": safe(lambda: d.FullFileName, path)}


@tool("documents")
def close_document(s: Session, save: bool = False, name: str = "") -> dict:
    """Close the active (or named) document. Unsaved changes are discarded unless save=true."""
    d = _find_doc(s, name) if name else s.doc()
    label = safe(lambda: d.DisplayName, "")
    if save:
        active = s.app.ActiveDocument
        save_document(s) if active is not None and s.key(d) == s.key(active) else d.Save()
    if getattr(s, "_current", None) is not None and s.key(d) == getattr(s, "_current_key", None):
        s.set_current(None)
    d.Close(not save)          # SkipSave
    return {"closed": label, "saved": save}


@contextlib.contextmanager
def _silent(s: Session):
    """Inventor's silent mode: translators and prompts take their defaults instead of opening a dialog
    that would wait forever for a person."""
    app = s.app
    was = safe(lambda: app.SilentOperation, False)
    app.SilentOperation = True
    try:
        yield
    finally:
        safe(lambda: setattr(app, "SilentOperation", was))


class _Watchdog:
    def __init__(self):
        self.cancelled: list[str] = []
        self.stop = threading.Event()


@contextlib.contextmanager
def _dialog_watchdog(s: Session, grace: float = 4.0):
    """While a translator runs, cancel any new modal dialog from Inventor after a few seconds, so a
    prompt can never block an unattended session. Uses window messages only, no mouse or keyboard."""
    import win32gui
    import win32process

    from .. import uidrive as U
    pid = win32process.GetWindowThreadProcessId(s.app.MainFrameHWND)[1]
    existing = set(U.process_windows(pid))
    dog = _Watchdog()

    def watch():
        seen: dict[int, float] = {}
        while not dog.stop.wait(0.5):
            for h in U.process_windows(pid):
                try:
                    if h in existing or win32gui.GetClassName(h) != "#32770":
                        continue
                    first = seen.setdefault(h, time.time())
                    if time.time() - first >= grace:
                        dog.cancelled.append(win32gui.GetWindowText(h) or "dialog")
                        U.click(U.find(h, cid=2))
                        seen.pop(h, None)
                        existing.add(h)
                except Exception:
                    continue
    th = threading.Thread(target=watch, daemon=True)
    th.start()
    try:
        yield dog
    finally:
        dog.stop.set()
        th.join(2)


# the export settings the DWG/DXF translator asks for in its dialog when it has no configuration file
DWG_EXPORT_INI = """[EXPORT SELECT OPTIONS]
AUTOCAD VERSION=AutoCAD 2018
CREATE AUTOCAD MECHANICAL=No
USE TRANSMITTAL=No
USE CUSTOMIZE=No
CUSTOMIZE FILE=
CUSTOMIZE LAYER FILE=
[EXPORT PROPERTIES]
SELECTED PROPERTIES=
[EXPORT DESTINATION]
SPACE=Model
SCALING=Geometry
ALL SHEETS=Yes
MAPPING=MapsBest
MODEL GEOMETRY ONLY=No
EXPLODE DIMENSIONS=No
SYMBOLS ARE BLOCKED=Yes
AUTO-REDRAW=No
[EXPORT LINE TYPE & LINE SCALE]
LINE TYPE FILE=
CONTINUOUS THROUGH GAPS=Yes
LINE SCALE=1.0
"""


DWG_TRANSLATORS = {"DWG": "{C24E3AC2-122E-11D5-8E91-0010B541CD80}", "DXF": "{C24E3AC4-122E-11D5-8E91-0010B541CD80}"}


def _translate_drawing(s: Session, d, path: str, fmt: str) -> None:
    """A drawing as AutoCAD DWG / DXF through the translator add-in."""
    app = s.app
    tr = app.ApplicationAddIns.ItemById(DWG_TRANSLATORS[fmt])
    if not tr.Activated:
        tr.Activate()
    ctx = app.TransientObjects.CreateTranslationContext()
    ctx.Type = K.kFileBrowseIOMechanism
    opts = app.TransientObjects.CreateNameValueMap()
    medium = app.TransientObjects.CreateDataMedium()
    medium.FileName = path
    ini = os.path.join(tempfile.gettempdir(), "inventor_forge_dwg_export.ini")
    with open(ini, "w", encoding="utf-8") as f:
        f.write(DWG_EXPORT_INI)
    opts.Add("Export_Acad_IniFile", ini)
    with _silent(s), _dialog_watchdog(s) as dog:
        try:
            tr.SaveCopyAs(d, ctx, opts, medium)
        except Exception:
            if not dog.cancelled:
                raise
    if dog.cancelled:
        raise InventorError(f"Inventor opened an options dialog for {fmt} export instead of writing the file "
                            f"({dog.cancelled}); it was cancelled so the session doesn't hang")


@tool("documents")
def export_document(s: Session, path: str, format: str = "") -> dict:
    """Export a copy of the active document: STEP, IGES, STL, SAT, Parasolid (X_T), OBJ, JT, 3MF,
    PDF/DWG/DXF (drawings), PNG/JPG (picture of the view). Format comes from the extension if omitted.
    """
    d = s.doc()
    fmt = (format or os.path.splitext(path)[1].lstrip(".")).upper()
    ext = EXPORT_EXT.get(fmt)
    if not ext:
        raise InventorError(f"unknown export format {fmt!r}; use one of {sorted(set(EXPORT_EXT))}")
    if not os.path.isabs(path):
        path = os.path.join(s.workdir, "exports", path)
    if not os.path.splitext(path)[1]:
        path += ext
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    if fmt in ("PNG", "BMP", "JPG"):
        return screenshot(s, path=path)
    if fmt == "PDF" and s.kind(d) == "drawing":
        from .drawing import export_pdf
        return export_pdf(s, path=path)
    if fmt in ("DWG", "DXF") and s.kind(d) == "drawing":
        _translate_drawing(s, d, path, fmt)
    else:
        with _silent(s):
            d.SaveAs(path, True)    # SaveCopyAs: the translator is chosen from the extension
    t0 = time.time()
    while not os.path.exists(path) and time.time() - t0 < 30:
        time.sleep(0.2)
    if not os.path.exists(path):
        stem, ext_ = os.path.splitext(path)
        parts = sorted(glob.glob(glob.escape(stem) + "*" + ext_))     # one file per sheet
        if parts:
            return {"paths": parts, "path": parts[0], "format": fmt, "bytes": sum(os.path.getsize(x) for x in parts)}
        raise InventorError(f"Inventor did not write {path} (is the {fmt} translator available?)")
    return {"path": path, "format": fmt, "bytes": os.path.getsize(path)}


@tool("view")
def set_view(s: Session, view: str = "iso", fit: bool = True) -> dict:
    """Orient the active 3D view: iso, iso_left, iso_bottom, front, back, top, bottom, left, right, home."""
    v = s.view()
    if v is None:
        raise InventorError("no active view")
    cam = v.Camera
    key = VIEWS.get(view.lower())
    if not key:
        raise InventorError(f"view must be one of {sorted(VIEWS)}")
    cam.ViewOrientationType = getattr(K, key)
    if fit:
        cam.Fit()
    cam.ApplyWithoutTransition()
    return {"view": view}


@tool("view", mutates=False)
def screenshot(s: Session, path: str = "", view: str = "", width: int = 1600, height: int = 1000) -> dict:
    """Save a PNG of the active document's view (model or drawing sheet) — look at it to check your work.

    view: optional orientation first (iso, front, top, ...); empty keeps the current camera
    """
    if view:
        set_view(s, view=view)
    v = s.view()
    if v is None:
        raise InventorError("no active view to capture")
    if not path:
        path = os.path.join(s.workdir, "renders", f"view-{int(time.time() * 1000)}.png")
    elif not os.path.isabs(path):
        path = os.path.join(s.workdir, "renders", path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        white = s.to.CreateColor(255, 255, 255)
        v.Camera.SaveAsBitmap(path, int(width), int(height), white, white)
    except Exception:
        v.SaveAsBitmap(path, int(width), int(height))
    return {"path": path, "width": width, "height": height}


@tool("session")
def transaction(s: Session, action: str = "begin", name: str = "forge") -> dict:
    """Group several operations into one undo step: begin, commit, or abort (rolls everything back)."""
    tm = s.app.TransactionManager
    if action == "begin":
        if s._txn is not None:
            raise InventorError("a transaction is already open — commit or abort it first")
        s._txn = tm.StartTransaction(s.doc(), name)
        return {"transaction": "open", "name": name}
    if s._txn is None:
        raise InventorError("no open transaction")
    t, s._txn = s._txn, None
    if action == "commit":
        t.End()
        return {"transaction": "committed"}
    if action == "abort":
        before = s.snapshot()
        t.Abort()
        s.doc().Update()
        return {"transaction": "aborted", **s.delta("Abort", before)}
    raise InventorError("action must be begin, commit or abort")


@tool("session")
def undo(s: Session, steps: int = 1) -> dict:
    """Undo the last N operations in the working document."""
    before = s.snapshot()
    s.front()                          # TransactionManager undoes in whatever document is active: make it ours
    tm = s.app.TransactionManager
    done = 0
    for _ in range(max(1, int(steps))):
        if not safe(lambda: tm.CanUndo, True):
            break
        tm.UndoTransaction()
        done += 1
    return {"undone": done, **s.delta("Undo", before)}


@tool("documents")
def close_unused_documents(s: Session) -> dict:
    """Close documents Inventor holds in the background (no window, nothing referencing them) — e.g.
    parts loaded for a drawing that was closed. Frees their files for save_document. Never touches
    documents with a window."""
    closed = []
    for _ in range(5):
        changed = False
        for d in list(items(s.app.Documents)):
            if safe(lambda d=d: d.Views.Count, 1) == 0 and safe(lambda d=d: d.ReferencingDocuments.Count, 1) == 0:
                name = safe(lambda d=d: d.DisplayName, "?")
                key = s.key(d)
                safe(lambda d=d: d.ReleaseReference())
                safe(lambda d=d: d.Close(True))
                if not any(s.key(x) == key for x in items(s.app.Documents)):
                    closed.append(name)
                    changed = True
        if not changed:
            break
    return {"closed": closed, "open_documents": s.app.Documents.Count}


@tool("session", mutates=False)
def model_version(s: Session) -> dict:
    """A cheap fingerprint of the active document's geometry, for noticing edits made by hand in Inventor (it changes
    whenever the model's shape changes)."""
    d = s._working()
    d = d if d is not None else late(s.app.ActiveDocument)
    if d is None:
        return {"document": None, "version": ""}
    ver = safe(lambda: d.ModelGeometryVersion, "") or ""
    if not ver:
        cd = safe(lambda: d.ComponentDefinition)
        rb = safe(lambda: cd.RangeBox)
        ver = f"{safe(lambda: cd.Features.Count, 0)}:{safe(lambda: rb.MaxPoint.X, 0)}:{safe(lambda: rb.MaxPoint.Y, 0)}"
    return {"document": safe(lambda: d.FullFileName, "") or safe(lambda: d.DisplayName, ""), "type": s.kind(d),
            "version": str(ver), "dirty": bool(safe(lambda: d.Dirty, False))}


@tool("documents", mutates=False)
def referenced_files(s: Session) -> dict:
    """The active document's file and every file it depends on (parts of an assembly, the model of a drawing)."""
    d = s.doc()
    files = [safe(lambda: d.FullFileName, "")]
    for r_ in items(safe(lambda: d.AllReferencedDocuments) or []):
        files.append(safe(lambda r_=r_: r_.FullFileName, ""))
    return {"files": [f for f in dict.fromkeys(files) if f]}
