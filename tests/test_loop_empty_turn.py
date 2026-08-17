"""Empty-completion retry (LUXE_EMPTY_TURN_RETRY).

A turn can end with NOTHING: no tool call, no text, a normal finish reason.
The loop's terminal test is `if not tool_calls:`, and an empty `resp.text`
satisfies it exactly as a real answer does — so the run recorded a blank
assistant message and stopped, `aborted=False`, no gate, no warning, and
nothing in the record separating "answered" from "said nothing".

Founding instance — session 0eb5998d8825 turn -5, 2026-08-17, a hosted
REASONING model (`moonshotai/kimi-k3`): steps=1, tool_calls=0, normal finish
reason, `content` empty. The model spent its budget in the reasoning channel
and emitted no content at all. The shape is NOT reasoning-specific — any
provider hiccup produces it — so the guard keys on the observable emptiness,
never on the model.

Deliberately modelled on truncated-turn retry (tests/test_loop_truncated_turn.py):
same nudge-and-continue mechanism, same env-switch spelling, same
ungated-telemetry rule. It reaches the benchmark path for the same reason: a
run that records an empty answer as a completion is measuring the wrong thing.
"""

from __future__ import annotations

from typing import Any

import pytest

from luxe.agents.guardrails import _EMPTY_TURN_MAX_RETRIES, EmptyTurnGuard
from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef


class _ScriptedBackend:
    def __init__(self, scripted: list[ChatResponse]) -> None:
        self._scripted = list(scripted)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        if not self._scripted:
            return ChatResponse(
                text="fallback", finish_reason="stop",
                timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))
        return self._scripted.pop(0)


def _role(max_steps: int = 30) -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=max_steps,
                      max_tokens_per_turn=8192, temperature=0.0)


def _empty(reasoning_chars: int = 0) -> ChatResponse:
    """The failure: normal finish, no tool call, nothing said."""
    return ChatResponse(
        text="", finish_reason="stop", reasoning_chars=reasoning_chars,
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=792))


def _answered(text: str = "here is your answer") -> ChatResponse:
    return ChatResponse(
        text=text, finish_reason="stop",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=50))


def _truncated() -> ChatResponse:
    """Empty AND capped — the truncation guard owns this one."""
    return ChatResponse(
        text="", finish_reason="length",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=8192))


def _edit() -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="e", name="edit_file",
                                     arguments={"path": "a.py", "old": "a",
                                                "new": "b"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=200))


def _tools():
    return [ToolDef(name="edit_file", description="edit",
                    parameters={"type": "object",
                                "properties": {"path": {"type": "string"},
                                               "old": {"type": "string"},
                                               "new": {"type": "string"}},
                                "required": ["path", "old", "new"]})]


def _run(scripted, on_notice=None):
    backend = _ScriptedBackend(list(scripted))
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="answer the question",
        tool_defs=_tools(),
        tool_fns={"edit_file": lambda args: ("patched", None)},
        on_notice=on_notice,
    )
    return backend, result


