"""τ-bench (tau-bench) multi-turn tool-use runner — skeleton.

tau-bench is a full agent-environment harness: user-simulator + tool-backed
environment + reward model. The recommended integration is to clone the
upstream repo and run our Backend inside its `Agent` interface.

This stub loads a tiny mocked task list so the smoke test can exercise the
multi-turn plumbing in `_common.run_benchmark`. Replace `_load_upstream`
with a real adapter once you've cloned https://github.com/sierra-research/tau-bench.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from benchmarks._common import Benchmark, Task, TaskResult
from harness.backends import ToolDef


@dataclass
class TauBench:
    name: str = "tau_bench"
    needs_tools: bool = True
    domain: str = "retail"  # retail | airline

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        for i, row in enumerate(_load_upstream(self.domain)):
            if limit and i >= limit:
                return
            yield Task(
                id=f"{self.domain}-{row['task_id']}",
                prompt=row["instruction"],
                reference={
                    "expected_actions": row["expected_actions"],
                    "tools": row["tools"],
                },
            )

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are an agent acting on behalf of a customer. Use the "
                    "provided tools to complete the request. Stop when done."
                ),
            },
            {"role": "user", "content": task.prompt},
        ]

    def tool_defs(self) -> list[ToolDef]:
        # Populated per-task; see _load_upstream.
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        expected = task.reference["expected_actions"]
        matched = sum(1 for exp in expected if _matches_any(exp, tool_log))
        total = len(expected)
        score = matched / total if total else 0.0
        return TaskResult(
            task_id=task.id,
            completion="",
            passed=score >= 0.95,
            score=score,
            details={"matched": matched, "total": total},
        )


def _load_upstream(domain: str) -> Iterable[dict[str, Any]]:
    """Stub that yields a single mock task so runner plumbing exercises end-to-end.

    TODO: replace with `from tau_bench.envs import get_env; ...` once the repo is
    cloned into the workspace.
    """
    yield {
        "task_id": "stub-001",
        "instruction": "Return your current account balance.",
        "expected_actions": [{"name": "get_balance", "arguments": {}}],
        "tools": [],
    }


def _matches_any(expected: dict[str, Any], actual_log: list[dict[str, Any]]) -> bool:
    for call in actual_log:
        if call.get("name") == expected.get("name"):
            if expected.get("arguments", {}) == call.get("arguments", {}):
                return True
    return False
