"""The three items from the plugin-catalog review of 635c8720: confined write paths, a minimal child environment,
and a safety-factor pattern that can't backtrack badly."""
import os
import sys
import time

import pytest

from conftest import load_plugin

P = load_plugin()
from hermes_shaperotator.core import paths, verify  # noqa: E402
from hermes_shaperotator.core.state import STATE  # noqa: E402


@pytest.fixture()
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(STATE, "job_dir", tmp_path / "jobs" / "20261112-000000")
    STATE.job_dir.mkdir(parents=True)
    return STATE.job_dir


# --------------------------------------------------------------------------- 1. write paths
def test_relative_paths_land_in_the_job_folder(job):
    assert paths.resolve("out/model.step") == (job / "out" / "model.step").resolve()


def test_no_tilde_or_variable_expansion(job, monkeypatch):
    monkeypatch.setenv("SECRET_DIR", str(job.parent.parent))
    assert paths.resolve("~/x.step") == (job / "~" / "x.step").resolve()
    assert paths.resolve("$SECRET_DIR/x.step") == (job / "$SECRET_DIR" / "x.step").resolve()


def test_dot_dot_is_refused(job):
    with pytest.raises(paths.PathError):
        paths.resolve("../../.bashrc")
    with pytest.raises(paths.PathError):
        paths.resolve(str(job / ".." / ".." / "x"))


def test_write_outside_the_job_folder_needs_approval(job, tmp_path, monkeypatch):
    monkeypatch.setattr(STATE.qa, "last_issues", 0)        # a clean drawing, so the QA gate lets the export through
    monkeypatch.setattr(STATE.qa, "dirty", False)
    target = str(tmp_path / "elsewhere" / "authorized_keys")
    for name, args in (("cad_document", {"action": "export", "path": target}),
                       ("cad_drawing", {"action": "export_pdf", "path": target}),
                       ("cad_native", {"tool": "tube_frame", "args": {"csv_path": target}}),
                       ("cad_native", {"tool": "export_document", "args": {"path": target}})):
        out = P._pre_tool_call(tool_name=name, args=args)
        assert out and out["action"] == "approve" and "authorized_keys" in out["message"], name


def test_writes_inside_the_job_folder_and_reads_anywhere_need_no_prompt(job, tmp_path):
    assert P._pre_tool_call(tool_name="cad_document", args={"action": "export", "path": "model.step"}) is None
    assert P._pre_tool_call(tool_name="cad_document", args={"action": "open", "path": str(tmp_path / "a.ipt")}) is None
    assert P._pre_tool_call(tool_name="cad_native",
                            args={"tool": "open_document", "args": {"path": str(tmp_path / "a.ipt")}}) is None


def test_dot_dot_is_blocked_by_the_hook(job):
    out = P._pre_tool_call(tool_name="cad_document", args={"action": "save", "path": "../../x.ipt"})
    assert out["action"] == "block"


@pytest.mark.skipif(sys.platform == "win32" and not hasattr(os, "symlink"), reason="symlinks")
def test_symlink_out_of_the_job_folder_is_followed(job, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, job / "link", target_is_directory=True)
    except OSError:
        pytest.skip("no symlink permission")
    out = P._pre_tool_call(tool_name="cad_document", args={"action": "export", "path": "link/x.step"})
    assert out and out["action"] == "approve"


def test_mouse_notice_survives_an_outside_path(job, tmp_path):
    out = P._pre_tool_call(tool_name="cad_native",
                           args={"tool": "analyze_stress", "args": {"report_path": str(tmp_path / "r.html")}})
    if out and "MOUSE" in out["message"]:
        assert "outside its job folder" in out["message"]


