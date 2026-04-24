"""Decode-throughput microbenchmark — pure perf signal, no grading.

Three fixed prompts at three input sizes (~50 / 500 / 4000 tokens) each
asking for ~1024 tokens of straight prose continuation. The signal is
in `metrics.throughput`: TTFT, decode tok/s, prompt-prefill rate, peak
RSS at end of task.

Used by `scripts/run_ab_benchmark.py` to compare Ollama vs llama-server
on the same weights. `grade()` is a no-op (always passes) so the runner
records timing without trying to score correctness.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from benchmarks._common import Benchmark, Task, TaskResult
from harness.backends import ToolDef


# Three input sizes, hand-tuned to land near the named token budget under
# the qwen2.5 tokenizer (~1.3 chars/token for English prose).
_PROMPTS: dict[str, str] = {
    "small_50tok": (
        "Continue the following observation in clear, plain prose. Aim for "
        "around eight hundred words of continuation, then stop.\n\n"
        "The wind shifted unexpectedly that afternoon, carrying a cold edge "
        "down from the ridge."
    ),
    "medium_500tok": (
        "Continue the following short essay in clear, plain prose. Aim for "
        "around eight hundred words of continuation, then stop.\n\n"
        "There is a particular kind of attention that long-distance walking "
        "rewards. It is unhurried, but not lazy; it is observant, but not "
        "anxious. After the first hour or so, the body finds a rhythm and "
        "stops asking for accommodations, and the mind, freed from the "
        "small administrative work of pacing and posture, begins to wander "
        "in the way it does just before sleep — half attentive, half loose. "
        "Things in the periphery suddenly matter: the way a cottonwood "
        "rattles when wind moves only the topmost leaves, the smell of warm "
        "asphalt where the road bends south, the small administrative "
        "satisfaction of seeing a number on a milepost match the number you "
        "expected. None of this is profound on its own. The cumulative "
        "effect is something else. By the third or fourth hour, the world "
        "feels as though it has been organized for you in advance — every "
        "view a deliberate composition, every encounter a small scene with "
        "its own beginning and end. This is, of course, a trick of "
        "attention. The world has not been arranged. You have only stopped "
        "demanding things of it for a few hours, and what is left is what "
        "was always there. "
    ),
}

_LARGE_BODY = (
    "Local language models occupy a peculiar middle ground in the "
    "current technical landscape. They are not the frontier — that "
    "ground belongs to the largest closed models, trained at scales "
    "that no individual operator can replicate, and accessible only "
    "through APIs whose latency, cost, and policy posture are out of "
    "the operator's hands. They are not, either, the small embedded "
    "models that ship with consumer hardware and operate within a "
    "narrow prompt envelope. Local language models, in the sense "
    "this essay uses the term, are the family of open-weight models "
    "in roughly the seven-to-seventy-billion-parameter range that "
    "can be downloaded, quantized, and served on a developer's own "
    "machine. They are big enough to do real work — write code, "
    "answer questions with citations, hold a multi-turn "
    "conversation — but small enough that the operator owns every "
    "part of the stack. This combination is rarer than it sounds. "
    "Most of the technical infrastructure of the past two decades "
    "has either pushed compute outward toward a small number of "
    "hyperscalers or downward toward a large number of resource-"
    "constrained client devices. The local language model lives "
    "in a third place: a single capable machine, owned by one "
    "person, doing one operator's work. "
)

_PROMPTS["large_4000tok"] = (
    "Continue the following long essay in clear, plain prose. Aim for "
    "around eight hundred words of continuation, then stop.\n\n"
    + (_LARGE_BODY * 14)
    + "\n\nContinue from here:"
)


@dataclass
class DecodeThroughput:
    name: str = "decode_throughput"
    needs_tools: bool = False

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        items = list(_PROMPTS.items())
        if limit:
            items = items[:limit]
        for tid, prompt in items:
            yield Task(
                id=tid,
                prompt=prompt,
                reference=None,
                metadata={"size": tid.split("_")[0]},
            )

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": "Continue the prompt naturally."},
            {"role": "user", "content": task.prompt if isinstance(task.prompt, str) else ""},
        ]

    def tool_defs(self) -> list[ToolDef]:
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        # No grading axis — the timing fields in metrics carry the signal.
        # `passed=True` so this benchmark doesn't pollute the per-bench
        # pass-rate column in mixed reports.
        return TaskResult(
            task_id=task.id,
            completion=completion,
            passed=True,
            score=1.0,
            details={"completion_chars": len(completion)},
        )
