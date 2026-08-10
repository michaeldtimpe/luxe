"""Truncated-turn retry (LUXE_TRUNCATED_TURN_RETRY).

A response that hits `max_tokens_per_turn` returns `finish_reason="length"`,
and mid-prose it carries no tool calls. The loop's terminal test is
`if not tool_calls:` and it never consulted `finish_reason`, so a turn CUT OFF
mid-sentence was indistinguishable from a model that finished and chose to
answer: the run ended `aborted=False`, with no diff and no gate fired.

Founding instance — lpe-rope-calc-implement-strict-flag, 2026-08-10, six
identical runs (`acceptance/pwir_ab_2026_08_10/STRICT-FLAG-FINDING.md`): three
tool calls absorbed the whole 3-file repo, the model then produced 36,838 chars
of planning monologue, generated exactly 8,192 completion tokens (the cap), and
was cut off at "construct a `PEInfo". Scored 1/5 for producing no diff while
reporting a clean completion.

Verified against the live endpoint before this was written: oMLX returns
`finish_reason='length'` when a generation is capped, and `Backend` propagates
it — so unlike an earlier switch, this one can actually fire.

Default OFF: with the switch unset the loop must behave exactly as before.
"""

from __future__ import annotations

from typing import Any

import pytest

from luxe.agents.guardrails import _TRUNCATED_TURN_MAX_RETRIES
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
                text="", finish_reason="stop",
                timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))
        return self._scripted.pop(0)


def _role(max_steps: int = 30) -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=max_steps,
                      max_tokens_per_turn=8192, temperature=0.0)


def _truncated(text: str = "I will now plan the refactor" * 20) -> ChatResponse:
    """A capped, mid-prose response: no tool calls, finish_reason='length'."""
    return ChatResponse(
        text=text, finish_reason="length",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=8192))


def _stopped(text: str = "done") -> ChatResponse:
    """A model that CHOSE to stop — must never be retried."""
    return ChatResponse(
        text=text, finish_reason="stop",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=50))


def _edit() -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="e", name="edit_file",
                                     arguments={"path": "pe_scan.py",
                                                "old": "a", "new": "b"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=200))


def _tools():
    return [ToolDef(name="edit_file", description="edit",
                    parameters={"type": "object",
                                "properties": {"path": {"type": "string"},
                                               "old": {"type": "string"},
                                               "new": {"type": "string"}},
                                "required": ["path", "old", "new"]})]


def _run(scripted):
    backend = _ScriptedBackend(list(scripted))
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="add a --strict flag",
        tool_defs=_tools(),
        tool_fns={"edit_file": lambda args: ("patched", None)},
    )
    return backend, result


def _nudges(backend) -> list[dict]:
    """Truncated-turn nudges in the final messages snapshot.

    Counted from the LAST snapshot rather than deduplicated across all of them:
    successive nudges are byte-identical, so any dedup silently collapses two
    firings into one and the bounded-retry test passes vacuously.
    """
    if not backend.calls:
        return []
    return [m for m in backend.calls[-1]
            if m.get("_luxe_nudge_type") == "truncated_turn"]


class TestDefaultOffIsUnchanged:
    def test_truncated_turn_is_terminal_by_default(self, monkeypatch):
        monkeypatch.delenv("LUXE_TRUNCATED_TURN_RETRY", raising=False)
        backend, result = _run([_truncated(), _edit()])
        # One chat, then the loop ends on the tool-call-free response — the
        # scripted edit is never reached. This is the pre-fix behaviour.
        assert len(backend.calls) == 1
        assert result.tool_calls_total == 0
        assert _nudges(backend) == []

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", "2", " 1"])
    def test_only_the_exact_string_one_enables_it(self, monkeypatch, value):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", value)
        backend, _ = _run([_truncated(), _edit()])
        assert len(backend.calls) == 1


class TestEnabled:
    def test_truncated_turn_is_nudged_and_the_model_can_recover(self,
                                                                monkeypatch):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        backend, result = _run([_truncated(), _edit(), _stopped()])
        assert len(backend.calls) == 3, "should continue past the cut-off turn"
        assert result.tool_calls_total == 1, "the edit must actually land"
        assert len(_nudges(backend)) == 1

    def test_the_cut_off_text_is_preserved_in_the_transcript(self, monkeypatch):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        backend, _ = _run([_truncated("half a thought"), _edit(), _stopped()])
        replayed = [m for s in backend.calls for m in s
                    if m.get("role") == "assistant"
                    and m.get("content") == "half a thought"]
        assert replayed, "the truncated text must survive into the history"

    def test_the_nudge_carries_the_compaction_markers(self, monkeypatch):
        # TieredCompact identifies droppable nudges by marker, never by body
        # text (agents.sdd). An unmarked nudge is uncompactable.
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        backend, _ = _run([_truncated(), _edit(), _stopped()])
        n = _nudges(backend)[0]
        assert n["_luxe_nudge"] is True
        assert n["_luxe_nudge_type"] == "truncated_turn"

    def test_retries_are_bounded(self, monkeypatch):
        """A model that ignores the nudge must not loop forever."""
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        backend, result = _run([_truncated()] * 10)
        # 1 initial + _TRUNCATED_TURN_MAX_RETRIES continuations, then terminal.
        assert len(backend.calls) == 1 + _TRUNCATED_TURN_MAX_RETRIES
        assert len(_nudges(backend)) == _TRUNCATED_TURN_MAX_RETRIES

    def test_a_model_that_chose_to_stop_is_never_retried(self, monkeypatch):
        """finish_reason='stop' is a real answer — retrying it would be a
        behaviour change on every ordinary completion in the suite."""
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        backend, _ = _run([_stopped(), _edit()])
        assert len(backend.calls) == 1
        assert _nudges(backend) == []

    @pytest.mark.parametrize("reason", ["", "content_filter", "unknown", "STOP"])
    def test_only_length_triggers_it(self, monkeypatch, reason):
        """An unknown finish_reason is not evidence of truncation."""
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        resp = ChatResponse(
            text="something", finish_reason=reason,
            timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))
        backend, _ = _run([resp, _edit()])
        assert len(backend.calls) == 1
        assert _nudges(backend) == []

    def test_a_truncated_turn_that_still_made_a_tool_call_is_untouched(
            self, monkeypatch):
        """Truncation mid-tool-call still dispatches; the guard must not fire
        and add a nudge on top of a turn that already acted."""
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        capped_but_acted = ChatResponse(
            text="",
            tool_calls=[ToolCallResponse(id="e", name="edit_file",
                                         arguments={"path": "p", "old": "a",
                                                    "new": "b"})],
            finish_reason="length",
            timing=GenerationTiming(prompt_tokens=100, completion_tokens=8192))
        backend, result = _run([capped_but_acted, _stopped()])
        assert result.tool_calls_total == 1
        assert _nudges(backend) == []
