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
    def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        self.api_key = api_key

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


def _persona_id(model: str) -> str:
    """The conversational system-prompt id for a turn served by `model`.

    Model-bound since 2026-08-17 (the persona self-identifies as the model,
    with luxe as the harness), so these assertions read the id through the
    registry accessor rather than hardcoding a spelling the registry owns.
    """
    from luxe.agents.prompts import chat_persona_id
    return chat_persona_id(model)


def test_chat_slot_gets_conversational_persona(_ctx, monkeypatch):
    """A turn routing to the chat slot (e.g. a bare greeting → review) swaps the
    role's prompt ids to the conversational variant so it answers directly
    instead of running the code-maintenance orientation loop."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("hello", session, sm, cfg, frozenset(), lambda m: "review")
    assert prep.slot == "chat"
    assert prep.role_cfg.system_prompt_id == _persona_id(prep.model)
    assert prep.role_cfg.task_prompt_id == "chat_conversational"


def test_freeform_codey_message_stays_conversational(_ctx, monkeypatch):
    """Regression (2026-07-29 'chats become coding sessions'): the keyword
    heuristic routes 'add …' to the code slot, but an unpinned freeform turn
    must STILL get the conversational persona (and no task overlay) — the slot
    only picks the model. Previously the persona was keyed on slot == 'chat',
    so this message inherited the repo-maintenance persona."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("add a feature", session, sm, cfg, frozenset(),
                             lambda m: "implement")
    assert prep.slot == "code"     # model routing unchanged
    assert prep.role_cfg.system_prompt_id == _persona_id(prep.model)
    assert prep.role_cfg.task_prompt_id == "chat_conversational"
    assert prep.role_cfg.task_overlay_id == ""


def test_freeform_turn_clears_config_task_overlay(_ctx, monkeypatch):
    """chat.yaml ships task_overlay_id on the monolith role; a conversational
    turn must not drag the manage/strict overlay along."""
    cfg, session, sm = _ctx
    cfg.roles["monolith"].task_overlay_id = "manage_strict_only"
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("can you explain how this repo builds?", session,
                             sm, cfg, frozenset(), lambda m: "summarize")
    assert prep.role_cfg.task_overlay_id == ""
    assert prep.role_cfg.system_prompt_id == _persona_id(prep.model)


def test_real_inference_heuristic_no_longer_flips_persona(_ctx, monkeypatch):
    """End-to-end with the REAL maintain heuristic (`cli._infer_task_type`):
    casual messages full of trigger keywords stay conversational."""
    from luxe.cli import _infer_task_type

    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    for msg in ("explain the difference between threads and processes",
                "I want to change my flight to Tuesday",
                "how do I fix a flat tire?"):
        prep = repl.prepare_turn(msg, session, sm, cfg, frozenset(),
                                 _infer_task_type)
        assert prep.role_cfg.system_prompt_id == _persona_id(prep.model), msg


def test_use_pinned_slot_keeps_baseline_persona(_ctx, monkeypatch):
    """`/use code` is an explicit task escalation: the pinned turn keeps the
    baseline maintenance persona."""
    cfg, session, sm = _ctx
    session.pinned_slot = "code"
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


def test_finalize_turn_stamps_backend_on_assistant_record(_ctx, monkeypatch):
    import json

    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("hello", session, sm, cfg, frozenset(), lambda m: "review")
    result = prep.call(lambda tc: None, None, None)
    repl.finalize_turn(session, prep, result, interrupted=False,
                       message="hello", started_at=1.0, ended_at=2.0)
    tp = session_store.session_dir(session.session_id) / "transcript.jsonl"
    records = [json.loads(l) for l in tp.read_text().splitlines()]
    assistant = [r for r in records if r["kind"] == "assistant"]
    assert assistant and assistant[-1]["backend"] == "local"


