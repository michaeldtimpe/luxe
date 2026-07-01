"""Regression net for the UI-agnostic turn seam (prepare_turn / finalize_turn)
extracted from `_run_turn` so the line REPL and the Textual TUI share one core.
Stubs `run_single`; no model/network."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from luxe.chat import repl
from luxe.chat import slots as slots_mod
from luxe.chat.session import ChatSession
from luxe.config import PipelineConfig, RoleConfig
from luxe.memory import session as session_store


class _FakeBackend:
    def __init__(self, base_url="", model=""):
        self.base_url = base_url
        self.model = model

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def thermal_guard(self, *a, **k):
        return True


class _FakeResult:
    def __init__(self):
        self.final_text = "the answer"
        self.steps = 2
        self.tool_calls_total = 3
        self.wall_s = 1.0
        self.completion_tokens = 42
        self.prompt_tokens = 100
        self.peak_context_pressure = 0.1
        self.final_context_pressure = 0.1


class _TC:
    def __init__(self, name, **args):
        self.name = name
        self.arguments = args
        self.error = None
        self.duplicate = False
        self.result = args.get("_result", "")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(slots_mod, "Backend", _FakeBackend)


@pytest.fixture
def _ctx(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )
    session = ChatSession(repo_path=str(repo))
    meta = session_store.new_session(repo_path=str(repo), project_hash="h", slot_models={})
    session.session_id = meta.session_id
    return cfg, session, slots_mod.SlotManager(cfg)


def test_prepare_turn_assembles_run_single_chat_call(_ctx, monkeypatch):
    cfg, session, sm = _ctx
    captured = {}

    def fake_run_single(backend, role_cfg, **kw):
        captured.update(kw)
        captured["role_cfg"] = role_cfg
        return _FakeResult()

    monkeypatch.setattr(repl, "run_single", fake_run_single)
    prep = repl.prepare_turn("do it", session, sm, cfg, frozenset(), lambda m: "review")
    res = prep.call(lambda tc: None, None, None)

    assert res.final_text == "the answer"
    assert captured["goal"] == "do it"
    assert captured["task_type"] == "review"
    assert captured["phase"] == "chat"            # chat-only, never benchmark
    assert "update_ledger" in captured["extra_tool_fns"]   # ledger tool always present
    assert prep.slot == "chat" and prep.model == "Champ"


def test_chat_slot_gets_conversational_persona(_ctx, monkeypatch):
    """A turn routing to the chat slot (e.g. a bare greeting → review) swaps the
    role's prompt ids to the conversational variant so it answers directly
    instead of running the code-maintenance orientation loop."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("hello", session, sm, cfg, frozenset(), lambda m: "review")
    assert prep.slot == "chat"
    assert prep.role_cfg.system_prompt_id == "chat_conversational"
    assert prep.role_cfg.task_prompt_id == "chat_conversational"


def test_code_slot_keeps_baseline_persona(_ctx, monkeypatch):
    """Focused work (implement → code slot) is untouched by the chat swap."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("add a feature", session, sm, cfg, frozenset(),
                             lambda m: "implement")
    assert prep.slot == "code"
    assert prep.role_cfg.system_prompt_id == "baseline"


def test_goal_rounds_keep_working_persona_on_chat_slot(_ctx, monkeypatch):
    """`continue work` infers to review → chat slot, but during an autonomous
    /goal run it must stay a working turn, not flip conversational."""
    cfg, session, sm = _ctx
    session.goal_active = True
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("continue work", session, sm, cfg, frozenset(),
                             lambda m: "review")
    assert prep.slot == "chat"
    assert prep.role_cfg.system_prompt_id == "baseline"


def test_plan_drafting_keeps_working_persona(_ctx, monkeypatch):
    """/plan drafting turns route through prepare_turn with plan_mode=True and
    must not be flipped to the conversational persona."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("draft a plan", session, sm, cfg, frozenset(),
                             lambda m: "review", plan_mode=True)
    assert prep.role_cfg.system_prompt_id == "baseline"


def test_note_tool_records_changed_files_and_fingerprint(_ctx, monkeypatch):
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("x", session, sm, cfg, frozenset(), lambda m: "review")

    prep.note_tool(_TC("edit_file", path="src/a.py"))
    prep.note_tool(_TC("grep", pattern="foo"))
    assert "src/a.py" in prep.changed_files
    assert ("grep", "foo") in prep.fingerprint


def test_finalize_turn_builds_outcome_and_persists(_ctx, monkeypatch):
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("hello", session, sm, cfg, frozenset(), lambda m: "review")
    prep.note_tool(_TC("edit_file", path="b.py"))
    result = prep.call(lambda tc: None, None, None)

    outcome = repl.finalize_turn(session, prep, result, interrupted=False,
                                 message="hello", started_at=1.0, ended_at=2.0)
    assert outcome.final_text == "the answer"
    assert outcome.result is result
    assert outcome.slot == "chat" and outcome.model == "Champ"
    assert outcome.files_changed == 1
    assert outcome.started_at == 1.0 and outcome.ended_at == 2.0
    # assistant turn persisted to the session history
    assert session.turns and session.turns[-1].assistant == "the answer"


def test_line_run_turn_still_works_headless(_ctx, monkeypatch):
    """The non-terminal line path runs end-to-end through the new core."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    out = Console(file=__import__("io").StringIO(), force_terminal=False, width=100)
    outcome = repl._run_turn("hi", session, sm, cfg, frozenset(), out,
                             repl.CancelToken(), lambda m: "review")
    assert outcome.final_text == "the answer"
    assert not outcome.interrupted
