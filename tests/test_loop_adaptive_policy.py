"""v1.11 Phase 1 — adaptive-policy substrate integration test.

End-to-end verification of the loop.py wiring:
  - disable-equivalence: LUXE_ADAPTIVE_POLICY unset → no adaptive_state
    events emitted; v1.10.5 behavior preserved
  - enabled: LUXE_ADAPTIVE_POLICY=1 → adaptive_state events emitted each
    step with the signal values + convergence_score
  - per-signal ablation: LUXE_ADAPTIVE_NO_WRITE=0 → consecutive_no_write
    field is None in emitted events (other signal still active)
"""
from __future__ import annotations

from typing import Any

import luxe.agents.loop as loop_module
from luxe.agents.loop import run_agent
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef


class _ScriptedBackend:
    def __init__(self, scripted: list[ChatResponse]) -> None:
        self._scripted = list(scripted)

    def chat(self, messages, **kwargs) -> ChatResponse:
        if not self._scripted:
            return ChatResponse(text="", finish_reason="stop",
                                timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))
        return self._scripted.pop(0)


def _read_resp() -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="c", name="read_file", arguments={"path": "x.py"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=100),
    )


def _terminal() -> ChatResponse:
    return ChatResponse(text="done", finish_reason="stop",
                        timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))


def _role() -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=10,
                      max_tokens_per_turn=2048, temperature=0.0)


def _read_tool() -> ToolDef:
    return ToolDef(
        name="read_file", description="read",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )


def _read_fn() -> dict[str, Any]:
    return {"read_file": lambda args: (f"contents of {args.get('path', '')}", None)}


def _capture_events(monkeypatch):
    """Patch append_event in loop module; return list captured calls land in."""
    captured: list[dict] = []

    def fake_append(run_id, kind, **fields):
        captured.append({"kind": kind, **fields})

    monkeypatch.setattr(loop_module, "append_event", fake_append)
    return captured


def test_adaptive_policy_disabled_emits_no_adaptive_state(monkeypatch):
    """LUXE_ADAPTIVE_POLICY unset → zero adaptive_state events.
    Disable-equivalence invariant from agents.sdd."""
    monkeypatch.delenv("LUXE_ADAPTIVE_POLICY", raising=False)
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp() for _ in range(4)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=_role(),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-adaptive-policy",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    assert adaptive_events == []


def test_adaptive_policy_enabled_emits_per_step(monkeypatch):
    """LUXE_ADAPTIVE_POLICY=1 → adaptive_state event each step with
    consecutive_no_write + score_trend + score_log_len + convergence_score."""
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp() for _ in range(4)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=_role(),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-adaptive-policy",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    assert len(adaptive_events) >= 4  # one per loop step before terminal
    sample = adaptive_events[0]
    for k in ("consecutive_no_write", "score_trend", "score_log_len", "convergence_score", "step"):
        assert k in sample, f"missing field {k} in adaptive_state event"

    # consecutive_no_write should grow monotonically across the 4 reads.
    no_writes = [e["consecutive_no_write"] for e in adaptive_events]
    assert no_writes[-1] > no_writes[0]


def test_adaptive_policy_ablation_no_write_off(monkeypatch):
    """LUXE_ADAPTIVE_NO_WRITE=0 → that signal returns None; others active."""
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_ADAPTIVE_NO_WRITE", "0")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp() for _ in range(3)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=_role(),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-adaptive-policy",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    assert len(adaptive_events) >= 3
    for e in adaptive_events:
        assert e["consecutive_no_write"] is None
        # score_trend should still be present (could be 0.0 with short log)
        assert "score_trend" in e


def test_adaptive_policy_ablation_score_trend_off(monkeypatch):
    monkeypatch.setenv("LUXE_ADAPTIVE_POLICY", "1")
    monkeypatch.setenv("LUXE_ADAPTIVE_SCORE_TREND", "0")
    monkeypatch.setenv("LUXE_LOG_TOOL_CALLS", "1")
    events = _capture_events(monkeypatch)

    scripted = [_read_resp() for _ in range(3)] + [_terminal()]
    run_agent(
        backend=_ScriptedBackend(scripted), role_cfg=_role(),
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
        run_id="test-adaptive-policy",
    )

    adaptive_events = [e for e in events if e["kind"] == "adaptive_state"]
    assert len(adaptive_events) >= 3
    for e in adaptive_events:
        assert e["score_trend"] is None
        assert e["consecutive_no_write"] is not None