def test_line_run_turn_still_works_headless(_ctx, monkeypatch):
    """The non-terminal line path runs end-to-end through the new core."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    out = Console(file=__import__("io").StringIO(), force_terminal=False, width=100)
    outcome = repl._run_turn("hi", session, sm, cfg, frozenset(), out,
                             repl.CancelToken(), lambda m: "review")
    assert outcome.final_text == "the answer"
    assert not outcome.interrupted


# --- interrupted turns keep observed state (2026-07-31) -----------------------


def test_finalize_interrupted_turn_records_observed_tool_calls(_ctx, monkeypatch):
    """ChatCancelled loses the in-flight AgentResult, but tools that COMPLETED
    were observed via note_tool — the transcript must not claim tool_calls=0
    (session 5bb630813c21 turn 11: one bash call ran, record said none)."""
    import json

    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("probe the network", session, sm, cfg, frozenset(),
                             lambda m: "review")
    prep.note_tool(_TC("bash", command="curl -v https://x"))
    prep.note_tool(_TC("bash", command="curl -v https://y"))

    outcome = repl.finalize_turn(session, prep, None, interrupted=True,
                                 message="probe the network",
                                 started_at=1.0, ended_at=2.0)
    assert outcome.tool_calls == 2
    tp = session_store.session_dir(session.session_id) / "transcript.jsonl"
    records = [json.loads(ln) for ln in tp.read_text().splitlines()]
    last = [r for r in records if r["kind"] == "assistant"][-1]
    assert last["interrupted"] is True
    assert last["tool_calls"] == 2


def test_finalize_interrupted_turn_persists_partial_stream(_ctx, monkeypatch):
    """'partial turn saved' must actually save the streamed prose, not an
    empty record."""
    import json

    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("hi", session, sm, cfg, frozenset(), lambda m: "review")

    repl.finalize_turn(session, prep, None, interrupted=True, message="hi",
                       started_at=1.0, ended_at=2.0,
                       partial_text="Half an answer that was")
    tp = session_store.session_dir(session.session_id) / "transcript.jsonl"
    records = [json.loads(ln) for ln in tp.read_text().splitlines()]
    last = [r for r in records if r["kind"] == "assistant"][-1]
    assert last["text"] == "Half an answer that was"
    assert session.turns[-1].assistant == "Half an answer that was"


def test_finalize_completed_turn_ignores_partial_text(_ctx, monkeypatch):
    """A completed turn keeps the model's final text even when a stream buffer
    is passed (it always is by the front-ends)."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("hi", session, sm, cfg, frozenset(), lambda m: "review")
    result = prep.call(lambda tc: None, None, None)
    outcome = repl.finalize_turn(session, prep, result, interrupted=False,
                                 message="hi", started_at=1.0, ended_at=2.0,
                                 partial_text="stream buffer contents")
    assert outcome.final_text == "the answer"
    # completed turns keep the AgentResult's own count, not the observed one
    assert outcome.tool_calls == 3


def test_prepare_turn_passes_cancel_and_on_start_to_chat_bash(_ctx, monkeypatch):
    """Write+dev mode wires the CancelToken and the dispatch callback into the
    chat bash fn (the cancellable/visible variant)."""
    from luxe.tools import fs as fs_mod
    cfg, session, sm = _ctx
    fs_mod.set_repo_root(session.repo_path)
    session.write_enabled = True
    session.unrestricted_bash = True
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())

    started: list = []
    tok = repl.CancelToken()
    bash_fn = None

    # capture what prepare_turn hands run_single
    def fake_run_single(backend, role_cfg, **kw):
        nonlocal bash_fn
        bash_fn = kw["extra_tool_fns"]["bash"]
        return _FakeResult()

    monkeypatch.setattr(repl, "run_single", fake_run_single)
    prep = repl.prepare_turn("run it", session, sm, cfg, frozenset(),
                             lambda m: "review", cancel=tok,
                             on_tool_start=started.append)
    prep.call(lambda tc: None, None, None)
    out, err = bash_fn({"command": "echo wired"})
    assert err is None and "wired" in out
    assert started == ["echo wired"]


# --- contract-scan cache invalidation ---------------------------------------


def test_finalize_turn_invalidates_scan_cache_when_a_sdd_is_written(_ctx, monkeypatch):
    """Chat caches the `.sdd` scan per repo root for the session; a turn that
    writes a contract must drop it so the next turn sees the new rules."""
    from luxe import spec_resolver

    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    dropped: list = []
    monkeypatch.setattr(spec_resolver, "invalidate_scan_cache", dropped.append)

    prep = repl.prepare_turn("hello", session, sm, cfg, frozenset(), lambda m: "review")
    prep.note_tool(_TC("write_file", path="src/luxe/luxe.sdd"))
    result = prep.call(lambda tc: None, None, None)
    repl.finalize_turn(session, prep, result, interrupted=False,
                       message="hello", started_at=1.0, ended_at=2.0)

    assert dropped == [session.repo_path]


def test_finalize_turn_keeps_scan_cache_for_ordinary_writes(_ctx, monkeypatch):
    from luxe import spec_resolver

    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    dropped: list = []
    monkeypatch.setattr(spec_resolver, "invalidate_scan_cache", dropped.append)

    prep = repl.prepare_turn("hello", session, sm, cfg, frozenset(), lambda m: "review")
    prep.note_tool(_TC("edit_file", path="src/luxe/cli.py"))
    result = prep.call(lambda tc: None, None, None)
    repl.finalize_turn(session, prep, result, interrupted=False,
                       message="hello", started_at=1.0, ended_at=2.0)

    assert dropped == []


