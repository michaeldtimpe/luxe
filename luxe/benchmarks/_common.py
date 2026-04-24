"""Shared Benchmark protocol and orchestration.

Every benchmark implements `Benchmark`. The runner resolves its task list,
skips already-completed tasks from the JSONL log (resumable), then invokes
the backend once per task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from harness import io
from harness.backends import Backend, ToolDef
from harness.metrics import RunMetrics

console = Console()


@dataclass
class Task:
    id: str
    prompt: str | list[dict[str, Any]]  # text prompt or full chat messages
    reference: Any = None  # grader-specific (test cases, canonical diff, etc.)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    task_id: str
    completion: str
    passed: bool
    score: float = 0.0  # 0-1; for graders that give partial credit
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


class Benchmark(Protocol):
    name: str
    needs_tools: bool

    def tasks(self, limit: int | None = None) -> Iterable[Task]: ...

    def build_messages(self, task: Task) -> list[dict[str, Any]]: ...

    def tool_defs(self) -> list[ToolDef]: ...  # [] if needs_tools is False

    def grade(self, task: Task, completion: str, tool_log: list[dict[str, Any]]) -> TaskResult: ...


def run_benchmark(
    bench: Benchmark,
    backend: Backend,
    *,
    phase: str,
    candidate_id: str,
    config_id: str,
    limit: int | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    extra_body: dict[str, Any] | None = None,
) -> list[TaskResult]:
    out_path = io.runs_path(phase, candidate_id, config_id, bench.name)
    done = io.completed_task_ids(out_path)

    # Attach the backend so benchmarks with compression stages that
    # need to call the model (e.g. summarize) can reach it via
    # getattr(self, "backend", None).
    try:
        bench.backend = backend  # type: ignore[attr-defined]
    except AttributeError:
        pass

    results: list[TaskResult] = []
    tasks = list(bench.tasks(limit=limit))
    remaining = [t for t in tasks if t.id not in done]

    console.log(
        f"[{bench.name}] {len(tasks)} tasks, {len(done)} already logged, running {len(remaining)}"
    )

    tools = bench.tool_defs() if bench.needs_tools else []

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
    ) as progress:
        bar = progress.add_task(f"{candidate_id} · {config_id} · {bench.name}", total=len(remaining))
        for task in remaining:
            metrics = RunMetrics(
                candidate_id=candidate_id,
                config_id=config_id,
                benchmark=bench.name,
                task_id=task.id,
            )
            metrics.known_tool_names = {t.name for t in tools}

            try:
                messages = bench.build_messages(task)
                response = backend.chat(
                    messages,
                    tools=tools or None,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    extra_body=extra_body,
                )
                metrics.record_turn(step=0, response=response)
                # Carry the server's peak RSS so far onto each task record.
                sampler = getattr(backend, "_rss_sampler", None)
                if sampler is not None:
                    metrics.peak_rss_bytes = sampler.peak_rss_bytes
                # Record the prompt size the backend actually saw — for
                # compression benchmarks this is the true context cost.
                if response.timing.prompt_tokens > metrics.peak_context_tokens:
                    metrics.peak_context_tokens = response.timing.prompt_tokens
                metrics.finish()

                graded = bench.grade(task, response.text, [])
            except Exception as e:  # noqa: BLE001
                metrics.finish()
                graded = TaskResult(
                    task_id=task.id,
                    completion="",
                    passed=False,
                    error=f"{type(e).__name__}: {e}",
                )

            # Mirror compression-benchmark fields from grader details
            # onto RunMetrics so the report layer can pivot on them
            # without cracking open the per-row details blob.
            _d = graded.details or {}
            for src, dst in (
                ("t_retrieval_s", "t_retrieval_s"),
                ("t_compression_s", "t_compression_s"),
                ("file_precision", "file_precision"),
                ("file_recall", "file_recall"),
            ):
                if src in _d:
                    setattr(metrics, dst, _d[src])

            graded.metrics = metrics.to_dict()
            io.append(
                out_path,
                {
                    "task_id": graded.task_id,
                    "passed": graded.passed,
                    "score": graded.score,
                    "error": graded.error,
                    "completion": graded.completion,
                    "details": graded.details,
                    "metrics": graded.metrics,
                },
            )
            results.append(graded)
            progress.advance(bar)

    # Fold previously-completed rows back in so callers see the full picture.
    for rec in io.read(out_path):
        if rec["task_id"] not in {r.task_id for r in results}:
            results.append(
                TaskResult(
                    task_id=rec["task_id"],
                    completion=rec.get("completion", ""),
                    passed=bool(rec.get("passed", False)),
                    score=float(rec.get("score", 0.0)),
                    error=rec.get("error"),
                    details=rec.get("details", {}),
                    metrics=rec.get("metrics", {}),
                )
            )
    return results


def extract_code_block(text: str, lang_hint: str | None = None) -> str:
    """Pull the first fenced code block out of a model response.

    Preserves per-line indentation (critical for Python body-only completions).
    Only strips surrounding newlines and trailing whitespace.
    """
    import re

    # Match the opening fence + optional language tag + optional trailing
    # horizontal whitespace + a newline. Crucially, we only eat `[ \t]`
    # after the language — `\s*` would also consume the leading indent of
    # the first code line (fatal for Python body-only completions).
    pattern = r"```(?:[\w+-]+)?[ \t]*\r?\n(.*?)```"
    matches = re.findall(pattern, text, flags=re.DOTALL)
    if matches:
        if lang_hint:
            for block in matches:
                if lang_hint.lower() in block.lower()[:80]:
                    return block.strip("\n").rstrip()
        return matches[0].strip("\n").rstrip()
    return text.strip("\n").rstrip()
