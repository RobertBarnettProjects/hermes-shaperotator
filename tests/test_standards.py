"""The standard is asked once per chat and kept for the rest of it."""
import json

import pytest
from conftest import load_plugin

P = load_plugin()
from hermes_shaperotator.core import standard as S  # noqa: E402
from hermes_shaperotator.core import tools as T  # noqa: E402
from hermes_shaperotator.core.state import STATE  # noqa: E402


@pytest.fixture()
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_file", lambda: tmp_path / "standards.json")
    monkeypatch.setitem(STATE.settings, "standard", "")
    monkeypatch.setattr(STATE, "session_id", "chat-1")
    return tmp_path


def test_not_chosen_until_the_user_picks(chat):
    out = json.loads(T.cad_standard({"action": "get"}))
    assert out["chosen"] is False and "Ask the user" in out["ask"]
    assert len(json.loads(T.cad_standard({"action": "list"}))["standards"]) == 7


def test_choice_is_kept_for_the_chat_only(chat, monkeypatch):
    out = json.loads(T.cad_standard({"action": "set", "standard": "ansi", "drawing_units": "in"}))
    assert out["standard"] == "ASME" and out["projection"] == "third-angle"
    assert json.loads(T.cad_standard({"action": "get"}))["standard"] == "ASME"
    assert S.for_call() == "ASME" and S.for_call("DIN") == "DIN"
    monkeypatch.setattr(STATE, "session_id", "chat-2")
    assert S.chosen() is None


def test_choice_survives_a_restart(chat):
    S.choose("JIS")
    assert json.loads((chat / "standards.json").read_text())["chat-1"]["standard"] == "JIS"


def test_drawing_asks_for_a_standard_first(chat):
    out = json.loads(T.cad_drawing({"action": "new"}))
    assert out["ok"] is False and "cad_standard" in out["error"]


def test_plugin_setting_counts_as_a_standing_choice(chat, monkeypatch):
    monkeypatch.setitem(STATE.settings, "standard", "AS 1100")
    assert S.chosen()["standard"] == "AS1100"


def test_bad_standard_is_a_clear_error(chat):
    out = json.loads(T.cad_standard({"action": "set", "standard": "klingon"}))
    assert out["ok"] is False and "AS1100" in out["error"]
    out = json.loads(T.cad_standard({"action": "set", "standard": "ISO", "drawing_units": "in"}))
    assert out["ok"] is False


def test_context_line_carries_the_standard(chat, monkeypatch):
    monkeypatch.setattr(P, "_CAD_SESSIONS", {"chat-1"})
    monkeypatch.setattr(STATE, "backends", {"x": object()})
    S.choose("DIN", session_id="chat-1")
    assert "standard for this chat: DIN" in P._pre_llm_call(session_id="chat-1")["context"]
