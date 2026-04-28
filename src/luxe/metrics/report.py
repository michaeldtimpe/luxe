"""Report generation — comparison tables and summaries."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from luxe.metrics.collector import RunMetrics
from luxe.pipeline.model import PipelineRun


def print_run_summary(run: PipelineRun, metrics: RunMetrics) -> None:
    """Print a formatted summary of a pipeline run."""
    console = Console()

    console.print(f"\n[bold]Pipeline Run: {run.id}[/]")
    console.print(f"Goal: {run.goal}")
    console.print(f"Type: {run.task_type} | Repo: {run.repo_path}")
    console.print(f"Status: {run.status.value} | Wall time: {run.total_wall_s:.1f}s")

    # Per-role table
    table = Table(title="Per-Role Metrics")
    table.add_column("Role", style="cyan")
    table.add_column("Model", style="dim")
    table.add_column("Wall (s)", justify="right")
    table.add_column("Prompt tok", justify="right")
    table.add_column("Comp tok", justify="right")
    table.add_column("tok/s", justify="right")
    table.add_column("Tools", justify="right")
    table.add_column("Pressure", justify="right")

    for role, data in metrics.per_role.items():
        table.add_row(
            role,
            data.get("model", "")[:30],
            f"{data['wall_s']:.1f}",
            str(data["prompt_tokens"]),
            str(data["completion_tokens"]),
            f"{data['decode_tok_per_s']:.1f}",
            str(data["tool_calls"]),
            f"{data['peak_context_pressure']:.2f}",
        )

    console.print(table)

    # Summary stats
    console.print(f"\nTotal tokens: {metrics.total_prompt_tokens + metrics.total_completion_tokens:,}")
    console.print(f"Total tool calls: {metrics.total_tool_calls}")
    console.print(f"Cache hit rate: {metrics.cache_hit_rate:.1%}")
    console.print(f"Peak context pressure: {metrics.peak_context_pressure:.2f}")
    console.print(f"Subtasks: {metrics.subtasks_done} done, {metrics.subtasks_blocked} blocked, "
                  f"{metrics.escalations} escalated")


def print_comparison(runs: list[tuple[str, RunMetrics]]) -> None:
    """Compare multiple runs side-by-side."""
    console = Console()

    table = Table(title="Run Comparison")
    table.add_column("Metric", style="cyan")
    for label, _ in runs:
        table.add_column(label, justify="right")

    rows = [
        ("Wall time (s)", lambda m: f"{m.total_wall_s:.1f}"),
        ("Total tokens", lambda m: f"{m.total_prompt_tokens + m.total_completion_tokens:,}"),
        ("Tool calls", lambda m: str(m.total_tool_calls)),
        ("Schema rejects", lambda m: str(m.total_schema_rejects)),
        ("Subtasks done", lambda m: str(m.subtasks_done)),
        ("Subtasks blocked", lambda m: str(m.subtasks_blocked)),
        ("Escalations", lambda m: str(m.escalations)),
        ("Cache hit rate", lambda m: f"{m.cache_hit_rate:.1%}"),
        ("Peak ctx pressure", lambda m: f"{m.peak_context_pressure:.2f}"),
    ]

    for label, fn in rows:
        table.add_row(label, *[fn(m) for _, m in runs])

    console.print(table)
