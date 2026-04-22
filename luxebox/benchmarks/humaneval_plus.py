"""HumanEval+ runner with a self-contained grader."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from benchmarks._common import Benchmark, Task, TaskResult, extract_code_block
from benchmarks._eval_plus_grader import grade_eval_plus
from harness.backends import ToolDef


@dataclass
class HumanEvalPlus:
    name: str = "humaneval_plus"
    needs_tools: bool = False

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        from evalplus.data import get_human_eval_plus

        data = get_human_eval_plus()
        items = list(data.items())
        if limit:
            items = items[:limit]
        for tid, task in items:
            yield Task(
                id=tid,
                prompt=task["prompt"],
                reference=task,
                metadata={"entry_point": task["entry_point"]},
            )

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are a precise Python coder. Complete the function. "
                    "Return only a Python code block containing the function body "
                    "(or the full function, your choice) — no prose, no tests."
                ),
            },
            {"role": "user", "content": task.prompt},
        ]

    def tool_defs(self) -> list[ToolDef]:
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        code = extract_code_block(completion, "python")
        base_ok, plus_ok, err = grade_eval_plus(task.reference, code)
        if err:
            return TaskResult(
                task_id=task.id,
                completion=code,
                passed=False,
                score=0.0,
                error=f"grader: {err}",
            )
        return TaskResult(
            task_id=task.id,
            completion=code,
            passed=base_ok and plus_ok,
            score=(int(base_ok) + int(plus_ok)) / 2,
            details={"base": base_ok, "plus": plus_ok},
        )