# --------------------------------------------------------------------------- 2. child environment
def test_child_env_drops_provider_keys(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("INVENTOR_FORGE_DEBUG", "1")
    env = paths.child_env({"MY_SERVER_SETTING": "x"}, prefixes=("INVENTOR_FORGE",))
    assert "OPENROUTER_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["MY_SERVER_SETTING"] == "x" and env["INVENTOR_FORGE_DEBUG"] == "1"
    assert any(k.upper() == "PATH" for k in env)


def test_mcp_server_gets_the_minimal_env(monkeypatch):
    from hermes_shaperotator.core import mcp_client
    seen = {}

    class Stop(Exception):
        pass

    def fake_popen(cmd, **kw):
        seen.update(kw["env"])
        raise Stop

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setattr(mcp_client.subprocess, "Popen", fake_popen)
    with pytest.raises(Stop):
        mcp_client.McpClient(["x"], env={"ONLY_THIS": "1"}).start()
    assert "OPENAI_API_KEY" not in seen and seen["ONLY_THIS"] == "1"


def test_mcp_servers_are_not_read_from_the_environment():
    import inspect
    assert "HERMES_SHAPEROTATOR_MCP_SERVERS" not in inspect.getsource(P.register)


# --------------------------------------------------------------------------- 3. regex
def test_padded_table_row_is_fast():
    text = "| safety factor" + " " * 5000 + "|" + " " * 5000 + "x\n"
    t0 = time.perf_counter()
    verify.extract(text * 3)
    assert time.perf_counter() - t0 < 0.5


def test_long_reply_is_capped():
    t0 = time.perf_counter()
    verify.extract(("SF " + " " * 200) * 20000)
    assert time.perf_counter() - t0 < 1.0


@pytest.mark.parametrize("text,value", [
    ("Safety factor: 2.3", 2.3), ("| **Safety factor** | 3.1 |", 3.1), ("factor of safety is about 1.8", 1.8),
    ("FoS (yield) = 2.5", 2.5), ("SF on yield ≈ 4.0", 4.0), ("a 2.0 safety factor", 2.0),
])
def test_safety_factor_still_found(text, value):
    assert [c.value for c in verify.extract(text) if c.quantity == "safety_factor"] == [value]


# --------------------------------------------------------------------------- review 4 (Fable 5.2, Sol 6.1)
@pytest.mark.parametrize("text", ["1" * 20000, "sag" * 6666, "deflect" * 2857, "1" * 10000 + " " * 10000,
                                  "9" * 10000, "1." * 10000])
def test_no_pattern_backtracks_on_long_runs(text):
    t0 = time.perf_counter()
    verify.extract(text)
    assert time.perf_counter() - t0 < 0.5


def test_snapshot_and_preview_names_stay_plain():
    assert paths.safe_name(r"C:\Users\Public\x") == "x"
    assert paths.safe_name(r"..\..\x") == "x"
    assert paths.safe_name("x/../../../escaped") == "escaped"


def test_sibling_job_folders_need_approval(job):
    sibling = job.parent / "older" / "model.stp"
    out = P._pre_tool_call(tool_name="cad_document", args={"action": "export", "path": str(sibling)})
    assert out and out["action"] == "approve"


def test_server_specific_argument_names_are_checked(job, tmp_path):
    for args in ({"destination": str(tmp_path / ".env")}, {"filePath": str(tmp_path / "k")},
                 {"outputPath": str(tmp_path / "k")}, {"anything": str(tmp_path / "k")}, {"anything": "~/.bashrc"}):
        out = P._pre_tool_call(tool_name="cad_native", args={"tool": "save_file", "args": args})
        assert out and out["action"] == "approve", args


def test_expressions_are_not_paths(job):
    assert P._pre_tool_call(tool_name="cad_native",
                            args={"tool": "set_parameter", "args": {"name": "d2", "expression": "d1/2"}}) is None


def test_implicit_save_of_an_outside_file_needs_approval(job, monkeypatch):
    monkeypatch.setattr(paths, "open_document_path", lambda: r"C:\Users\someone\customer.ipt")
    for name, args in (("cad_document", {"action": "save"}), ("cad_document", {"action": "close", "save": True}),
                       ("cad_native", {"tool": "save_document", "args": {}})):
        out = P._pre_tool_call(tool_name=name, args=args)
        assert out and out["action"] == "approve" and "customer.ipt" in out["message"], name
    monkeypatch.setattr(paths, "open_document_path", lambda: str(job / "model.ipt"))
    assert P._pre_tool_call(tool_name="cad_document", args={"action": "save"}) is None


def test_nested_and_listed_paths_are_checked(job, tmp_path):
    for args in ({"export": {"path": str(tmp_path / "x.step")}}, {"files": [str(tmp_path / "a"), "b"]},
                 {"jobs": [{"out": "~/x"}]}):
        out = P._pre_tool_call(tool_name="cad_native", args={"tool": "batch_export", "args": args})
        assert out and out["action"] == "approve", args


def test_free_text_is_not_a_path(job):
    for args in ({"notes": ["A: all dimensions in mm", "/ 2 places"]}, {"fields": {"title": "C: bracket"}},
                 {"comment": "Cost $5 each"}, {"mass_note": "~3 kg"}, {"alloy": "100%Fe%"}, {"profile": "50x50x3 SHS"}):
        assert P._pre_tool_call(tool_name="cad_native", args={"tool": "any_tool", "args": args}) is None, args


def test_climbing_and_uri_arguments_prompt(job):
    for args in ({"out": "../../../x.step"}, {"location": "sub/../../../x"}, {"uri": "file:///C:/Users/x/.ssh/k"}):
        out = P._pre_tool_call(tool_name="cad_native", args={"tool": "export", "args": args})
        assert out and out["action"] in ("approve", "block"), args


def test_named_close_and_in_place_savers_prompt(job, monkeypatch):
    monkeypatch.setattr(paths, "open_document_path", lambda: paths.UNKNOWN)
    for args in ({"tool": "close_document", "args": {"name": "customer.ipt", "save": True}},
                 {"tool": "run_study_cases", "args": {"template": "S", "cases": []}},
                 {"tool": "save_document", "args": {}}):
        out = P._pre_tool_call(tool_name="cad_native", args=args)
        assert out and out["action"] == "approve", args


def test_path_words(job):
    assert paths.path_named("filePath") and paths.path_named("csv_path") and paths.path_named("outputDir")
    assert paths.path_named("export.saveAs") and paths.path_named("files[0]")
    assert not paths.path_named("profile") and not paths.path_named("material")
