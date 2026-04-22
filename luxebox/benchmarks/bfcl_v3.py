"""BFCL v3 single-turn tool-call accuracy.

Uses the `bfcl-eval` package for the dataset + AST-equivalence grader, so our
job is to produce tool calls in OpenAI-compat format and hand the list back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from benchmarks._common import Benchmark, Task, TaskResult
from harness.backends import ToolDef


@dataclass
class BFCLv3:
    name: str = "bfcl_v3"
    needs_tools: bool = True
    category: str = "simple"  # simple | multiple | parallel | parallel_multiple

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        from bfcl_eval import dataset as bfcl_dataset

        rows = bfcl_dataset.load(self.category)
        if limit:
            rows = rows[:limit]
        for row in rows:
            yield Task(
                id=row["id"],
                prompt=row["question"],
                reference={
                    "expected": row["ground_truth"],
                    "functions": row["function"],
                },
                metadata={"category": self.category},
            )

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": "Call the most appropriate function(s) to answer the user.",
            },
            {"role": "user", "content": task.prompt if isinstance(task.prompt, str) else json.dumps(task.prompt)},
        ]

    def tool_defs(self) -> list[ToolDef]:
        # BFCL tool defs are per-task; returned via build-time override.
        # The runner will pass task.reference["functions"] through when calling chat.
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        from bfcl_eval import grader

        # tool_log is passed by the runner as the list of ToolCall dicts.
        calls = [
            {"name": c.get("name"), "arguments": c.get("arguments", {})} for c in tool_log
        ]
        verdict = grader.ast_equivalent(
            predicted=calls,
            expected=task.reference["expected"],
            functions=task.reference["functions"],
            category=self.category,
        )
        return TaskResult(
            task_id=task.id,
            completion=json.dumps(calls),
            passed=bool(verdict["correct"]),
            score=float(verdict["correct"]),
            details=verdict,
        )
