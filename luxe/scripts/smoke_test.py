"""Smoke test: runs the harness end-to-end against a mock backend.

Exercises the plumbing without touching any real model. If this passes, the
shapes of config → registry → benchmark → metrics → io → report are all
consistent.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks._common import Task, TaskResult, run_benchmark  # noqa: E402
from benchmarks._common import extract_code_block  # noqa: E402
from harness import report  # noqa: E402
from harness.backends import ToolDef  # noqa: E402
from harness.mock_backend import MockBackend  # noqa: E402


class _ToyBench:
    """In-memory benchmark with 3 tasks — no network, no grader deps."""

    name = "toy_bench"
    needs_tools = False

    def tasks(self, limit: int | None = None):
        for i in range(3):
            yield Task(id=f"toy-{i}", prompt=f"return {i}")

    def build_messages(self, task: Task):
        return [
            {"role": "system", "content": "Return Python that returns the number."},
            {"role": "user", "content": task.prompt},
        ]

    def tool_defs(self) -> list[ToolDef]:
        return []

    def grade(self, task: Task, completion: str, tool_log) -> TaskResult:
        body = extract_code_block(completion, "python")
        passed = "return" in body
        return TaskResult(
            task_id=task.id, completion=body, passed=passed, score=1.0 if passed else 0.0
        )


def main() -> int:
    # Clean stale smoke-test run so we re-exercise the writer path.
    runs_dir = ROOT / "results" / "runs" / "phase_a" / "mock-model" / "baseline"
    if runs_dir.exists():
        shutil.rmtree(runs_dir)

    backend = MockBackend(canned_text="```python\nreturn 1\n```")
    results = run_benchmark(
        _ToyBench(),
        backend,
        phase="phase_a",
        candidate_id="mock-model",
        config_id="baseline",
    )
    assert len(results) == 3, f"expected 3 results, got {len(results)}"
    assert all(r.passed for r in results), "toy bench should pass on canned 'return 1'"

    md = report.phase_a_report()
    assert "mock-model" in md, md

    print("✓ smoke test passed")
    print()
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
