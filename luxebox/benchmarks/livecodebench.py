"""LiveCodeBench runner (time-filtered).

LiveCodeBench publishes dated problems to mitigate training-set contamination.
We filter to problems released after a cutoff (default: 2025-01-01) so a
candidate trained earlier cannot have memorized solutions.

Evaluation uses the per-problem hidden tests (Python), which come with the
dataset. We execute each test with a short timeout.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from benchmarks._common import Benchmark, Task, TaskResult, extract_code_block
from harness.backends import ToolDef


@dataclass
class LiveCodeBench:
    name: str = "livecodebench"
    needs_tools: bool = False
    cutoff: date = date(2025, 1, 1)
    timeout_s: float = 15.0

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        from datasets import load_dataset
        from rich.console import Console

        console = Console()
        try:
            ds = load_dataset(
                "livecodebench/code_generation_lite",
                split="test",
                trust_remote_code=True,
            )
        except Exception as e:  # noqa: BLE001
            # datasets>=4 dropped script-loader support; LCB publishes via a
            # loading script. Until we wire a direct GitHub-release fetch,
            # yield nothing and let the rest of Phase A continue.
            console.log(
                f"[yellow]livecodebench: skipped ({type(e).__name__}: "
                f"{str(e).splitlines()[0][:120]}). See TODO in benchmarks/livecodebench.py."
            )
            return
        count = 0
        for row in ds:
            ts = row.get("contest_date") or row.get("date")
            if not ts:
                continue
            try:
                parsed = date.fromisoformat(ts[:10])
            except ValueError:
                continue
            if parsed < self.cutoff:
                continue
            yield Task(
                id=row["question_id"],
                prompt=row["question_content"],
                reference={
                    "starter_code": row.get("starter_code", ""),
                    "public_tests": row.get("public_test_cases", []),
                    "private_tests": row.get("private_test_cases", []),
                    "fn_name": row.get("metadata", {}).get("func_name"),
                },
            )
            count += 1
            if limit and count >= limit:
                return

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        starter = task.reference.get("starter_code") or ""
        user = task.prompt
        if starter:
            user = f"{task.prompt}\n\nStarter code:\n```python\n{starter}\n```"
        return [
            {
                "role": "system",
                "content": (
                    "You are a competitive-programming coder. Return a complete "
                    "Python solution in a single ```python code block. Read from "
                    "stdin if the problem says so; otherwise implement the "
                    "function in the starter code."
                ),
            },
            {"role": "user", "content": user},
        ]

    def tool_defs(self) -> list[ToolDef]:
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        code = extract_code_block(completion, "python")
        tests = (task.reference.get("public_tests") or []) + (
            task.reference.get("private_tests") or []
        )
        if not tests:
            return TaskResult(task_id=task.id, completion=code, passed=False, error="no tests")

        passed = 0
        for tc in tests:
            stdin = tc.get("input", "") if isinstance(tc, dict) else ""
            expected = (tc.get("output", "") if isinstance(tc, dict) else "").rstrip()
            ok = _exec_python(code, stdin, expected, timeout_s=self.timeout_s)
            passed += int(ok)

        total = len(tests)
        score = passed / total if total else 0.0
        return TaskResult(
            task_id=task.id,
            completion=code,
            passed=passed == total,
            score=score,
            details={"passed": passed, "total": total},
        )


def _exec_python(code: str, stdin: str, expected: str, timeout_s: float) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "sol.py"
        src.write_text(code)
        try:
            res = subprocess.run(  # noqa: S603
                [sys.executable, str(src)],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return False
        if res.returncode != 0:
            return False
        return res.stdout.rstrip() == expected.rstrip()
