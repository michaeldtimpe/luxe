"""Tests for Mode B fix in src/luxe/agents/loop.py — mid-loop write-pressure
injection.

Targets the prose-mode trap observed on nothing-ever-happens-document-config
(v1.4.0 rep 1, 2026-05-03): agent issues many reads, generates significant
prose, never calls write_file. The fix injects a synthetic user message
once thresholds are crossed (tool calls + completion tokens + step number,
all with zero writes).

Off by default. Enabled via LUXE_WRITE_PRESSURE=1.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from luxe.agents.loop import (
    _WRITE_PRESSURE_MESSAGE,
    _WRITE_PRESSURE_MIN_STEP,
    _WRITE_PRESSURE_MIN_TOKENS,
    _WRITE_PRESSURE_MIN_TOOLS,
    run_agent,
)
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
from luxe.config import RoleConfig
from luxe.tools.base import ToolDef


class _ScriptedBackend:
    """Backend stub that yields a pre-scripted sequence of ChatResponses,
    capturing the messages list passed in on each call so assertions can
    inspect the conversation post-hoc.
    """

    def __init__(self, scripted: list[ChatResponse]) -> None:
        self._scripted = list(scripted)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        if not self._scripted:
            return ChatResponse(text="", finish_reason="stop",
                                timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))
        return self._scripted.pop(0)


def _read_resp(completion_tokens: int = 1500) -> ChatResponse:
    """A response that emits one read_file tool call."""
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="c", name="read_file", arguments={"path": "x.py"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=completion_tokens),
    )


def _terminal_resp() -> ChatResponse:
    """A response with no tool calls — ends the agent loop."""
    return ChatResponse(
        text="done",
        finish_reason="stop",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=100),
    )


def _make_role(max_steps: int = 30) -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=max_steps,
                      max_tokens_per_turn=2048, temperature=0.0)


def _read_tool() -> ToolDef:
    return ToolDef(
        name="read_file",
        description="read",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )


def _read_fn() -> dict[str, Any]:
    return {"read_file": lambda args: (f"contents of {args.get('path', '')}", None)}


def test_write_pressure_disabled_by_default(monkeypatch):
    """Without LUXE_WRITE_PRESSURE=1, no synthetic user message is injected
    even when the threshold conditions are met.
    """
    monkeypatch.delenv("LUXE_WRITE_PRESSURE", raising=False)
    # 11 read responses (above threshold) then terminal, with high tokens.
    scripted = [_read_resp(completion_tokens=500) for _ in range(11)] + [_terminal_resp()]
    backend = _ScriptedBackend(scripted)
    role = _make_role()

    result = run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
    )

    # Walk every messages snapshot — the synthetic message must never appear.
    for snapshot in backend.calls:
        for msg in snapshot:
            assert _WRITE_PRESSURE_MESSAGE not in str(msg.get("content", ""))
    assert result.tool_calls_total >= 11


def test_write_pressure_fires_when_thresholds_met(monkeypatch):
    """With LUXE_WRITE_PRESSURE=1 and N reads + M tokens past step K and
    zero writes, the synthetic user message lands exactly once.
    """
    monkeypatch.setenv("LUXE_WRITE_PRESSURE", "1")
    # Each read response carries enough completion tokens that 11 of them
    # easily clears the 4000-token threshold; step-count clears 5 quickly.
    scripted = [_read_resp(completion_tokens=500) for _ in range(15)] + [_terminal_resp()]
    backend = _ScriptedBackend(scripted)
    role = _make_role()

    run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
    )

    final_messages = backend.calls[-1]
    pressure_msgs = [
        m for m in final_messages
        if m.get("role") == "user" and _WRITE_PRESSURE_MESSAGE in str(m.get("content", ""))
    ]
    assert len(pressure_msgs) == 1, f"expected exactly 1 injection, got {len(pressure_msgs)}"


def test_write_pressure_fires_only_once(monkeypatch):
    """Across many subsequent turns past the threshold the injection still
    happens only once — it sets a flag on the run.
    """
    monkeypatch.setenv("LUXE_WRITE_PRESSURE", "1")
    scripted = [_read_resp(completion_tokens=500) for _ in range(20)] + [_terminal_resp()]
    backend = _ScriptedBackend(scripted)
    role = _make_role()

    run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
    )

    final_messages = backend.calls[-1]
    pressure_msgs = [
        m for m in final_messages
        if m.get("role") == "user" and _WRITE_PRESSURE_MESSAGE in str(m.get("content", ""))
    ]
    assert len(pressure_msgs) == 1


def test_write_pressure_does_not_fire_under_step_threshold(monkeypatch):
    """If the agent terminates before step >= MIN_STEP, no injection."""
    monkeypatch.setenv("LUXE_WRITE_PRESSURE", "1")
    # Only 3 reads (< 5 steps) — should not fire.
    scripted = [_read_resp(completion_tokens=2000) for _ in range(3)] + [_terminal_resp()]
    backend = _ScriptedBackend(scripted)
    role = _make_role()

    run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool()], tool_fns=_read_fn(),
    )

    for snapshot in backend.calls:
        for msg in snapshot:
            assert _WRITE_PRESSURE_MESSAGE not in str(msg.get("content", ""))


def test_write_pressure_does_not_fire_after_write(monkeypatch):
    """If the agent has already called write_file, the read-loop trap is
    not the failure mode — injection must not fire.
    """
    monkeypatch.setenv("LUXE_WRITE_PRESSURE", "1")

    write_resp = ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="w", name="write_file",
                                     arguments={"path": "out.md", "content": "x"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=500),
    )
    # Write at step 1, then 15 reads with high tokens, then terminal.
    scripted = [write_resp] + [_read_resp(completion_tokens=500) for _ in range(15)] + [_terminal_resp()]
    backend = _ScriptedBackend(scripted)
    role = _make_role()

    write_def = ToolDef(
        name="write_file", description="write",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]},
    )
    tool_fns = {
        "read_file": lambda args: ("x", None),
        "write_file": lambda args: ("ok", None),
    }

    run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=[_read_tool(), write_def], tool_fns=tool_fns,
    )

    for snapshot in backend.calls:
        for msg in snapshot:
            assert _WRITE_PRESSURE_MESSAGE not in str(msg.get("content", ""))


def test_write_pressure_threshold_constants_are_sensible():
    """Sanity-check the constants — guards against accidental edits that
    would make the gate fire too early or never. Values reflect the v1.4.0
    rep-1 trace: 17 calls, 9092 tokens — well above the MIN thresholds.
    """
    assert _WRITE_PRESSURE_MIN_TOOLS >= 5
    assert _WRITE_PRESSURE_MIN_TOOLS <= 20
    assert _WRITE_PRESSURE_MIN_TOKENS >= 1000
    assert _WRITE_PRESSURE_MIN_TOKENS <= 8000
    assert _WRITE_PRESSURE_MIN_STEP >= 3
    assert _WRITE_PRESSURE_MIN_STEP <= 10
