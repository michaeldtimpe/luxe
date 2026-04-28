"""Real-world benchmark runner — tests pipeline configs against actual GitHub repos.

Unlike the synthetic benchmark (which uses planted-bug fixtures with ground truth),
this runner evaluates configs against real repos where quality is measured by:
- Completeness of analysis (tool calls, files examined)
- Context pressure management
- Throughput and wall time
- Report coherence (qualitative, saved for manual review)
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from luxe.config import PipelineConfig, load_config
from luxe.metrics.collector import RunMetrics, collect
from luxe.pipeline.model import PipelineRun, Status
from luxe.pipeline.orchestrator import PipelineOrchestrator

console = Console()


@dataclass
class RepoTask:
    """A task to run against a real repo."""
    task_type: str
    goal: str


@dataclass
class RepoSpec:
    """A repo to benchmark against."""
    url: str
    name: str = ""
    tasks: list[RepoTask] = field(default_factory=list)

    def __post_init__(self):
        if not self.name:
            self.name = self.url.rstrip("/").split("/")[-1]


@dataclass
class RealWorldResult:
    repo_name: str
    repo_url: str
    task_type: str
    goal: str
    config_name: str
    config_path: str
    metrics: RunMetrics = field(default_factory=RunMetrics)
    report: str = ""
    status: str = "pending"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "repo_url": self.repo_url,
            "task_type": self.task_type,
            "goal": self.goal,
            "config_name": self.config_name,
            "config_path": self.config_path,
            "metrics": asdict(self.metrics),
            "report": self.report,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class RealWorldSuite:
    name: str
    started_at: float = field(default_factory=time.time)
    results: list[RealWorldResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "started_at": self.started_at,
            "total_results": len(self.results),
            "results": [r.to_dict() for r in self.results],
        }


DEFAULT_TASKS = [
    RepoTask("summarize", "Summarize the architecture of this repository — describe its purpose, key modules, entry points, and how the components connect."),
    RepoTask("review", "Review this codebase for bugs, security issues, and code quality problems. Cite specific files and line numbers."),
    RepoTask("manage", "Analyze this repository and suggest concrete improvements — refactoring opportunities, missing tests, dependency issues, performance concerns."),
]


def clone_repo(url: str, base_dir: Path) -> Path:
    """Shallow-clone a repo, return local path."""
    name = url.rstrip("/").split("/")[-1].replace(".git", "")
    dest = base_dir / name
    if dest.exists():
        console.print(f"  [dim]Using cached clone: {dest}[/]")
        return dest

    console.print(f"  [dim]Cloning {url}...[/]")
    result = subprocess.run(
        ["git", "clone", "--depth=1", url, str(dest)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Clone failed: {result.stderr.strip()}")
    return dest


def run_real_world_benchmark(
    config_paths: list[str | Path],
    repos: list[RepoSpec],
    output_dir: str | Path = "./benchmarks/real",
    clone_dir: str | Path | None = None,
) -> RealWorldSuite:
    """Run real-world benchmarks: each repo × each task × each config."""

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    if clone_dir:
        clone_base = Path(clone_dir)
        clone_base.mkdir(parents=True, exist_ok=True)
    else:
        clone_base = Path(tempfile.mkdtemp(prefix="luxe_real_"))

    # Fill in default tasks for repos that don't specify them
    for repo in repos:
        if not repo.tasks:
            repo.tasks = list(DEFAULT_TASKS)

    total_runs = sum(len(r.tasks) for r in repos) * len(config_paths)
    console.print(f"\n[bold]Real-World Benchmark Suite[/]")
    console.print(f"Repos: {len(repos)} | Configs: {len(config_paths)} | "
                  f"Tasks per repo: {len(repos[0].tasks)} | Total runs: {total_runs}")

    suite = RealWorldSuite(name=f"real_{int(time.time())}")

    # Clone all repos upfront
    repo_paths: dict[str, Path] = {}
    for repo in repos:
        try:
            repo_paths[repo.name] = clone_repo(repo.url, clone_base)
        except Exception as e:
            console.print(f"  [red]Failed to clone {repo.url}: {e}[/]")

    run_count = 0

    for config_path in config_paths:
        config_path = Path(config_path)
        config_name = config_path.stem

        try:
            config = load_config(config_path)
        except Exception as e:
            console.print(f"[red]Failed to load {config_path}: {e}[/]")
            continue

        profile_name = config.profile.name or config_name

        console.print(f"\n{'='*70}")
        console.print(f"[bold cyan]Config: {profile_name}[/]")
        console.print(f"{'='*70}")

        orch = PipelineOrchestrator(config)

        for repo in repos:
            if repo.name not in repo_paths:
                continue
            repo_path = str(repo_paths[repo.name])

            console.print(f"\n[bold]Repo: {repo.name}[/] ({repo.url})")

            for task in repo.tasks:
                run_count += 1
                console.print(f"\n  [{run_count}/{total_runs}] "
                              f"[bold yellow]{task.task_type}[/] — {task.goal[:80]}...")

                result = RealWorldResult(
                    repo_name=repo.name,
                    repo_url=repo.url,
                    task_type=task.task_type,
                    goal=task.goal,
                    config_name=config_name,
                    config_path=str(config_path),
                )

                try:
                    run = orch.run(task.goal, task.task_type, repo_path)
                    result.metrics = collect(run)
                    result.report = run.final_report or ""
                    result.status = run.status.value

                    total_tok = result.metrics.total_prompt_tokens + result.metrics.total_completion_tokens
                    console.print(f"    [green]Done[/] — {result.metrics.total_wall_s:.1f}s, "
                                  f"{result.metrics.total_tool_calls} tool calls, "
                                  f"{total_tok:,} tok, "
                                  f"pressure: {result.metrics.peak_context_pressure:.2f}")

                    # Save individual report
                    report_dir = output / config_name / repo.name
                    report_dir.mkdir(parents=True, exist_ok=True)
                    report_path = report_dir / f"{task.task_type}.md"
                    report_path.write_text(
                        f"# {task.task_type.title()}: {repo.name}\n\n"
                        f"**Config**: {profile_name}\n"
                        f"**Goal**: {task.goal}\n"
                        f"**Wall time**: {result.metrics.total_wall_s:.1f}s\n"
                        f"**Tool calls**: {result.metrics.total_tool_calls}\n"
                        f"**Peak context pressure**: {result.metrics.peak_context_pressure:.2f}\n\n"
                        f"---\n\n{result.report}"
                    )

                except Exception as e:
                    result.status = "error"
                    result.error = str(e)
                    console.print(f"    [red]Error: {e}[/]")

                suite.results.append(result)

    # Save suite
    suite_path = output / f"{suite.name}.json"
    suite_path.write_text(json.dumps(suite.to_dict(), indent=2, default=str))
    console.print(f"\n[dim]Suite saved: {suite_path}[/]")

    return suite


def print_real_world_comparison(suite: RealWorldSuite) -> None:
    """Print head-to-head comparison of real-world benchmark results."""
    from collections import defaultdict

    by_config: dict[str, list[RealWorldResult]] = defaultdict(list)
    for r in suite.results:
        by_config[r.config_name].append(r)

    config_names = sorted(by_config.keys())

    # Overall comparison
    table = Table(title="Real-World Benchmark — Overall Comparison")
    table.add_column("Metric", style="cyan")
    for name in config_names:
        table.add_column(name, justify="right")

    def _safe_avg(results, fn):
        vals = [fn(r) for r in results if r.status != "error"]
        return f"{sum(vals)/len(vals):.2f}" if vals else "N/A"

    def _safe_sum(results, fn):
        vals = [fn(r) for r in results if r.status != "error"]
        return f"{sum(vals):.1f}" if vals else "N/A"

    rows = [
        ("Runs completed", lambda r: 1 if r.status != "error" else 0, _safe_sum),
        ("Runs failed", lambda r: 1 if r.status == "error" else 0, _safe_sum),
        ("Avg wall time (s)", lambda r: r.metrics.total_wall_s, _safe_avg),
        ("Total wall time (s)", lambda r: r.metrics.total_wall_s, _safe_sum),
        ("Avg tool calls", lambda r: float(r.metrics.total_tool_calls), _safe_avg),
        ("Avg context pressure", lambda r: r.metrics.peak_context_pressure, _safe_avg),
        ("Avg cache hit rate", lambda r: r.metrics.cache_hit_rate, _safe_avg),
        ("Avg schema rejects", lambda r: float(r.metrics.total_schema_rejects), _safe_avg),
        ("Total escalations", lambda r: float(r.metrics.escalations), _safe_sum),
        ("Total tokens (k)", lambda r: (r.metrics.total_prompt_tokens + r.metrics.total_completion_tokens) / 1000, _safe_sum),
    ]

    for label, fn, agg in rows:
        table.add_row(label, *[agg(by_config[n], fn) for n in config_names])
    console.print(table)

    # Per-repo × task comparison
    repo_tasks = set()
    for r in suite.results:
        repo_tasks.add((r.repo_name, r.task_type))

    detail = Table(title="Per-Repo × Task Comparison")
    detail.add_column("Repo", style="cyan")
    detail.add_column("Task")
    for name in config_names:
        detail.add_column(f"{name}\nwall(s)", justify="right")
        detail.add_column(f"{name}\ntools", justify="right")
        detail.add_column(f"{name}\npressure", justify="right")

    for repo_name, task_type in sorted(repo_tasks):
        row = [repo_name, task_type]
        for cfg_name in config_names:
            match = [r for r in suite.results
                     if r.repo_name == repo_name and r.task_type == task_type
                     and r.config_name == cfg_name]
            if match and match[0].status != "error":
                r = match[0]
                row.extend([
                    f"{r.metrics.total_wall_s:.1f}",
                    str(r.metrics.total_tool_calls),
                    f"{r.metrics.peak_context_pressure:.2f}",
                ])
            else:
                row.extend(["ERR", "-", "-"])
        detail.add_row(*row)

    console.print(detail)

    # Per-role throughput comparison
    role_table = Table(title="Per-Role Throughput (aggregate across all runs)")
    role_table.add_column("Config", style="cyan")
    role_table.add_column("Role")
    role_table.add_column("Model", style="dim")
    role_table.add_column("Total wall (s)", justify="right")
    role_table.add_column("Comp tokens", justify="right")
    role_table.add_column("Avg tok/s", justify="right")
    role_table.add_column("Tool calls", justify="right")

    for cfg_name in config_names:
        role_agg: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"wall_s": 0, "comp_tok": 0, "tools": 0, "model": ""}
        )
        for r in by_config[cfg_name]:
            if r.status == "error":
                continue
            for role, data in r.metrics.per_role.items():
                d = role_agg[role]
                d["wall_s"] += data.get("wall_s", 0)
                d["comp_tok"] += data.get("completion_tokens", 0)
                d["tools"] += data.get("tool_calls", 0)
                d["model"] = data.get("model", "")

        for role, d in sorted(role_agg.items()):
            tps = d["comp_tok"] / d["wall_s"] if d["wall_s"] > 0 else 0
            role_table.add_row(
                cfg_name, role, d["model"][:40],
                f"{d['wall_s']:.1f}", str(d["comp_tok"]),
                f"{tps:.1f}", str(d["tools"]),
            )

    console.print(role_table)

    # Report locations
    console.print(f"\n[bold]Individual reports saved to:[/]")
    seen_dirs: set[str] = set()
    for r in suite.results:
        d = f"  benchmarks/real/{r.config_name}/{r.repo_name}/"
        if d not in seen_dirs:
            console.print(f"  [dim]{d}[/]")
            seen_dirs.add(d)


def load_real_world_suite(path: str | Path) -> RealWorldSuite:
    """Load a previously saved real-world suite."""
    data = json.loads(Path(path).read_text())
    suite = RealWorldSuite(name=data["name"], started_at=data["started_at"])
    for rd in data.get("results", []):
        metrics_data = rd.get("metrics", {})
        metrics = RunMetrics(**{k: v for k, v in metrics_data.items()
                                if k in RunMetrics.__dataclass_fields__})
        suite.results.append(RealWorldResult(
            repo_name=rd["repo_name"],
            repo_url=rd["repo_url"],
            task_type=rd["task_type"],
            goal=rd["goal"],
            config_name=rd["config_name"],
            config_path=rd["config_path"],
            metrics=metrics,
            report=rd.get("report", ""),
            status=rd.get("status", "done"),
            error=rd.get("error"),
        ))
    return suite
