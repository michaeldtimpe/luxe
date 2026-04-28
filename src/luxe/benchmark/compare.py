"""Cross-config comparison — tables and reports from benchmark results."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from luxe.benchmark.runner import BenchmarkResult, BenchmarkSuite

console = Console()


def print_suite_summary(suite: BenchmarkSuite) -> None:
    """Print a full comparison of all configs across all tasks."""

    # Group results by config
    by_config: dict[str, list[BenchmarkResult]] = defaultdict(list)
    for r in suite.results:
        by_config[r.config_name].append(r)

    # Group results by task
    by_task: dict[str, list[BenchmarkResult]] = defaultdict(list)
    for r in suite.results:
        by_task[r.task_id].append(r)

    config_names = sorted(by_config.keys())

    # --- Overall comparison ---
    table = Table(title="Overall Config Comparison")
    table.add_column("Metric", style="cyan")
    for name in config_names:
        table.add_column(name, justify="right")

    def _avg(results: list[BenchmarkResult], fn) -> str:
        vals = [fn(r) for r in results if r.error is None]
        return f"{sum(vals) / len(vals):.2f}" if vals else "N/A"

    def _sum(results: list[BenchmarkResult], fn) -> str:
        vals = [fn(r) for r in results if r.error is None]
        return f"{sum(vals):.1f}" if vals else "N/A"

    rows = [
        ("Tasks completed", lambda r: 1 if r.error is None else 0, _sum),
        ("Tasks failed", lambda r: 1 if r.error is not None else 0, _sum),
        ("Avg detection rate", lambda r: r.score.detection_rate, _avg),
        ("Avg wall time (s)", lambda r: r.metrics.total_wall_s, _avg),
        ("Total wall time (s)", lambda r: r.metrics.total_wall_s, _sum),
        ("Avg tool calls", lambda r: r.metrics.total_tool_calls, _avg),
        ("Avg context pressure", lambda r: r.metrics.peak_context_pressure, _avg),
        ("Avg cache hit rate", lambda r: r.metrics.cache_hit_rate, _avg),
        ("Total tokens (k)", lambda r: (r.metrics.total_prompt_tokens + r.metrics.total_completion_tokens) / 1000, _sum),
        ("Avg schema rejects", lambda r: r.metrics.total_schema_rejects, _avg),
        ("Total escalations", lambda r: r.metrics.escalations, _sum),
    ]

    for label, fn, agg_fn in rows:
        table.add_row(label, *[agg_fn(by_config[name], fn) for name in config_names])

    console.print(table)

    # --- Per-task comparison ---
    for task_id, results in sorted(by_task.items()):
        task_table = Table(title=f"Task: {task_id}")
        task_table.add_column("Metric", style="cyan")
        for r in results:
            task_table.add_column(r.config_name, justify="right")

        task_rows = [
            ("Status", lambda r: "OK" if r.error is None else f"ERR: {r.error[:30]}"),
            ("Detection rate", lambda r: f"{r.score.detection_rate:.0%}"),
            ("Findings detected", lambda r: str(len(r.score.findings_detected))),
            ("Findings missed", lambda r: str(len(r.score.findings_missed))),
            ("Wall time (s)", lambda r: f"{r.metrics.total_wall_s:.1f}"),
            ("Tool calls", lambda r: str(r.metrics.total_tool_calls)),
            ("Peak ctx pressure", lambda r: f"{r.metrics.peak_context_pressure:.2f}"),
            ("Cache hit rate", lambda r: f"{r.metrics.cache_hit_rate:.0%}"),
            ("Prompt tokens", lambda r: f"{r.metrics.total_prompt_tokens:,}"),
            ("Completion tokens", lambda r: f"{r.metrics.total_completion_tokens:,}"),
            ("Escalations", lambda r: str(r.metrics.escalations)),
        ]

        for label, fn in task_rows:
            task_table.add_row(label, *[fn(r) for r in results])

        console.print(task_table)

    # --- Per-role comparison (aggregate across tasks) ---
    role_data: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
    for r in suite.results:
        if r.error is not None:
            continue
        for role_name, role_metrics in r.metrics.per_role.items():
            key = r.config_name
            if role_name not in role_data[key]:
                role_data[key][role_name] = {
                    "wall_s": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "tool_calls": 0, "count": 0, "model": role_metrics.get("model", ""),
                }
            d = role_data[key][role_name]
            d["wall_s"] += role_metrics.get("wall_s", 0)
            d["prompt_tokens"] += role_metrics.get("prompt_tokens", 0)
            d["completion_tokens"] += role_metrics.get("completion_tokens", 0)
            d["tool_calls"] += role_metrics.get("tool_calls", 0)
            d["count"] += 1

    if role_data:
        role_table = Table(title="Per-Role Aggregate (across all tasks)")
        role_table.add_column("Config", style="cyan")
        role_table.add_column("Role")
        role_table.add_column("Model", style="dim")
        role_table.add_column("Total wall (s)", justify="right")
        role_table.add_column("Avg wall (s)", justify="right")
        role_table.add_column("Total tokens", justify="right")
        role_table.add_column("Avg tok/s", justify="right")
        role_table.add_column("Total tools", justify="right")

        for config_name in config_names:
            for role_name, d in sorted(role_data.get(config_name, {}).items()):
                total_tok = d["prompt_tokens"] + d["completion_tokens"]
                avg_wall = d["wall_s"] / d["count"] if d["count"] else 0
                avg_tps = d["completion_tokens"] / d["wall_s"] if d["wall_s"] > 0 else 0
                role_table.add_row(
                    config_name, role_name,
                    d["model"][:35],
                    f"{d['wall_s']:.1f}",
                    f"{avg_wall:.1f}",
                    f"{total_tok:,}",
                    f"{avg_tps:.1f}",
                    str(d["tool_calls"]),
                )

        console.print(role_table)

    # --- Findings detail ---
    detail_table = Table(title="Detection Detail (per task × config)")
    detail_table.add_column("Task", style="cyan")
    detail_table.add_column("Config")
    detail_table.add_column("Detected", style="green")
    detail_table.add_column("Missed", style="red")

    for r in suite.results:
        if r.error is not None:
            continue
        detected = ", ".join(r.score.findings_detected[:3]) or "(none)"
        missed = ", ".join(r.score.findings_missed[:3]) or "(none)"
        if len(r.score.findings_detected) > 3:
            detected += f" (+{len(r.score.findings_detected) - 3})"
        if len(r.score.findings_missed) > 3:
            missed += f" (+{len(r.score.findings_missed) - 3})"
        detail_table.add_row(r.task_id, r.config_name, detected, missed)

    console.print(detail_table)


def load_suite(path: str | Path) -> BenchmarkSuite:
    """Load a previously saved benchmark suite from JSON."""
    data = json.loads(Path(path).read_text())
    suite = BenchmarkSuite(name=data["name"], started_at=data["started_at"])

    from luxe.benchmark.scorer import TaskScore
    from luxe.metrics.collector import RunMetrics

    for rd in data.get("results", []):
        score_data = rd.get("score", {})
        score = TaskScore(**{k: v for k, v in score_data.items()
                            if k in TaskScore.__dataclass_fields__})

        metrics_data = rd.get("metrics", {})
        metrics = RunMetrics(**{k: v for k, v in metrics_data.items()
                               if k in RunMetrics.__dataclass_fields__})

        suite.results.append(BenchmarkResult(
            task_id=rd["task_id"],
            task_name=rd["task_name"],
            config_name=rd["config_name"],
            config_path=rd["config_path"],
            score=score,
            metrics=metrics,
            error=rd.get("error"),
        ))

    return suite
