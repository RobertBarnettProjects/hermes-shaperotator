"""Reference guides: short, rule-shaped notes on how Inventor behaves, for the model using the tools. Read with the
guide tool or as MCP resources (inventor-forge://guide/<topic>)."""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
SUMMARY = {
    "session": "working alongside the user, failures and hangs, COM binding, starting Inventor, unattended runs",
    "modelling": "checking features, multi-body targets, sketches, derived parts, model states, split faces, welds",
    "assemblies": "placing and checking, fabrication before analysis, copying assemblies, saving",
    "stress-analysis": "contacts and meshes, running solves safely, reading peaks and reactions, disk use",
    "drawings": "building sheets, company templates and prompts, annotation, rebuilding edited drawings",
    "printing": "multi-body print parts, clearances, batches, print package export and mesh checks",
}


def topics() -> dict:
    return dict(SUMMARY)


def read(topic: str) -> str:
    """One guide's text. Raises KeyError for an unknown topic."""
    key = topic.strip().lower().replace("_", "-").replace(" ", "-")
    key = {"fea": "stress-analysis", "analysis": "stress-analysis", "stress": "stress-analysis",
           "modeling": "modelling", "assembly": "assemblies", "drawing": "drawings", "print": "printing",
           "3d-print": "printing", "com": "session", "unattended": "session"}.get(key, key)
    if key not in SUMMARY:
        raise KeyError(topic)
    with open(os.path.join(HERE, key + ".md"), encoding="utf-8") as f:
        return f.read()
