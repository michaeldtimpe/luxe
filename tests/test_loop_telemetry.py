"""Additive tool-call telemetry (2026-08-04 events.jsonl follow-up).

Three failure classes the C1 taxonomy could not measure now emit direct
records: `tool_reject` (reason=schema|unknown_tool) and `textfallback_drop`.
These tests pin two properties: (a) the events fire with the right fields,
and (b) the instrumentation never changes the model-visible conversation
or the loop's control flow — it is telemetry, not a gate.
"""

from __future__ import annotations

import pytest

import luxe.agents.loop as loop_mod
from luxe.agents.loop import _parse_text_tool_calls, run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef


class _ScriptedBackend:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append([dict(m) for m in messages])
        if not self._scripted:
            return ChatResponse(
                text="done", finish_reason="stop",
                timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))
        return self._scripted.pop(0)


def _role() -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=8,
                      max_tokens_per_turn=2048, temperature=0.0)


def _read_tool() -> ToolDef:
    return ToolDef(
        name="read_file", description="read",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]})


def _tools():
    return {"read_file": lambda args: (f"contents of {args.get('path', '')}", None)}


def _resp(text: str = "", tool_calls=None) -> ChatResponse:
    return ChatResponse(
        text=text, tool_calls=tool_calls or [],
        finish_reason="tool_calls" if tool_calls else "stop",
        timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))


@pytest.fixture
def events(monkeypatch):
    """Capture append_event calls without writing to the real ~/.luxe."""
    captured: list[tuple[str, dict]] = []

    def _capture(run_id, kind, **fields):
        captured.append((kind, fields))

    monkeypatch.setattr(loop_mod, "append_event", _capture)
    return captured


def _run(backend, run_id):
    return run_agent(backend=backend, role_cfg=_role(), system_prompt="s",
                     task_prompt="t", tool_defs=[_read_tool()],
                     tool_fns=_tools(), run_id=run_id)


class TestParseTextToolCallsDrops:
    def test_known_name_is_returned_and_not_dropped(self):
        drops: list[str] = []
        calls = _parse_text_tool_calls(
            '<tool_call>{"name": "read_file", "arguments": {"path": "x"}}</tool_call>',
            {"read_file"}, drops=drops)
        assert [c.name for c in calls] == ["read_file"]
        assert drops == []

    def test_unknown_name_is_dropped_and_collected(self):
        drops: list[str] = []
        calls = _parse_text_tool_calls(
            '<tool_call>{"name": "made_up", "arguments": {}}</tool_call>',
            {"read_file"}, drops=drops)
        assert calls == []
        assert drops == ["made_up"]

    def test_bare_json_unknown_name_is_collected(self):
        drops: list[str] = []
        calls = _parse_text_tool_calls(
            'I will call {"name": "fake_tool", "arguments": {}} now',
            {"read_file"}, drops=drops)
        assert calls == []
        assert "fake_tool" in drops

    def test_default_signature_behaves_as_before(self):
        calls = _parse_text_tool_calls(
            '<tool_call>{"name": "made_up", "arguments": {}}</tool_call>',
            {"read_file"})
        assert calls == []


class TestToolRejectEvents:
    def test_schema_reject_emits_tool_reject(self, events):
        bad = _resp(tool_calls=[
            ToolCallResponse(id="c", name="read_file", arguments={})])
        result = _run(_ScriptedBackend([bad]), "tel-test-1")
        rejects = [f for k, f in events if k == "tool_reject"]
        assert len(rejects) == 1
        assert rejects[0]["reason"] == "schema"
        assert rejects[0]["name"] == "read_file"
        assert "message" in rejects[0]
        # The counter the legacy proxy reads is unchanged.
        assert result.schema_rejects == 1

    def test_unknown_tool_dispatch_emits_tool_reject(self, events):
        bad = _resp(tool_calls=[
            ToolCallResponse(id="c", name="made_up", arguments={})])
        _run(_ScriptedBackend([bad]), "tel-test-2")
        rejects = [f for k, f in events if k == "tool_reject"]
        assert len(rejects) == 1
        assert rejects[0]["reason"] == "unknown_tool"
        assert rejects[0]["name"] == "made_up"
        assert rejects[0]["message"].startswith("Unknown tool")

    def test_happy_path_emits_no_tool_reject(self, events):
        good = _resp(tool_calls=[
            ToolCallResponse(id="c", name="read_file",
                             arguments={"path": "x.py"})])
        _run(_ScriptedBackend([good]), "tel-test-3")
        assert [k for k, _ in events if k == "tool_reject"] == []