def test_the_conversational_persona_names_the_model_serving_the_turn(
        _ctx, monkeypatch):
    """End-to-end for the 2026-08-17 self-identity change: the id `prepare_turn`
    puts on the role must resolve to a system prompt that names the routed
    model, with luxe as the harness around it."""
    from luxe.agents.prompts import get as get_prompt

    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("hello", session, sm, cfg, frozenset(),
                             lambda m: "review")
    system = get_prompt(prep.role_cfg.system_prompt_id).system
    assert system.startswith(f"You are {prep.model}, an AI assistant")
    assert "through luxe" in system
    assert "CONVERSATION" in system


def test_a_pinned_turn_keeps_the_unparameterised_maintenance_persona(
        _ctx, monkeypatch):
    """The identity change is scoped to the conversational persona: an
    explicit `/use <slot>` task turn still gets the baseline, which names no
    model and no tool."""
    from luxe.agents.prompts import get as get_prompt

    cfg, session, sm = _ctx
    session.pinned_slot = "code"
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    prep = repl.prepare_turn("do the thing", session, sm, cfg, frozenset(),
                             lambda m: "implement")
    assert prep.role_cfg.system_prompt_id == "baseline"
    assert get_prompt("baseline").system.startswith(
        "You are a code maintenance specialist")


# --- aborted turns are visible failures (2026-08-23) -------------------------


class _AbortedResult(_FakeResult):
    """What `run_single` returns when the loop CONTAINS a backend failure:
    `aborted`/`abort_reason` set, empty prose, no exception raised."""

    def __init__(self, reason="Backend error: ConnectError: "
                              "[Errno 61] Connection refused"):
        super().__init__()
        self.final_text = ""
        self.steps = 1
        self.tool_calls_total = 0
        self.aborted = True
        self.abort_reason = reason


def _errors(session) -> list:
    import json
    tp = session_store.session_dir(session.session_id) / "transcript.jsonl"
    if not tp.is_file():
        return []
    records = [json.loads(ln) for ln in tp.read_text().splitlines()]
    return [r for r in records if r["kind"] == "error"]


def test_aborted_turn_is_reported_and_recorded(_ctx, monkeypatch):
    """Regression (session 3aabb18b0e07): the endpoint was unreachable behind a
    dead system proxy, so every turn came back aborted with empty text — and
    the REPL rendered five SUCCESSFUL blank replies, with not one `error`
    record in the transcript to say what happened."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _AbortedResult())
    out = Console(file=__import__("io").StringIO(), force_terminal=False, width=100)

    outcome = repl._run_turn("hi", session, sm, cfg, frozenset(), out,
                             repl.CancelToken(), lambda m: "review")

    assert not outcome.interrupted
    printed = out.file.getvalue()
    assert "Connection refused" in printed          # visible, not silent
    errs = _errors(session)
    assert errs and errs[-1]["text"].startswith("Backend error:")
    assert errs[-1]["model"] == "Champ"


def test_healthy_turn_writes_no_error_record(_ctx, monkeypatch):
    cfg, session, sm = _ctx
    monkeypatch.setattr(repl, "run_single", lambda *a, **k: _FakeResult())
    out = Console(file=__import__("io").StringIO(), force_terminal=False, width=100)
    repl._run_turn("hi", session, sm, cfg, frozenset(), out,
                   repl.CancelToken(), lambda m: "review")
    assert not _errors(session)


def test_note_aborted_turn_ignores_a_completed_turn(_ctx):
    cfg, session, sm = _ctx
    assert repl.note_aborted_turn(session, sm, _FakeResult()) is None
    assert not _errors(session)


def test_abort_hint_only_offered_for_backend_failures(_ctx, monkeypatch):
    """`/backend` is the escape hatch for a dead endpoint — pointing at it for
    'Max steps reached' would name the wrong problem."""
    cfg, session, sm = _ctx
    monkeypatch.setattr(sm, "unreachable_hint",
                        lambda: "local oMLX unreachable — try /backend m5")

    reason, hint = repl.note_aborted_turn(session, sm, _AbortedResult())
    assert reason.startswith("Backend error:")
    assert hint == "local oMLX unreachable — try /backend m5"

    _, hint = repl.note_aborted_turn(session, sm,
                                     _AbortedResult("Max steps reached (30)"))
    assert hint is None
