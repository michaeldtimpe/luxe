"""Post-write idle streak: counting REPEAT calls (LUXE_POST_WRITE_IDLE_REPEATS).

`PostWriteIdleExitGuard`'s docstring always claimed it caught non-write calls
that "hit the dedup short-circuit". But `read_file` is in
`_DEDUP_EXEMPT_TOOLS`, so a repeated read returns its full content, the streak
resets, and the guard never arms. `consecutive_repeat` is blind to it for the
same reason — that exemption.

Demonstrated on m1, 2026-08-10 (Qwen3.6-35B-A3B-4bit, `luxe smoke --code`):
step 1 reads key f0ee19e9; after the edit at step 9, step 10 reads the SAME
key again, recorded dup=False with bytes=78 — so the streak reset. It is a
latent gap rather than the cause of that drill's abort, which was a step-budget
problem fixed separately. The `_m1_shape` fixture below is a MINIMAL
reproduction of the repeated-read pattern, not a replay of that trajectory.

The switch is opt-in and default OFF. With it unset the loop must behave
exactly as before — this is the benchmark path, and no maintain_suite run has
sanctioned a default-ON change.

Helpers are defined locally rather than imported from another test module; no
test in this suite imports another, and this one should not start.
"""

from __future__ import annotations

from typing import Any

import pytest

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


def _make_role(max_steps: int = 30) -> RoleConfig:
    return RoleConfig(model_key="test", num_ctx=4096, max_steps=max_steps,
                      max_tokens_per_turn=2048, temperature=0.0)


def _terminal_resp() -> ChatResponse:
    return ChatResponse(
        text="done", finish_reason="stop",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=100))


def _write_resp() -> ChatResponse:
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(
            id="w", name="write_file",
            arguments={"path": "calc.py", "content": "x"})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=200))


def _read_resp(path: str, call_id: str) -> ChatResponse:
    """A read_file call whose result is real content — NOT zero bytes."""
    return ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id=call_id, name="read_file",
                                     arguments={"path": path})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=40))


def _write_tool() -> ToolDef:
    return ToolDef(
        name="write_file", description="write",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"},
                                   "content": {"type": "string"}},
                    "required": ["path", "content"]})


def _read_tool() -> ToolDef:
    return ToolDef(
        name="read_file", description="read",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"]})


def _m1_shape() -> list[ChatResponse]:
    """write, then the same file read repeatedly — minimal repro of the
    blind spot, NOT a replay of m1 (see the module docstring)."""
    return [
        _write_resp(),
        *[_read_resp("calc.py", f"r{i}") for i in range(6)],
        _terminal_resp(),
    ]


def _run(scripted):
    backend = _ScriptedBackend(list(scripted))
    result = run_agent(
        backend=backend, role_cfg=_make_role(max_steps=30),
        system_prompt="sys", task_prompt="fix the bug",
        tool_defs=[_write_tool(), _read_tool()],
        tool_fns={
            "write_file": lambda args: ("ok", None),
            # Non-empty on purpose: a repeat is not a 0-byte call, which is
            # exactly why the original guard could not see this shape.
            "read_file": lambda args: ("def add(a, b):\n    return a + b\n",
                                       None),
        },
    )
    return backend, result


class TestDefaultOffIsUnchanged:
    def test_repeated_reads_do_not_arm_the_guard_by_default(self, monkeypatch):
        monkeypatch.delenv("LUXE_POST_WRITE_IDLE_REPEATS", raising=False)
        backend, result = _run(_m1_shape())
        # Every read runs and the terminal response ends it: the guard never
        # fired, exactly as before this switch existed.
        assert len(backend.calls) == 8
        assert result.tool_calls_total == 7

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", "2", " 1"])
    def test_only_the_exact_string_one_enables_it(self, monkeypatch, value):
        monkeypatch.setenv("LUXE_POST_WRITE_IDLE_REPEATS", value)
        backend, _ = _run(_m1_shape())
        assert len(backend.calls) == 8


class TestEnabled:
    def test_repeated_reads_exit_cleanly(self, monkeypatch):
        monkeypatch.setenv("LUXE_POST_WRITE_IDLE_REPEATS", "1")
        backend, result = _run(_m1_shape())
        # write, then read#1 is novel (resets), and reads 2/3/4 are repeats —
        # the third arms the guard. 5 chats: write + 4 reads.
        assert len(backend.calls) == 5, f"got {len(backend.calls)}"
        assert result.aborted is False, "post-write idle exit is a CLEAN exit"
        assert result.tool_calls_total == 5

    def test_a_novel_read_still_resets_the_streak(self, monkeypatch):
        """Legitimate exploration after a write must not be cut short."""
        monkeypatch.setenv("LUXE_POST_WRITE_IDLE_REPEATS", "1")
        backend, result = _run([
            _write_resp(),
            _read_resp("a.py", "r1"),
            _read_resp("a.py", "r2"),   # repeat 1
            _read_resp("a.py", "r3"),   # repeat 2
            _read_resp("b.py", "r4"),   # NOVEL -> resets
            _read_resp("c.py", "r5"),   # NOVEL -> resets
            _terminal_resp(),
        ])
        assert len(backend.calls) == 7, "novel reads must not trip the guard"
        assert result.aborted is False

    def test_streak_only_counts_after_a_write(self, monkeypatch):
        monkeypatch.setenv("LUXE_POST_WRITE_IDLE_REPEATS", "1")
        backend, result = _run([
            *[_read_resp("calc.py", f"r{i}") for i in range(5)],
            _terminal_resp(),
        ])
        assert len(backend.calls) == 6, "no write yet -> guard must not arm"
        assert result.aborted is False
