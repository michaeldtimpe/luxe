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

DEFAULT-ON since 2026-08-10, after the maintain_suite A/B took the suite
27/30 -> 30/30 with zero regressions
(`acceptance/truncated_turn_ab_2026_08_10/REPORT.md`). The switch now follows
the tiered_compact spelling: only the exact string "0" disables it, which is
the ablation path these tests pin.
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


def _run(scripted, on_notice=None):
    backend = _ScriptedBackend(list(scripted))
    result = run_agent(
        backend=backend, role_cfg=_role(),
        system_prompt="sys", task_prompt="add a --strict flag",
        tool_defs=_tools(),
        tool_fns={"edit_file": lambda args: ("patched", None)},
        on_notice=on_notice,
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


class TestDefaultOn:
    def test_a_truncated_turn_is_retried_with_no_env_set(self, monkeypatch):
        """The promoted default: unset means ON."""
        monkeypatch.delenv("LUXE_TRUNCATED_TURN_RETRY", raising=False)
        backend, result = _run([_truncated(), _edit(), _stopped()])
        assert len(backend.calls) == 3
        assert result.tool_calls_total == 1
        assert len(_nudges(backend)) == 1

    def test_exactly_zero_restores_the_old_terminal_behaviour(self,
                                                              monkeypatch):
        """The ablation path. One chat, then the loop ends on the tool-call-
        free response and the scripted edit is never reached — the pre-fix
        behaviour, preserved for ablation."""
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "0")
        backend, result = _run([_truncated(), _edit()])
        assert len(backend.calls) == 1
        assert result.tool_calls_total == 0
        assert _nudges(backend) == []

    @pytest.mark.parametrize("value", ["1", "", "no", "false", "true", "2"])
    def test_only_the_exact_string_zero_disables_it(self, monkeypatch, value):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", value)
        backend, _ = _run([_truncated(), _edit(), _stopped()])
        assert len(backend.calls) == 3, f"{value!r} must not disable the retry"


class TestEnabled:
    """Explicitly set to "1" — same as the default, kept explicit so these
    stay meaningful if the default ever moves again."""

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


class TestRetryBound:
    """`LUXE_TRUNCATED_TURN_MAX_RETRIES` (2026-08-11).

    Each retry is a full capped generation — ~2.5 min at 8,192 tokens on the
    champion. In a chat session the capped turn is often the model rambling
    rather than mid-edit, so the default 2 can spend ~8 minutes before the
    turn ends. The knob buys that back without giving up the mechanism (or
    the telemetry that tells the two apart)."""

    def test_the_bound_is_configurable(self, monkeypatch):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_MAX_RETRIES", "1")
        backend, _ = _run([_truncated()] * 10)
        assert len(backend.calls) == 2          # 1 initial + 1 retry
        assert len(_nudges(backend)) == 1

    def test_zero_never_fires_but_leaves_the_switch_on(self, monkeypatch):
        """Distinct from LUXE_TRUNCATED_TURN_RETRY=0 in the records: the
        ungated `terminal_turn_truncated` event still reports the mechanism as
        enabled, so an ablation and a tightened leash don't look alike."""
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_MAX_RETRIES", "0")
        backend, _ = _run([_truncated()] * 5)
        assert len(backend.calls) == 1
        assert _nudges(backend) == []

    def test_a_raised_bound_is_honoured(self, monkeypatch):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_MAX_RETRIES", "4")
        backend, _ = _run([_truncated()] * 10)
        assert len(backend.calls) == 5
        assert len(_nudges(backend)) == 4

    def test_a_malformed_bound_keeps_the_benched_default(self, monkeypatch):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_MAX_RETRIES", "lots")
        backend, _ = _run([_truncated()] * 10)
        assert len(backend.calls) == 1 + _TRUNCATED_TURN_MAX_RETRIES


class TestNotices:
    """`on_notice` — the loop says out loud that it is retrying.

    Before this, a retry was visible only in `events.jsonl`, which nobody
    reads mid-turn: the session showed a spinner and a climbing elapsed
    counter while the mechanism spent two more full generations. Display
    only — never consulted for a decision."""

    def test_a_retry_announces_itself(self, monkeypatch):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        seen: list[str] = []
        _run([_truncated(), _edit(), _stopped()], on_notice=seen.append)
        assert len(seen) == 1
        assert "cut off" in seen[0]
        assert "8,192-token cap" in seen[0]
        assert "1/2" in seen[0]                 # which retry, and of how many

    def test_each_retry_is_announced_then_the_ending_is_too(self, monkeypatch):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        seen: list[str] = []
        _run([_truncated()] * 10, on_notice=seen.append)
        # 2 retries + the terminal "ending the turn" line: the user learns the
        # turn stopped because it was cut off, not because it finished.
        assert len(seen) == 3
        assert "1/2" in seen[0] and "2/2" in seen[1]
        assert "ending the turn" in seen[2]
        assert "2 retries already used" in seen[2]

    def test_the_terminal_notice_fires_even_with_retries_disabled(self,
                                                                  monkeypatch):
        """The case the whole mechanism exists for: a turn that was CUT OFF and
        is being reported as an answer. Say so regardless of the switch."""
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "0")
        seen: list[str] = []
        _run([_truncated()], on_notice=seen.append)
        assert len(seen) == 1
        assert "without retrying" in seen[0]

    def test_an_ordinary_completion_says_nothing(self, monkeypatch):
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        seen: list[str] = []
        _run([_stopped()], on_notice=seen.append)
        assert seen == []

    def test_a_raising_callback_cannot_kill_the_run(self, monkeypatch):
        """Display-only means a broken front-end costs the notice, not the
        turn — the loop owns work the UI does not."""
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")

        def _boom(_text):
            raise RuntimeError("front-end is gone")

        backend, result = _run([_truncated(), _edit(), _stopped()],
                               on_notice=_boom)
        assert result.tool_calls_total == 1
        assert len(_nudges(backend)) == 1

    def test_notices_are_off_by_default(self, monkeypatch):
        """The benchmark/maintain path passes no callback and is unchanged."""
        monkeypatch.setenv("LUXE_TRUNCATED_TURN_RETRY", "1")
        backend, result = _run([_truncated(), _edit(), _stopped()])
        assert result.tool_calls_total == 1
