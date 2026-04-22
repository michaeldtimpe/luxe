"""SWE-bench Lite runner — skeleton.

Proper SWE-bench evaluation requires the upstream harness
(https://github.com/princeton-nlp/SWE-bench) to build per-task Docker images
and run the instance's test suite inside. That's heavy to set up, so we keep
this as a skeleton that:

1. Loads the 50-task Python subset (`princeton-nlp/SWE-bench_Lite`).
2. Feeds the issue + repo state to the model and captures the proposed patch.
3. Writes candidate patches to `results/swebench_lite/<candidate>/patches/`
   where the upstream harness can pick them up:

       python -m swebench.harness.run_evaluation \
           --predictions_path results/.../patches.jsonl \
           --run_id luxebox

We deliberately don't shell out to Docker from the harness — you should run
the upstream evaluator yourself and feed the resulting report back via
`scripts/import_swebench.py` (not yet implemented).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from benchmarks._common import Benchmark, Task, TaskResult
from harness.backends import ToolDef


@dataclass
class SWEBenchLite:
    name: str = "swebench_lite"
    needs_tools: bool = False
    limit_default: int = 50

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        from datasets import load_dataset

        ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
        n = min(limit or self.limit_default, self.limit_default)
        for i, row in enumerate(ds):
            if i >= n:
                return
            yield Task(
                id=row["instance_id"],
                prompt=row["problem_statement"],
                reference={
                    "repo": row["repo"],
                    "base_commit": row["base_commit"],
                    "test_patch": row["test_patch"],
                    "gold_patch": row["patch"],
                },
            )

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are a senior engineer resolving a GitHub issue. Produce a "
                    "unified diff that fixes the described problem. Output only the "
                    "diff inside a ```diff block."
                ),
            },
            {"role": "user", "content": task.prompt},
        ]

    def tool_defs(self) -> list[ToolDef]:
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        # Capture the patch; defer scoring to the upstream evaluator.
        out_dir = Path("results/swebench_lite_patches")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{task.id}.patch").write_text(completion)

        predictions_file = out_dir / "predictions.jsonl"
        with predictions_file.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "instance_id": task.id,
                        "model_name_or_path": "luxebox",
                        "model_patch": completion,
                    }
                )
                + "\n"
            )

        return TaskResult(
            task_id=task.id,
            completion=completion,
            passed=False,
            score=0.0,
            details={"captured_patch": True, "predictions_file": str(predictions_file)},
            error="upstream harness run required; passed=0 is a placeholder",
        )
