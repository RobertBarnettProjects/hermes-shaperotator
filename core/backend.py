"""Backend-neutral CAD contract.

A backend is anything that can execute the operations in ``OPS``. Tools translate
LLM-facing arguments into ``backend.call(op, **kwargs)``; backends return a dict
(always JSON-serialisable). Geometry-changing ops include a ``delta`` string such as
``"Pad | V 23497 (-503 mm³) | F7 E14"`` so the model can verify every step.

All lengths are millimetres, angles degrees, forces newtons.
"""
from __future__ import annotations

import abc
import threading
from typing import Any

# op name -> short description (used for docs, `cad_status`, and capability listing)
OPS: dict[str, str] = {
    # session / documents
    "connect": "Start or attach to the CAD application",
    "status": "Connection + active document summary",
    "new_part": "New empty part (metric)",
    "new_assembly": "New empty assembly (metric)",
    "open": "Open a model file",
    "save": "Save the active document (optionally to a path)",
    "close": "Close the active document",
    "export": "Export the active document (STEP, STL, 3MF, IGES, PDF ...)",
    # sketching
    "sketch": "New sketch on a base plane (XY/XZ/YZ, optional offset) or a face",
    "rect": "Centred rectangle",
    "circle": "Circle (diameter dimension added)",
    "line": "Line (optionally construction)",
    "polygon": "Regular polygon (circumscribed radius)",
    "profile": "Closed polyline profile from points",
    # features
    "extrude": "Extrude the latest/selected sketch (join/cut/new; distance or through-all)",
    "revolve": "Revolve a sketch profile about X/Y/Z (join/cut)",
    "hole": "Drilled or tapped hole at (x, y) on the XY plane",
    "fillet": "Fillet edges by index",
    "chamfer": "Chamfer edges by index",
    "pattern_circular": "Circular pattern of a feature",
    "delete_feature": "Delete features by index",
    # parameters
    "get_parameters": "List parameters",
    "set_parameter": "Set a parameter expression",
    "add_parameter": "Add a user parameter",
    # inspection
    "inspect": "Volume, bbox, face/edge counts, feature tree",
    "list_edges": "Edge table (index, length, midpoint)",
    "list_faces": "Face table (index, type, area, centroid)",
    "find_edge": "Edge index by midpoint coordinates",
    "find_face": "Face index by centroid coordinates",
    "render": "Save a PNG preview of the active model or sheet",
    # transactions
    "transaction": "begin / commit / abort an undo group",
    # drawings
    "drawing_new": "New drawing document (AS1100 or ISO)",
    "drawing_part_sheet": "One-shot detail sheet for a part",
    "drawing_assembly_sheet": "One-shot GA sheet for an assembly",
    "drawing_check": "Drawing QA report",
    "drawing_export_pdf": "Export drawing to PDF",
    "drawing_render": "Render the drawing sheet to PNG",
    "drawing_set_properties": "Set title block fields",
    # analysis
    "fea": "Linear-static FEA (FreeCAD FEM + CalculiX)",
    "shape_faces": "Face table of the active body or a STEP file",
    "viewer_mesh": "Pickable triangle mesh for the Desktop pane",
    # escape hatches
    "python": "Run Python inside the CAD application",
    "native": "Call a backend-native tool by name (Inventor: every inventor-forge tool)",
}


class BackendError(RuntimeError):
    """Raised by backends for user-facing failures (message goes back to the model)."""


class CadBackend(abc.ABC):
    name: str = "abstract"
    #: what kind of CAD program is behind it ("freecad", "inventor", or "generic" for an unknown MCP
    #: server). Tools branch on the family, never on the backend's name, so any transport works.
    family: str = "generic"
    #: ops this backend implements; anything else reports "not supported"
    supported: frozenset[str] = frozenset()

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------
    @abc.abstractmethod
    def available(self) -> tuple[bool, str]:
        """(is the CAD application installed/usable here, human-readable reason)."""

    @abc.abstractmethod
    def _call(self, op: str, **kwargs: Any) -> dict:
        """Execute one op. May raise BackendError."""

    def shutdown(self) -> None:  # pragma: no cover - optional
        pass

    # -- public entry ----------------------------------------------------
    def call(self, op: str, **kwargs: Any) -> dict:
        if op not in OPS:
            raise BackendError(f"unknown op {op!r}")
        if op not in self.supported:
            raise BackendError(f"{op!r} is not supported by the {self.name} backend yet")
        with self._lock:
            return self._call(op, **kwargs)
