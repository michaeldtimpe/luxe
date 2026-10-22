"""Turn carry (2026-10-16): a freeform chat turn continues the previous turn's
conversation, so its prompt EXTENDS the last request and the server's prefix
cache serves it, instead of every turn re-prefilling from zero.

Three layers:
- `run_agent(prior_messages=)` — starts from the prior conversation plus the
  new user message; `AgentResult.messages` is what the next turn carries.
- `run_single(prior_messages=)` — a continued turn's message is the bare goal
  plus follow-up blocks (the Task/Goal frame is already in the conversation).
- `chat/turn.py` — when a turn may carry, and when it must re-fold instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from luxe.agents import single as single_mod
from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.chat import repl
from luxe.chat import slots as slots_mod
from luxe.chat import turn as turn_mod
from luxe.chat.session import ChatSession
from luxe.config import PipelineConfig, RoleConfig
from luxe.memory import session as session_store
from luxe.tools.base import ToolDef


# --- run_agent ---------------------------------------------------------------

class _Backend:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        return self._responses.pop(0)


def _answer(text: str = "done") -> ChatResponse:
    return ChatResponse(text=text, finish_reason="stop",
                        timing=GenerationTiming(prompt_tokens=50))


def _run(backend, *, system="sys", task="task", prior=None):
    return run_agent(
        backend=backend,
        role_cfg=RoleConfig(model_key="t", num_ctx=10_000, max_steps=4,
                            max_tokens_per_turn=256, temperature=0.0),
        system_prompt=system, task_prompt=task,
        tool_defs=[ToolDef(name="read_file", description="read", parameters={
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"]})],
        tool_fns={"read_file": lambda args: ("contents", None)},
        prior_messages=prior,
    )


def test_fresh_run_exports_its_conversation_plus_the_answer():
    backend = _Backend([
        ChatResponse(text="", tool_calls=[ToolCallResponse(
            id="r", name="read_file", arguments={"path": "a.py"})],
            finish_reason="tool_calls", timing=GenerationTiming(prompt_tokens=40)),
        _answer("the answer"),
    ])
    result = _run(backend)
    assert not result.carried
    # Everything the last request sent, then the final answer.
    assert result.messages[:-1] == backend.calls[-1]
    assert result.messages[-1] == {"role": "assistant", "content": "the answer"}


def test_prior_messages_make_the_new_prompt_extend_the_old_one():
    first = _Backend([_answer("first answer")])
    turn1 = _run(first, task="turn one")
    second = _Backend([_answer("second answer")])
    turn2 = _run(second, task="turn two", prior=turn1.messages)

    sent = second.calls[0]
    # The cache property: turn 1's last request is a strict prefix of turn 2's.
    assert sent[:len(first.calls[-1])] == first.calls[-1]
    assert sent[-2:] == [{"role": "assistant", "content": "first answer"},
                         {"role": "user", "content": "turn two"}]
    assert turn2.carried


def test_a_different_system_prompt_rejects_the_carry():
    turn1 = _run(_Backend([_answer()]))
    backend = _Backend([_answer()])
    result = _run(backend, system="another persona", prior=turn1.messages)
    assert not result.carried
    assert backend.calls[0] == [{"role": "system", "content": "another persona"},
                                {"role": "user", "content": "task"}]


def test_an_aborted_run_does_not_append_an_answer():
    backend = _Backend([ChatResponse(text="", tool_calls=[ToolCallResponse(
        id=str(i), name="read_file", arguments={"path": f"{i}.py"})],
        finish_reason="tool_calls", timing=GenerationTiming(prompt_tokens=40))
        for i in range(4)])
    result = _run(backend)
    assert result.aborted
    assert result.messages[-1]["role"] != "assistant" or result.messages[-1].get("tool_calls")


# --- run_single --------------------------------------------------------------

def _capture_run_single(monkeypatch, **kw):
    seen: dict[str, Any] = {}

    def fake_run_agent(backend, role_cfg, **k):
        seen.update(k)
        return "result"

    monkeypatch.setattr(single_mod, "run_agent", fake_run_agent)
    single_mod.run_single(object(), RoleConfig(model_key="t"), goal="hello",
                          task_type="review", **kw)
    return seen


def test_run_single_fresh_prompt_is_unchanged(monkeypatch):
    seen = _capture_run_single(monkeypatch, extra_context="\n\nX")
    assert seen["task_prompt"].startswith("Task type: review\nGoal: hello\n\n")
    assert seen["task_prompt"].endswith("X")
    assert seen["prior_messages"] is None


def test_run_single_continued_turn_sends_only_the_message(monkeypatch):
    prior = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    seen = _capture_run_single(monkeypatch, extra_context="\n\n<attached_files/>",
                               prior_messages=prior)
    assert seen["task_prompt"] == "hello\n\n<attached_files/>"
    assert seen["prior_messages"] is prior


# --- chat --------------------------------------------------------------------

class _FakeBackend:
    def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
        self.base_url, self.model = base_url, model
        self.timeout_s, self.api_key = timeout_s, api_key

    def unload_all_loaded(self, *, except_for=None):
        return {}

    def thermal_guard(self, *a, **k):
        return True


class _Result:
    def __init__(self, messages, *, prompt_tokens=1000, aborted=False):
        self.final_text = messages[-1]["content"]
        self.steps, self.tool_calls_total, self.wall_s = 1, 0, 1.0
        self.completion_tokens, self.prompt_tokens = 10, prompt_tokens
        self.last_prompt_tokens = prompt_tokens
        self.peak_context_pressure = self.final_context_pressure = 0.1
        self.aborted, self.abort_reason = aborted, ""
        self.messages = messages
        self.step_texts: list[str] = []


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch):
    # $HOME isolation comes from tests/conftest.py's autouse _hermetic_home.
    monkeypatch.setattr(slots_mod, "Backend", _FakeBackend)


@pytest.fixture
def chat(tmp_path, monkeypatch):
    """Drive real prepare_turn/finalize_turn with a stub `run_single` that
    records what each turn was given and answers with a growing conversation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = PipelineConfig(models={"monolith": "Champ"},
                         roles={"monolith": RoleConfig(model_key="monolith",
                                                       num_ctx=32768)})
    session = ChatSession(repo_path=str(repo))
    session.session_id = session_store.new_session(
        repo_path=str(repo), project_hash="h", slot_models={}).session_id
    sm = slots_mod.SlotManager(cfg)
    calls: list[dict[str, Any]] = []
    knobs = {"prompt_tokens": 1000, "aborted": False}

    def fake_run_single(backend, role_cfg, **kw):
        calls.append(kw)
        prior = kw.get("prior_messages") or [{"role": "system", "content": "sys"}]
        msgs = [*prior, {"role": "user", "content": kw["goal"]},
                {"role": "assistant", "content": f"answer to {kw['goal']}"}]
        return _Result(msgs, prompt_tokens=knobs["prompt_tokens"],
                       aborted=knobs["aborted"])

    monkeypatch.setattr(turn_mod, "run_single", fake_run_single)

    def turn(message: str):
        prep = repl.prepare_turn(message, session, sm, cfg, frozenset(),
                                 lambda m: "review")
        result = prep.call(lambda tc: None, None, None)
        repl.finalize_turn(session, prep, result, interrupted=False,
                           message=message, started_at=1.0, ended_at=2.0)
        return prep

    return session, cfg, calls, knobs, turn


