"""Replay a recorded luxe session through a backend, turn by turn.

Reads a sanitized luxe session JSONL (the same format Session.append
writes at ~/.luxe/sessions/<id>.jsonl) and walks the events for one
agent at a time. At each user turn we issue backend.chat() with the
conversation built up so far, then advance the conversation using the
*recorded* assistant + tool-result events — not the new response.

The point is to time the same conversation on different backends.
Letting agent paths diverge would conflate backend perf with
random-walk differences.

Each task is one user turn from the session. Per-turn metrics carry
TTFT and decode tok/s; structural similarity (did the new response
call the same tool?) goes into `details` for spot-checking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from benchmarks._common import Benchmark, Task, TaskResult
from harness.backends import ToolDef


REPLAY_INPUTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "results"
    / "ab_ollama_vs_llamacpp"
    / "replay_inputs"
)


def _load_session(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _agent_turns(events: list[dict[str, Any]], agent: str) -> list[dict[str, Any]]:
    """Filter events to one agent's transcript, preserving tool rounds."""
    return [e for e in events if e.get("agent") == agent]


def _build_conversation_up_to(
    events: list[dict[str, Any]], cutoff_index: int
) -> list[dict[str, Any]]:
    """Reconstruct an OpenAI-format messages list from session events
    [0..cutoff_index-1]. The cutoff is the index of the next user turn
    we're about to issue, so the conversation should not include it."""
    messages: list[dict[str, Any]] = []
    for ev in events[:cutoff_index]:
        role = ev.get("role")
        content = (ev.get("content") or "").strip()
        if role == "user":
            if content:
                messages.append({"role": "user", "content": content})
        elif role == "assistant":
            if content:
                messages.append({"role": "assistant", "content": content})
        elif role == "tool":
            # Collapse tool turns into a single assistant+tool pair so the
            # model sees the call → result shape it expects on next turn.
            name = ev.get("tool") or "?"
            args = ev.get("arguments") or {}
            result = ev.get("result") or ""
            err = ev.get("error")
            tool_content = result if not err else f"ERROR: {err}"
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"replay_{len(messages)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": f"replay_{len(messages) - 1}",
                "content": str(tool_content),
            })
    return messages


@dataclass
class LuxeReplay:
    name: str = "luxe_replay"
    needs_tools: bool = False  # tools come from the recorded transcript
    fixture: str = "all"  # filename stem under replay_inputs/, or "all"
    agent_filter: str | None = None  # restrict to one agent across fixtures

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        if not REPLAY_INPUTS_DIR.exists():
            return
        files = sorted(REPLAY_INPUTS_DIR.glob("*.jsonl"))
        if self.fixture != "all":
            files = [p for p in files if p.stem == self.fixture]
        emitted = 0
        for path in files:
            events = _load_session(path)
            agents = (
                {self.agent_filter}
                if self.agent_filter
                else {e["agent"] for e in events if e.get("agent")}
            )
            for agent in agents:
                turns = _agent_turns(events, agent)
                # Each user turn becomes one Task; idx is the absolute
                # event index in the per-agent stream, used to rebuild
                # the prefix conversation deterministically.
                for idx, ev in enumerate(turns):
                    if ev.get("role") != "user":
                        continue
                    prefix = _build_conversation_up_to(turns, idx)
                    new_user = (ev.get("content") or "").strip()
                    if not new_user:
                        continue
                    yield Task(
                        id=f"{path.stem}::{agent}::turn{idx}",
                        prompt=prefix + [{"role": "user", "content": new_user}],
                        reference={
                            "expected_next_tool": _next_tool_name(turns, idx),
                            "fixture": path.stem,
                            "agent": agent,
                        },
                        metadata={"prefix_len": len(prefix)},
                    )
                    emitted += 1
                    if limit and emitted >= limit:
                        return

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        # task.prompt is already a fully-formed messages list.
        if isinstance(task.prompt, list):
            return task.prompt
        return [{"role": "user", "content": str(task.prompt)}]

    def tool_defs(self) -> list[ToolDef]:
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        expected = (task.reference or {}).get("expected_next_tool")
        # tool_log isn't currently populated by the runner for non-tool
        # benches; fall back to scanning the completion text for tool
        # mentions or just record what we got.
        actual = None
        if tool_log:
            actual = tool_log[0].get("name") if tool_log else None
        passed = bool(completion.strip())
        score = 1.0 if passed else 0.0
        return TaskResult(
            task_id=task.id,
            completion=completion[:2000],
            passed=passed,
            score=score,
            details={
                "expected_next_tool": expected,
                "actual_first_tool": actual,
                "tool_match": (expected and actual and expected == actual) or False,
                "completion_chars": len(completion),
            },
        )


def _next_tool_name(turns: list[dict[str, Any]], from_idx: int) -> str | None:
    """The name of the first tool the original transcript called after
    the user turn at from_idx — used as a soft equivalence signal."""
    for ev in turns[from_idx + 1 :]:
        if ev.get("role") == "tool":
            return ev.get("tool")
        if ev.get("role") == "user":
            break
    return None