class TestTextfallbackDropEvents:
    def test_dropped_candidate_emits_event_and_control_flow_unchanged(self, events):
        prose = _resp(
            text='<tool_call>{"name": "made_up", "arguments": {}}</tool_call>')
        result = _run(_ScriptedBackend([prose]), "tel-test-4")
        drops = [f for k, f in events if k == "textfallback_drop"]
        assert len(drops) == 1
        assert drops[0]["names"] == ["made_up"]
        assert drops[0]["recovered"] is False
        # Control flow unchanged: the loop still ends the run with the
        # prose as final text, exactly as before the instrumentation.
        assert result.final_text.startswith("<tool_call>")

    def test_recovered_call_emits_no_event(self, events):
        prose = _resp(
            text='<tool_call>{"name": "read_file", "arguments": {"path": "x"}}'
                 '</tool_call>')
        _run(_ScriptedBackend([prose]), "tel-test-5")
        assert [k for k, _ in events if k == "textfallback_drop"] == []

    def test_no_run_id_means_no_events_but_same_behavior(self, events):
        prose = _resp(
            text='<tool_call>{"name": "made_up", "arguments": {}}</tool_call>')
        result = run_agent(backend=_ScriptedBackend([prose]), role_cfg=_role(),
                           system_prompt="s", task_prompt="t",
                           tool_defs=[_read_tool()], tool_fns=_tools())
        assert [k for k, _ in events if k == "textfallback_drop"] == []
        assert result.final_text.startswith("<tool_call>")


class TestEmptyAndStepTextRecords(object):
    """`terminal_turn_empty` + `step_texts` (2026-08-17).

    Both exist because a record could not previously answer two questions:
    "did the model actually answer?" and "did it say anything before it
    acted?". Neither touches control flow.
    """

    def _empty(self, reasoning_chars=0):
        return ChatResponse(
            text="", finish_reason="stop", reasoning_chars=reasoning_chars,
            timing=GenerationTiming(prompt_tokens=10, completion_tokens=792))

    def test_a_terminal_empty_turn_is_recorded(self, events, monkeypatch):
        """Ungated: it fires in BOTH arms, and with the retry off it is the
        only trace that the run produced no answer."""
        monkeypatch.setenv("LUXE_EMPTY_TURN_RETRY", "0")
        _run(_ScriptedBackend([self._empty(reasoning_chars=3568)]), "r-empty")
        recs = [f for k, f in events if k == "terminal_turn_empty"]
        assert len(recs) == 1
        assert recs[0]["reasoning_chars"] == 3568
        assert recs[0]["retry_enabled"] is False

    def test_the_retry_fires_its_own_event_before_the_terminal_one(
            self, events, monkeypatch):
        monkeypatch.delenv("LUXE_EMPTY_TURN_RETRY", raising=False)
        backend = _ScriptedBackend([self._empty(), self._empty()])
        _run(backend, "r-empty2")
        assert [f for k, f in events if k == "empty_turn_retry"]
        terminal = [f for k, f in events if k == "terminal_turn_empty"]
        assert terminal and terminal[-1]["retry_enabled"] is True
        assert terminal[-1]["retries_used"] == 1

    def test_an_answered_turn_records_neither(self, events):
        _run(_ScriptedBackend([_resp("here you go")]), "r-ok")
        assert [k for k, _ in events if k == "terminal_turn_empty"] == []
        assert [k for k, _ in events if k == "empty_turn_retry"] == []

    def test_acting_step_prose_is_collected_separately(self, events):
        """The model speaks, then acts, then answers. `final_text` keeps its
        exact meaning; the lead-in lands in `step_texts` for the chat layer."""
        backend = _ScriptedBackend([
            _resp("Let me check the config first.",
                  tool_calls=[ToolCallResponse(id="1", name="read_file",
                                               arguments={"path": "a.py"})]),
            _resp("It sets the flag."),
        ])
        result = _run(backend, "r-steps")
        assert result.step_texts == ["Let me check the config first."]
        assert result.final_text == "It sets the flag."

    def test_a_silent_acting_step_contributes_nothing(self, events):
        backend = _ScriptedBackend([
            _resp("", tool_calls=[ToolCallResponse(id="1", name="read_file",
                                                   arguments={"path": "a.py"})]),
            _resp("done"),
        ])
        assert _run(backend, "r-silent").step_texts == []
