"""Benchmark runner — executes tasks across multiple configs and collects results."""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from luxe.benchmark.fixtures import FIXTURES
from luxe.benchmark.scorer import TaskScore, score_run
from luxe.benchmark.tasks import BenchmarkTask, get_tasks
from luxe.config import load_config
from luxe.metrics.collector import RunMetrics, collect, save_metrics
from luxe.pipeline.model import PipelineRun
from luxe.pipeline.orchestrator import PipelineOrchestrator

console = Console()


@dataclass
class BenchmarkResult:
    task_id: str
    task_name: str
    config_name: str
    config_path: str
    score: TaskScore
    metrics: RunMetrics
    report: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "config_name": self.config_name,
            "config_path": self.config_path,
            "score": self.score.to_dict(),
            "metrics": asdict(self.metrics),
            "error": self.error,
        }


@dataclass
class BenchmarkSuite:
    name: str
    started_at: float = field(default_factory=time.time)
    results: list[BenchmarkResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "started_at": self.started_at,
            "total_results": len(self.results),
            "results": [r.to_dict() for r in self.results],
        }


def run_benchmark(
    config_paths: list[str | Path],
    task_ids: list[str] | None = None,
    task_tags: list[str] | None = None,
    output_dir: str | Path = "./benchmarks",
    fixture_dir: str | Path | None = None,
    execution_modes: list[str] | None = None,
) -> BenchmarkSuite:
    """Run benchmark tasks across multiple pipeline configs.

    Args:
        config_paths: List of pipeline.yaml paths to compare.
        task_ids: Specific task IDs to run (None = all).
        task_tags: Filter tasks by tags (None = all).
        output_dir: Where to save results.
        fixture_dir: Where to create test repos (default: temp dir).
        execution_modes: Pipeline execution modes to evaluate. Defaults to
            [config.execution] for each config. Pass ["swarm", "microloop"]
            to A/B compare modes side-by-side; results carry a
            "config_name [mode]" label so the existing comparison tables
            light up automatically.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    if fixture_dir:
        fix_base = Path(fixture_dir)
        fix_base.mkdir(parents=True, exist_ok=True)
    else:
        fix_base = Path(tempfile.mkdtemp(prefix="luxe_bench_"))

    tasks = get_tasks(task_tags)
    if task_ids:
        tasks = [t for t in tasks if t.id in task_ids]

    if not tasks:
        console.print("[yellow]No matching benchmark tasks found.[/]")
        return BenchmarkSuite(name="empty")

    console.print(f"\n[bold]Benchmark Suite[/]")
    console.print(f"Configs: {len(config_paths)} | Tasks: {len(tasks)} | "
                  f"Total runs: {len(config_paths) * len(tasks)}")

    # Create fixture repos once (shared across configs)
    needed_fixtures = {t.fixture for t in tasks}
    repos: dict[str, Path] = {}
    for fixture_name in needed_fixtures:
        if fixture_name in FIXTURES:
            console.print(f"  Creating fixture: {fixture_name}")
            repos[fixture_name] = FIXTURES[fixture_name](fix_base)

    suite = BenchmarkSuite(name=f"bench_{int(time.time())}")

    for config_path in config_paths:
        config_path = Path(config_path)
        config_stem = config_path.stem
        console.print(f"\n{'='*60}")
        console.print(f"[bold cyan]Config: {config_stem}[/] ({config_path})")
        console.print(f"{'='*60}")

        try:
            config = load_config(config_path)
        except Exception as e:
            console.print(f"[red]Failed to load config: {e}[/]")
            for task in tasks:
                suite.results.append(BenchmarkResult(
                    task_id=task.id, task_name=task.name,
                    config_name=config_stem, config_path=str(config_path),
                    score=TaskScore(), metrics=RunMetrics(),
                    error=f"Config load failed: {e}",
                ))
            continue

        modes = list(execution_modes) if execution_modes else [config.execution]
        label_with_mode = len(modes) > 1

        for mode in modes:
            mode_label = f"{config_stem} [{mode}]" if label_with_mode else config_stem
            if label_with_mode:
                console.print(f"\n[bold magenta]Execution mode: {mode}[/]")
            orch = PipelineOrchestrator(config, execution_mode=mode)

            for task in tasks:
                if task.fixture not in repos:
                    console.print(f"  [yellow]Skipping {task.id}: fixture {task.fixture} not found[/]")
                    continue

                repo_path = str(repos[task.fixture])
                console.print(f"\n[bold yellow]▶ Task: {task.name}[/] ({task.id})")
                console.print(f"  Type: {task.task_type} | Fixture: {task.fixture}")

                try:
                    run = orch.run(task.goal, task.task_type, repo_path)
                    metrics = collect(run)
                    score = score_run(run, task, mode_label)

                    result = BenchmarkResult(
                        task_id=task.id, task_name=task.name,
                        config_name=mode_label, config_path=str(config_path),
                        score=score, metrics=metrics,
                        report=run.final_report,
                    )

                    console.print(f"  Detection rate: {score.detection_rate:.0%} "
                                 f"({len(score.findings_detected)}/{len(score.findings_detected) + len(score.findings_missed)})")
                    console.print(f"  Wall time: {metrics.total_wall_s:.1f}s | "
                                 f"Tool calls: {metrics.total_tool_calls} | "
                                 f"Pressure: {metrics.peak_context_pressure:.2f}")

                except Exception as e:
                    console.print(f"  [red]Failed: {e}[/]")
                    result = BenchmarkResult(
                        task_id=task.id, task_name=task.name,
                        config_name=mode_label, config_path=str(config_path),
                        score=TaskScore(), metrics=RunMetrics(),
                        error=str(e),
                    )

                suite.results.append(result)

    # Save suite results
    suite_path = output / f"{suite.name}.json"
    suite_path.write_text(json.dumps(suite.to_dict(), indent=2, default=str))
    console.print(f"\n[dim]Suite results saved: {suite_path}[/]")

    return suite
