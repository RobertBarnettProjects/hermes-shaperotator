"""Evidence log: every analysis result the agent could cite, with a stable id (FEA-001 ...).

The claims checker compares numbers in the agent's replies against this log. It's appended
to <job folder>/evidence.jsonl so the trail survives the session.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Record:
    id: str
    kind: str                     # "fea" | "inspect"
    summary: str
    values: dict[str, float]      # quantity -> value (MPa, mm, kg, mm3, -)
    verified: bool
    warnings: list[str] = field(default_factory=list)
    at: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)


class EvidenceLog:
    def __init__(self):
        self._items: list[Record] = []
        self._n: dict[str, int] = {}
        self._lock = threading.Lock()

    def add(self, kind: str, summary: str, values: dict[str, float], verified: bool,
            warnings: list[str] | None = None, data: dict | None = None, path=None) -> Record:
        with self._lock:
            self._n[kind] = self._n.get(kind, 0) + 1
            rec = Record(f"{kind.upper()}-{self._n[kind]:03d}", kind, summary,
                         {k: float(v) for k, v in values.items() if v is not None}, verified,
                         list(warnings or []), data=data or {})
            self._items.append(rec)
        if path is not None:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({**rec.__dict__}, default=str) + "\n")
            except OSError:
                pass
        return rec

    def items(self) -> list[Record]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._n.clear()


EVIDENCE = EvidenceLog()
