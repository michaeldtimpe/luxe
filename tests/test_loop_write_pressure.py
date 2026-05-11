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
    _WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE,
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


def _verify_resp(name: str = "lint") -> ChatResponse:
    """A response that emits one tool call that returns 0 bytes."""
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="v", name=name, arguments={})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=50),
    )


def _write_resp() -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="w", name="write_file",
                                     arguments={"path": "out.md", "content": "x"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=200),
    )


def _zero_byte_tool(name: str) -> ToolDef:
    return ToolDef(
        name=name, description=name,
        parameters={"type": "object", "properties": {}, "required": []},
    )


def _write_tool() -> ToolDef:
    return ToolDef(
        name="write_file", description="write",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]},
    )


def test_post_write_idle_exits_after_three_zero_byte_calls():
    """Once a write succeeds, three back-to-back 0-byte verification calls
    trigger a clean exit (not aborted). Targets the qwen3-coder-next-80b
    post-success drift pattern observed in the m5max_moe bake-off."""
    # write → lint(0B) → bash(0B) → typecheck(0B) → terminal
    # The third 0-byte call after the write should trigger the exit.
    scripted = [
        _write_resp(),
        _verify_resp("lint"),
        _verify_resp("bash"),
        _verify_resp("typecheck"),
        # If we get here, exit didn't fire — would emit more.
        _verify_resp("security_scan"),
        _terminal_resp(),
    ]
    backend = _ScriptedBackend(scripted)
    role = _make_role(max_steps=30)

    tool_fns = {
        "write_file": lambda args: ("ok", None),
        "lint": lambda args: ("", None),
        "bash": lambda args: ("", None),
        "typecheck": lambda args: ("", None),
        "security_scan": lambda args: ("", None),
    }
    tool_defs = [
        _write_tool(),
        _zero_byte_tool("lint"),
        _zero_byte_tool("bash"),
        _zero_byte_tool("typecheck"),
        _zero_byte_tool("security_scan"),
    ]

    result = run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=tool_defs, tool_fns=tool_fns,
    )

    # The exit should fire after the third 0-byte call (step 3, 0-indexed).
    # That's 4 backend chat calls: write + 3 verifications.
    assert len(backend.calls) == 4, f"expected 4 chats, got {len(backend.calls)}"
    assert result.aborted is False, "post-write idle exit must NOT be marked aborted"
    assert result.tool_calls_total == 4  # write + 3 verifications


def test_post_write_idle_resets_on_productive_tool():
    """A non-zero-byte tool call resets the idle counter — only consecutive
    0-byte calls trigger the exit. (Uses unique tool names per call to
    sidestep the older consecutive-repeat-steps bailout, which is
    upstream of this new exit and fires first on duplicate calls.)"""
    scripted = [
        _write_resp(),
        _verify_resp("lint"),           # 0B (1)
        _verify_resp("bash"),           # 0B (2)
        _verify_resp("read_file"),      # productive → reset
        _verify_resp("typecheck"),      # 0B (1)
        _verify_resp("security_scan"),  # 0B (2)
        _verify_resp("find_symbol"),    # 0B (3) → exit
        _terminal_resp(),
    ]
    backend = _ScriptedBackend(scripted)
    role = _make_role(max_steps=30)

    tool_fns = {
        "write_file": lambda args: ("ok", None),
        "lint": lambda args: ("", None),
        "bash": lambda args: ("", None),
        "typecheck": lambda args: ("", None),
        "security_scan": lambda args: ("", None),
        "find_symbol": lambda args: ("", None),
        "read_file": lambda args: ("useful content here", None),
    }
    tool_defs = [
        _write_tool(),
        _zero_byte_tool("lint"),
        _zero_byte_tool("bash"),
        _zero_byte_tool("typecheck"),
        _zero_byte_tool("security_scan"),
        _zero_byte_tool("find_symbol"),
        _zero_byte_tool("read_file"),
    ]

    result = run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=tool_defs, tool_fns=tool_fns,
    )

    assert len(backend.calls) == 7
    assert result.aborted is False
    assert result.tool_calls_total == 7


def test_post_write_idle_inactive_before_first_write():
    """The drift detector only arms after at least one successful write —
    a model doing exploratory 0-byte tool calls before any write should
    still be allowed to continue (write_pressure handles that failure
    mode). Uses unique tool names per call to avoid the older
    consecutive-repeat-steps bailout."""
    names = ["lint", "bash", "typecheck", "security_scan", "find_symbol"]
    scripted = [_verify_resp(n) for n in names] + [_terminal_resp()]
    backend = _ScriptedBackend(scripted)
    role = _make_role(max_steps=30)

    tool_fns = {n: lambda args: ("", None) for n in names}
    tool_defs = [_zero_byte_tool(n) for n in names]

    result = run_agent(
        backend=backend, role_cfg=role,
        system_prompt="sys", task_prompt="do work",
        tool_defs=tool_defs, tool_fns=tool_fns,
    )

    # All 5 verifications + 1 terminal = 6 chats. Exit must NOT fire.
    assert len(backend.calls) == 6
    assert result.aborted is False


def test_write_pressure_fires_on_tool_call_ceiling_even_with_low_completion(monkeypatch):
    """For tool-call-heavy models like qwen3-coder-next-80b (avg ~1855
    completion tokens vs qwen3.6-35b's ~3800), the original 4000-token
    gate never fires even when the model emits 30 reads with zero writes.
    The OR with `_WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE` catches that
    pathology by gating on tool-call count when completion stays low."""
    monkeypatch.setenv("LUXE_WRITE_PRESSURE", "1")
    # 15 reads × 100 completion tokens each = 1500 total — well below the
    # 4000 token threshold but above the 15-tool ceiling.
    scripted = [_read_resp(completion_tokens=100) for _ in range(15)] + [_terminal_resp()]
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
    assert len(pressure_msgs) == 1, (
        f"expected exactly 1 injection on the tool-ceiling branch, got {len(pressure_msgs)}"
    )


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
    # MAX_TOOLS_BEFORE_FIRE must be > MIN_TOOLS so the OR branch is
    # additive (catches the low-completion case) without overriding the
    # original min gate.
    assert _WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE > _WRITE_PRESSURE_MIN_TOOLS
    assert _WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE <= 25
