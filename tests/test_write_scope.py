"""The write scope sent with each CAD server call: job folder always, outside files only for the call whose
approval named them, and a server's refusal turned into the next prompt for the same call."""
import json

import pytest

from conftest import load_plugin

plugin = load_plugin()
from hermes_shaperotator.core import paths, writescope  # noqa: E402
from hermes_shaperotator.core.state import STATE  # noqa: E402


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(STATE, "job_dir", tmp_path / "job")
    (tmp_path / "job").mkdir()
    return tmp_path / "job"


def test_default_scope_is_the_job_folder(job):
    assert writescope.current() == {"dirs": [str(job.resolve())], "files": []}


def test_refusal_becomes_the_next_prompt_and_then_the_scope(job, tmp_path):
    outside = str(tmp_path / "lib" / "a.ipt")
    seen = []
    refusal = {"ok": False, "error": "Error: won't save ... Nothing was written.\n" + writescope.NEEDS_MARK + " "
               + json.dumps([outside])}

    def handler(args, **kw):
        seen.append(writescope.current())
        return json.dumps(refusal) if len(seen) == 1 else json.dumps({"ok": True})

    run = writescope.scoped("cad_native", handler)
    args = {"tool": "save_document", "args": {"path": str(job / "asm.iam")}}
    plugin._pre_tool_call(tool_name="cad_native", args=args)
    run(args)
    assert seen[0]["files"] == []                                   # nothing outside approved the first time
    d = plugin._pre_tool_call(tool_name="cad_native", args=args)
    assert d and d["action"] == "approve" and outside in d["message"]
    run(args)
    assert seen[1]["files"] == [outside]                            # approved: allowed for this call only
    run(args)
    assert seen[2]["files"] == []                                   # and not for the next one


def test_refusal_inside_nested_json_is_found():
    path = r"C:\lib\b.ipt"
    inner = json.dumps({"error": "x\n" + writescope.NEEDS_MARK + " " + json.dumps([path])})
    assert writescope.parse_needed(json.dumps({"result": inner})) == [path]


def test_outside_files_resolves_what_the_call_names(job, tmp_path):
    out = paths.outside_files("cad_document", {"action": "export", "path": str(tmp_path / "x.step")})
    assert out == [str((tmp_path / "x.step").resolve())]
    assert paths.outside_files("cad_document", {"action": "export", "path": str(job / "y.step")}) == []