def _nudges(backend) -> list[dict]:
    """Empty-turn nudges in the FINAL messages snapshot (successive nudges are
    byte-identical, so deduplicating across snapshots would collapse two
    firings into one and pass the bounded test vacuously)."""
    if not backend.calls:
        return []
    return [m for m in backend.calls[-1]
            if m.get("_luxe_nudge_type") == "empty_turn"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LUXE_EMPTY_TURN_RETRY", raising=False)
    monkeypatch.delenv("LUXE_TRUNCATED_TURN_RETRY", raising=False)


class TestDefaultOn:
    def test_an_empty_turn_is_retried_with_no_env_set(self):
        """The promoted default: unset means ON."""
        backend, result = _run([_empty(), _answered()])
        assert len(backend.calls) == 2
        assert result.final_text == "here is your answer"
        assert len(_nudges(backend)) == 1

    def test_exactly_zero_restores_the_old_terminal_behaviour(self, monkeypatch):
        """The ablation path — and the shape of the bug: one chat, then the
        loop ends on the blank response and records it as the answer."""
        monkeypatch.setenv("LUXE_EMPTY_TURN_RETRY", "0")
        backend, result = _run([_empty(), _answered()])
        assert len(backend.calls) == 1
        assert result.final_text == ""
        assert result.aborted is False        # this is why it went unnoticed
        assert _nudges(backend) == []

    @pytest.mark.parametrize("value", ["1", "", "no", "false", "true", "2"])
    def test_only_the_exact_string_zero_disables_it(self, monkeypatch, value):
        monkeypatch.setenv("LUXE_EMPTY_TURN_RETRY", value)
        backend, _ = _run([_empty(), _answered()])
        assert len(_nudges(backend)) == 1


class TestWhatItDoesNotFireOn:
    def test_a_real_answer_is_never_retried(self):
        backend, result = _run([_answered()])
        assert len(backend.calls) == 1
        assert _nudges(backend) == []

    def test_a_turn_with_tool_calls_is_never_retried(self):
        """An empty text alongside a tool call is the NORMAL acting shape."""
        backend, result = _run([_edit(), _answered()])
        assert _nudges(backend) == []
        assert result.tool_calls_total == 1

    def test_truncation_is_left_to_the_guard_that_owns_it(self):
        """Empty AND finish_reason='length' is a cut-off turn. Firing both
        guards would spend two retry budgets on one problem."""
        backend, _ = _run([_truncated(), _answered()])
        assert _nudges(backend) == []
        assert [m for m in backend.calls[-1]
                if m.get("_luxe_nudge_type") == "truncated_turn"]

    def test_whitespace_only_counts_as_empty(self):
        """A lone newline is not an answer either."""
        blank = ChatResponse(
            text="  \n\t ", finish_reason="stop",
            timing=GenerationTiming(prompt_tokens=10, completion_tokens=3))
        backend, _ = _run([blank, _answered()])
        assert len(_nudges(backend)) == 1


class TestBounded:
    def test_it_fires_at_most_once(self):
        """A model that returns nothing twice will not answer on a third, and
        each attempt is a full generation."""
        backend, result = _run([_empty(), _empty(), _empty()])
        assert len(_nudges(backend)) == _EMPTY_TURN_MAX_RETRIES == 1
        assert len(backend.calls) == 2
        assert result.final_text == ""

    def test_the_second_empty_response_ends_the_run(self):
        backend, result = _run([_empty(), _empty(), _answered()])
        assert len(backend.calls) == 2      # the answer is never reached
        assert result.final_text == ""


class TestTheNotice:
    def test_it_announces_the_retry(self):
        notices: list[str] = []
        _run([_empty(), _answered()], on_notice=notices.append)
        assert any("replied with nothing" in n for n in notices)

    def test_it_says_when_the_model_thought_and_said_nothing(self):
        """"It thought for 3,568 characters and said nothing" is a different
        report from "it returned nothing", with a different fix."""
        notices: list[str] = []
        _run([_empty(reasoning_chars=3568), _answered()],
             on_notice=notices.append)
        assert any("3,568 characters of reasoning" in n for n in notices)

    def test_a_raising_callback_cannot_kill_the_run(self):
        def boom(_text):
            raise RuntimeError("ui exploded")

        _backend, result = _run([_empty(), _answered()], on_notice=boom)
        assert result.final_text == "here is your answer"


class TestTheGuardInIsolation:
    def test_it_is_inert_when_disabled(self):
        assert EmptyTurnGuard.should_fire(
            empty_turn_retry_enabled=False, text="", has_tool_calls=False,
            finish_reason="stop", retries_used=0) is None

    @pytest.mark.parametrize("text,tools,reason,fires", [
        ("", False, "stop", True),
        ("  \n ", False, "stop", True),
        ("", False, "", True),               # blank reason is not evidence
        ("answered", False, "stop", False),
        ("", True, "tool_calls", False),
        ("", False, "length", False),        # truncation guard's job
    ])
    def test_the_firing_conditions(self, text, tools, reason, fires):
        got = EmptyTurnGuard.should_fire(
            empty_turn_retry_enabled=True, text=text, has_tool_calls=tools,
            finish_reason=reason, retries_used=0)
        assert (got is not None) is fires

    def test_the_budget_is_respected(self):
        assert EmptyTurnGuard.should_fire(
            empty_turn_retry_enabled=True, text="", has_tool_calls=False,
            finish_reason="stop", retries_used=1) is None
