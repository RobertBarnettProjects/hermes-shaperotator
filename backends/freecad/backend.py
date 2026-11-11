"""FreeCAD backend: forwards ops to a headless FreeCAD worker process."""
from __future__ import annotations

from typing import Any

from ...core.backend import BackendError, CadBackend
from .client import FreeCADClient, WorkerError, find_freecad_python


class FreeCADBackend(CadBackend):
    name = "freecad"
    family = "freecad"
    supported = frozenset({
        "connect", "status", "new_part", "open", "save", "close", "export",
        "sketch", "rect", "circle", "line", "polygon", "profile",
        "extrude", "revolve", "hole", "fillet", "chamfer", "pattern_circular", "delete_feature",
        "get_parameters", "set_parameter", "add_parameter",
        "inspect", "list_edges", "list_faces", "find_edge", "find_face", "render",
        "transaction", "python", "fea", "shape_faces", "viewer_mesh",
        "drawing_new", "drawing_part_sheet", "drawing_check", "drawing_render", "drawing_export_pdf",
        "drawing_set_properties",
    })

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self._client: FreeCADClient | None = None

    def python_path(self) -> str | None:
        return find_freecad_python(self.settings.get("freecad_path", "") or "")

    def available(self) -> tuple[bool, str]:
        py = self.python_path()
        if py:
            return True, f"FreeCAD Python at {py}"
        return False, ("FreeCAD not found — install FreeCAD 1.0+ or set "
                       "plugins.entries.hermes-shaperotator.settings.freecad_path")

    def _call(self, op: str, **kwargs: Any) -> dict:
        if self._client is None:
            py = self.python_path()
            if not py:
                raise BackendError(self.available()[1])
            self._client = FreeCADClient(py, timeout=900.0)  # FEA with a convergence re-solve can be slow
        try:
            return self._client.request(op, **kwargs)
        except WorkerError as e:
            raise BackendError(str(e)) from e

    def shutdown(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
