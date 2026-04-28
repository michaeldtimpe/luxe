"""CLI entry point for luxe."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import click
from rich.console import Console

from luxe.config import load_config
from luxe.metrics.collector import collect, save_metrics
from luxe.metrics.report import print_run_summary
from luxe.pipeline.orchestrator import PipelineOrchestrator

console = Console()


def _resolve_repo(repo: str) -> str:
    """Resolve a repo argument to a local path. Clones if it's a URL."""
    p = Path(repo).expanduser().resolve()
    if p.is_dir():
        return str(p)

    if repo.startswith(("http://", "https://", "git@")):
        clone_dir = Path(tempfile.mkdtemp(prefix="luxe_"))
        console.print(f"[dim]Cloning {repo} → {clone_dir}[/]")
        result = subprocess.run(
            ["git", "clone", "--depth=1", repo, str(clone_dir)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            console.print(f"[red]Clone failed:[/] {result.stderr}")
            sys.exit(1)
        return str(clone_dir)

    console.print(f"[red]Not a directory or repo URL:[/] {repo}")
    sys.exit(1)


@click.group()
def main():
    """luxe — MLX-only repo maintainer."""
    pass


@main.command()
@click.argument("repo")
@click.argument("goal")
@click.option("--type", "task_type", default="review",
              type=click.Choice(["review", "implement", "bugfix", "document", "summarize", "manage"]),
              help="Task type determines pipeline shape")
@click.option("--config", "config_path", default=None, help="Path to pipeline.yaml")
@click.option("--output", "output_dir", default="./runs", help="Directory for metrics output")
@click.option("--save-report", is_flag=True, help="Save final report as markdown")
def run(repo: str, goal: str, task_type: str, config_path: str | None,
        output_dir: str, save_report: bool):
    """Run a luxe pipeline against a repository.

    REPO: Local path or git URL to clone.
    GOAL: What to accomplish (e.g., "review for security issues").
    """
    config = load_config(config_path)
    repo_path = _resolve_repo(repo)

    console.print(f"\n[bold]Swarm Pipeline[/]")
    console.print(f"Task: {task_type} | Repo: {repo_path}")
    console.print(f"Goal: {goal}\n")

    orch = PipelineOrchestrator(config)
    pipeline_run = orch.run(goal, task_type, repo_path)

    metrics = collect(pipeline_run)
    print_run_summary(pipeline_run, metrics)

    metrics_path = save_metrics(metrics, output_dir)
    console.print(f"\n[dim]Metrics saved: {metrics_path}[/]")

    if save_report and pipeline_run.final_report:
        report_path = Path(output_dir) / f"report_{pipeline_run.id}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(pipeline_run.final_report)
        console.print(f"[dim]Report saved: {report_path}[/]")

    if pipeline_run.final_report:
        console.print(f"\n{'='*60}")
        console.print(pipeline_run.final_report)


@main.command()
@click.option("--config", "config_path", default=None, help="Path to pipeline.yaml")
def check(config_path: str | None):
    """Check oMLX connectivity and model availability."""
    from luxe.backend import Backend

    config = load_config(config_path)
    backend = Backend(base_url=config.omlx_base_url)

    if not backend.health():
        console.print(f"[red]Cannot reach oMLX at {config.omlx_base_url}[/]")
        sys.exit(1)

    console.print(f"[green]oMLX is healthy[/] at {config.omlx_base_url}")

    available = set(backend.list_models())
    console.print(f"\nAvailable models ({len(available)}):")
    for m in sorted(available):
        console.print(f"  {m}")

    console.print(f"\nPipeline model requirements:")
    all_ok = True
    for role_name, model_id in config.models.items():
        found = model_id in available
        status = "[green]✓[/]" if found else "[red]✗[/]"
        console.print(f"  {status} {role_name}: {model_id}")
        if not found:
            all_ok = False

    if not all_ok:
        console.print("\n[yellow]Some models are missing. Load them in oMLX before running.[/]")
        sys.exit(1)
    else:
        console.print("\n[green]All pipeline models available.[/]")


@main.command()
@click.argument("metrics_dir", default="./runs")
def compare(metrics_dir: str):
    """Compare metrics from multiple pipeline runs."""
    from luxe.metrics.collector import RunMetrics
    from luxe.metrics.report import print_comparison

    p = Path(metrics_dir)
    if not p.is_dir():
        console.print(f"[red]Directory not found:[/] {metrics_dir}")
        sys.exit(1)

    runs: list[tuple[str, RunMetrics]] = []
    for f in sorted(p.glob("run_*.json")):
        data = json.loads(f.read_text())
        m = RunMetrics(**{k: v for k, v in data.items()
                         if k in RunMetrics.__dataclass_fields__})
        label = f"{m.task_type}/{m.run_id[:8]}"
        runs.append((label, m))

    if not runs:
        console.print("[yellow]No run metrics found.[/]")
        sys.exit(1)

    print_comparison(runs)


@main.command()
@click.argument("configs", nargs=-1, required=True)
@click.option("--tasks", "task_ids", multiple=True, help="Specific task IDs to run")
@click.option("--tags", multiple=True, help="Filter tasks by tag (security, python, core, etc.)")
@click.option("--output", "output_dir", default="./benchmarks", help="Output directory")
@click.option("--fixtures", "fixture_dir", default=None, help="Directory for test repos (default: temp)")
def benchmark(configs: tuple[str, ...], task_ids: tuple[str, ...], tags: tuple[str, ...],
              output_dir: str, fixture_dir: str | None):
    """Run benchmark tasks across multiple pipeline configs.

    CONFIGS: One or more paths to pipeline YAML configs.

    Examples:
        luxe benchmark configs/qwen_32gb.yaml configs/deepseek_32gb.yaml
        luxe benchmark configs/*.yaml --tags security --output ./results
        luxe benchmark configs/qwen_32gb.yaml --tasks review-python-security
    """
    from luxe.benchmark.compare import print_suite_summary
    from luxe.benchmark.runner import run_benchmark

    suite = run_benchmark(
        config_paths=list(configs),
        task_ids=list(task_ids) or None,
        task_tags=list(tags) or None,
        output_dir=output_dir,
        fixture_dir=fixture_dir,
    )

    console.print(f"\n{'='*60}")
    console.print("[bold]BENCHMARK RESULTS[/]")
    console.print(f"{'='*60}\n")

    print_suite_summary(suite)


@main.command(name="benchmark-report")
@click.argument("suite_path")
def benchmark_report(suite_path: str):
    """Print results from a previously saved benchmark suite.

    SUITE_PATH: Path to a bench_*.json file from a previous benchmark run.
    """
    from luxe.benchmark.compare import load_suite, print_suite_summary

    suite = load_suite(suite_path)
    print_suite_summary(suite)


@main.command(name="list-tasks")
@click.option("--tags", multiple=True, help="Filter by tag")
def list_tasks(tags: tuple[str, ...]):
    """List available benchmark tasks."""
    from luxe.benchmark.tasks import get_tasks

    tasks = get_tasks(list(tags) or None)
    if not tasks:
        console.print("[yellow]No tasks found.[/]")
        return

    table_data = []
    for t in tasks:
        console.print(f"  [cyan]{t.id}[/] — {t.name}")
        console.print(f"    Type: {t.task_type} | Fixture: {t.fixture} | Tags: {', '.join(t.tags)}")
        gt = t.ground_truth
        if gt.expected_findings:
            console.print(f"    Expected findings: {len(gt.expected_findings)}")


@main.command(name="list-models")
@click.argument("config_path")
def list_models(config_path: str):
    """Show all models required by a pipeline config with memory estimates."""
    config = load_config(config_path)

    console.print(f"\n[bold]Models for: {config_path}[/]\n")

    seen: dict[str, list[str]] = {}
    for role_name, model_id in config.models.items():
        if model_id not in seen:
            seen[model_id] = []
        seen[model_id].append(role_name)

    for model_id, roles in seen.items():
        console.print(f"  [cyan]{model_id}[/]")
        console.print(f"    Roles: {', '.join(roles)}")

    console.print(f"\n  Unique models: {len(seen)}")
    console.print(f"  (Pipeline is sequential — only one model loaded at a time)")


@main.command(name="benchmark-repos")
@click.argument("repos", nargs=-1, required=True)
@click.option("--configs", "-c", multiple=True, required=True,
              help="Pipeline config paths (pass multiple for comparison)")
@click.option("--output", "output_dir", default="./benchmarks/real", help="Output directory")
@click.option("--clone-dir", default=None, help="Where to clone repos (default: temp dir)")
@click.option("--tasks", "task_filter", multiple=True,
              type=click.Choice(["summarize", "review", "manage"]),
              help="Which tasks to run (default: all three)")
def benchmark_repos(repos: tuple[str, ...], configs: tuple[str, ...],
                    output_dir: str, clone_dir: str | None,
                    task_filter: tuple[str, ...]):
    """Run real-world benchmarks against GitHub repos.

    REPOS: One or more GitHub URLs or local paths.

    Runs each repo through summarize, review, and improvement-suggestion tasks
    with every config, then prints a head-to-head comparison.

    Examples:

      luxe benchmark-repos https://github.com/user/repo1 https://github.com/user/repo2 \\
        -c configs/qwen_32gb.yaml -c configs/deepseek_32gb.yaml

      luxe benchmark-repos /path/to/local/repo \\
        -c configs/qwen_32gb.yaml --tasks review --tasks summarize
    """
    from luxe.benchmark.real_world import (
        RepoSpec, RepoTask, DEFAULT_TASKS,
        run_real_world_benchmark, print_real_world_comparison,
    )

    repo_specs = [RepoSpec(url=url) for url in repos]

    if task_filter:
        for spec in repo_specs:
            spec.tasks = [t for t in DEFAULT_TASKS if t.task_type in task_filter]

    suite = run_real_world_benchmark(
        config_paths=list(configs),
        repos=repo_specs,
        output_dir=output_dir,
        clone_dir=clone_dir,
    )

    console.print(f"\n{'='*70}")
    console.print("[bold]REAL-WORLD BENCHMARK RESULTS[/]")
    console.print(f"{'='*70}\n")

    print_real_world_comparison(suite)


@main.command(name="benchmark-repos-report")
@click.argument("suite_path")
def benchmark_repos_report(suite_path: str):
    """Print results from a saved real-world benchmark suite.

    SUITE_PATH: Path to a real_*.json file.
    """
    from luxe.benchmark.real_world import load_real_world_suite, print_real_world_comparison

    suite = load_real_world_suite(suite_path)
    print_real_world_comparison(suite)


if __name__ == "__main__":
    main()
