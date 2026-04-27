"""Prefix-cache-decay benchmark — measures the oMLX SSD-KV-cache value prop.

Construct a luxe-shaped prefix (~1k system + ~2k tool schema + filler
"repo context" at three sizes: 4k / 16k / 32k tokens). For each
prefix size, issue N identical-prefix queries that vary only in the
last ~50 tokens. The first query is "cold" (cache miss); subsequent
queries should reuse the prefix's KV cache and have much lower TTFT.

Per-task records land in the standard JSONL with `metrics.throughput.
time_to_first_token_s` populated by `_common.run_benchmark()`. The
verdict script (`scripts/omlx_verdict.py`) post-processes those rows
to compute:

  ttft_cold[size]            = TTFT of the first query
  ttft_warm_median[size]     = median TTFT of queries 2..N
  cache_benefit_ratio[size]  = ttft_cold / ttft_warm_median

Restart-between-queries variant (the "SSD-cache survives process
death" test) is run by invoking `run_benchmark` with `--limit 1` in a
shell loop that re-launches the server between each call. Measuring
that here would require teardown control the Benchmark protocol
doesn't have.

Task IDs are `<size>_q<idx>`; the verdict script groups by the size
prefix and orders by idx for warm/cold computation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from benchmarks._common import Benchmark, Task, TaskResult
from harness.backends import ToolDef


# Prefix sizes (in approximate tokens — qwen2.5 tokenizer is ~1.3 chars/tok
# for English prose; the filler body is hand-tuned to land near each named
# token budget).
_PREFIX_SIZES = ("small_4k", "medium_16k", "large_32k")
_QUERIES_PER_SIZE = 10

_SYSTEM_PROMPT = (
    "You are a senior engineer working inside a large code review "
    "session. The user will paste a long block of repository context, "
    "then ask short, targeted questions about it. Your job is to "
    "answer concisely, citing specific symbols when relevant. Avoid "
    "speculating beyond what the provided context supports. Return at "
    "most three sentences per answer; if a longer answer is genuinely "
    "needed, ask the user to clarify scope first. " * 3
)

# A pretend ~2k-token tools schema block. Real tools surfaces in luxe
# look like this: a JSON-shaped sketch, plus prose protocol notes. We
# don't actually serve tools to the model here — the goal is to inflate
# the prefix to a realistic luxe shape, not to test tool-calling.
_TOOLS_SCHEMA_BLOB = (
    "Available tools (read-only summary; do not call):\n\n"
    + "\n".join(
        f"- tool_{i:02d}(arg1: string, arg2: int = {i}): description of "
        f"tool {i}'s purpose, with notes on when to call it and what "
        f"shape the return value takes. Includes warnings about "
        f"common mistakes such as forgetting to validate the input "
        f"before passing it to downstream consumers."
        for i in range(40)
    )
)

# Filler "repo context" — chunked prose at three sizes. Hand-tuned for
# the qwen2.5 tokenizer (~1.3 chars/tok for English prose).
_FILLER_BODY = (
    "The codebase is organized as a Python package with submodules for "
    "each functional area. Each submodule exports a small public surface "
    "(a class plus a few free functions) and keeps its implementation "
    "details private behind underscore-prefixed names. Test coverage is "
    "uneven — utility code is well-covered, but the larger orchestration "
    "modules rely on integration tests that are slow and occasionally "
    "flaky on busy hosts. Configuration is loaded from a YAML file at "
    "startup; runtime overrides come in via environment variables, with "
    "the env values taking precedence. Logging uses the stdlib `logging` "
    "module and a JSON formatter; per-module log levels are tuned in the "
    "config file rather than at the call site. Errors are categorized "
    "into recoverable (retried with exponential backoff) and "
    "unrecoverable (logged + raised). The data layer abstracts over "
    "three storage backends behind a single interface; only one is used "
    "in production today, but the others remain available for tests. "
)

# Repetition counts hand-tuned so the resulting prefix lands near the
# named token budget for the qwen2.5 tokenizer.
_FILLER_REPS = {
    "small_4k": 14,
    "medium_16k": 56,
    "large_32k": 120,
}

# Distinct trailing queries (the only thing that varies across the N
# requests for a given prefix size). Kept short (~50 tokens each) so
# the prefix dominates the input length.
_QUERIES = [
    "Summarize the role of the configuration loader in two sentences.",
    "What is the recovery strategy for transient errors? Be concise.",
    "Which storage backends does the data layer support?",
    "Where do environment-variable overrides take effect, and at what precedence?",
    "How is logging configured, and where do per-module levels live?",
    "Describe the public surface of a typical submodule.",
    "What is the test-coverage profile of the larger orchestration modules?",
    "How are recoverable vs unrecoverable errors handled differently?",
    "Which serialization format is used for the configuration file?",
    "Name two characteristics of the integration test suite.",
]


def _prefix_for(size: str) -> str:
    body = _FILLER_BODY * _FILLER_REPS[size]
    return (
        f"Repository context follows. Read it carefully before answering.\n\n"
        f"{body}"
    )


@dataclass
class PrefixCacheDecay:
    name: str = "prefix_cache_decay"
    needs_tools: bool = False

    def tasks(self, limit: int | None = None) -> Iterable[Task]:
        emitted = 0
        for size in _PREFIX_SIZES:
            for idx in range(_QUERIES_PER_SIZE):
                if limit is not None and emitted >= limit:
                    return
                yield Task(
                    id=f"{size}_q{idx:02d}",
                    prompt=_QUERIES[idx % len(_QUERIES)],
                    reference=None,
                    metadata={"size": size, "query_idx": idx},
                )
                emitted += 1

    def build_messages(self, task: Task) -> list[dict[str, Any]]:
        size = task.metadata.get("size", _PREFIX_SIZES[0])
        repo_context = _prefix_for(size)
        # Keep the prefix EXACTLY identical across the N queries for a
        # given size — only the user query at the very end differs. KV
        # cache hit depends on byte-for-byte match of the early tokens.
        return [
            {
                "role": "system",
                "content": _SYSTEM_PROMPT + "\n\n" + _TOOLS_SCHEMA_BLOB,
            },
            {
                "role": "user",
                "content": (
                    f"{repo_context}\n\n"
                    f"---\n\n"
                    f"Question: {task.prompt if isinstance(task.prompt, str) else ''}"
                ),
            },
        ]

    def tool_defs(self) -> list[ToolDef]:
        return []

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult:
        # No correctness signal — the metric is TTFT, post-processed by
        # the verdict script. Mark passed=True so the bench doesn't
        # pollute pass-rate columns in mixed reports.
        return TaskResult(
            task_id=task.id,
            completion=completion,
            passed=True,
            score=1.0,
            details={
                "size": task.metadata.get("size"),
                "query_idx": task.metadata.get("query_idx"),
                "completion_chars": len(completion),
            },
        )