def test_second_turn_continues_the_first(chat):
    session, _cfg, calls, _k, turn = chat
    assert not turn("one").carried
    assert calls[0]["prior_messages"] is None
    assert turn("two").carried
    assert calls[1]["prior_messages"][-1] == {"role": "assistant",
                                              "content": "answer to one"}
    # No folded history: the conversation itself is the history now.
    assert "<conversation_history>" not in calls[1]["extra_context"]
    assert turn("three").carried


def test_clear_retires_the_carry(chat):
    session, _cfg, _calls, _k, turn = chat
    turn("one")
    session.turns.clear()
    assert not turn("two").carried


def test_a_frame_change_re_folds(chat):
    session, _cfg, calls, _k, turn = chat
    turn("one")
    session.write_enabled = True      # different tools + mode hints
    assert not turn("two").carried
    assert "<conversation_history>" in calls[1]["extra_context"]
    assert turn("three").carried      # and the next turn carries again


def test_constraints_change_re_folds(chat):
    session, _cfg, _calls, _k, turn = chat
    turn("one")
    session.system_constraints.append("always answer in French")
    assert not turn("two").carried


def test_a_full_window_re_folds(chat):
    _s, _cfg, _calls, knobs, turn = chat
    knobs["prompt_tokens"] = 20_000   # > 0.5 × 32768
    turn("one")
    assert not turn("two").carried


def test_an_aborted_turn_hands_nothing_on(chat):
    session, _cfg, _calls, knobs, turn = chat
    knobs["aborted"] = True
    turn("one")
    assert session.carry is None
    knobs["aborted"] = False
    assert not turn("two").carried


def test_config_can_turn_it_off(chat):
    _s, cfg, _calls, _k, turn = chat
    cfg.turn_carry = False
    turn("one")
    assert not turn("two").carried


def test_plan_and_pinned_turns_never_carry(chat):
    session, _cfg, _calls, _k, turn = chat
    turn("one")
    session.pinned_slot = "chat"
    assert not turn("two").carried


def test_attachments_ride_a_carried_turn_once(chat):
    session, _cfg, calls, _k, turn = chat
    turn("one")
    session.attachments = [{"path": "a.txt", "content": "AAA"}]
    assert turn("two").carried
    assert '<file path="a.txt">\nAAA\n</file>' in calls[1]["extra_context"]
    assert session.attachments == []
    turn("three")
    assert "attached_files" not in calls[2]["extra_context"]
