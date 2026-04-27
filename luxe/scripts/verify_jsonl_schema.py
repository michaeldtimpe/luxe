"""Sanity check for benchmark jsonl outputs.

Background: open item #4 from SESSION_REPORT_2026-04-26.md flagged the
patched `qwen2.5-32b × llamacpp × prefix_cache_decay` jsonl as having
"candidate / backend / bench reading None". Confirmed 2026-04-27 that
those fields aren't top-level columns — they're nested under `metrics`
(`metrics.candidate_id`, `metrics.config_id`, `metrics.benchmark`). The
verdict scripts read via _get(r, "metrics", ...) so the data is usable.

This script formalizes the schema contract and runs it across the
overnight result tree, so future schema regressions are caught fast.

Usage:
    .venv/bin/python -m scripts.verify_jsonl_schema [--phase overnight_<TS>]
    # exit 0 if every jsonl matches; 1 with a list of violators otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=False, add_completion=False, pretty_exceptions_enable=False)

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results" / "runs"

REQUIRED_TOP = {"task_id", "metrics"}
REQUIRED_METRICS = {"candidate_id", "config_id", "benchmark", "throughput"}


def _check_jsonl(path: Path) -> list[str]:
    issues: list[str] = []
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as e:
        return [f"unreadable: {e}"]
    if not rows:
        return ["empty"]
    for i, row in enumerate(rows[:5]):  # sample first 5 rows
        missing_top = REQUIRED_TOP - set(row.keys())
        if missing_top:
            issues.append(f"row {i}: missing top-level {sorted(missing_top)}")
        metrics = row.get("metrics") or {}
        missing_metrics = REQUIRED_METRICS - set(metrics.keys())
        if missing_metrics:
            issues.append(f"row {i}: missing metrics.* {sorted(missing_metrics)}")
        thr = metrics.get("throughput") or {}
        ttft = thr.get("ttft_s") or thr.get("time_to_first_token_s")
        if not isinstance(ttft, (int, float)):
            issues.append(f"row {i}: no usable ttft (got {ttft!r})")
    return issues


@app.command()
def main(
    phase: str = typer.Option(
        None, "--phase",
        help="Restrict to results/runs/<phase>/. Default: scan everything.",
    ),
) -> None:
    base = RESULTS / phase if phase else RESULTS
    if not base.exists():
        typer.echo(f"no such results dir: {base}", err=True)
        raise typer.Exit(2)

    n_total = 0
    n_bad = 0
    bad: list[tuple[Path, list[str]]] = []
    for jsonl in sorted(base.rglob("*.jsonl")):
        n_total += 1
        issues = _check_jsonl(jsonl)
        if issues:
            n_bad += 1
            bad.append((jsonl, issues))

    typer.echo(f"checked {n_total} jsonl file(s); {n_bad} with issues")
    if bad:
        for path, issues in bad:
            typer.echo(f"\n  {path.relative_to(ROOT)}")
            for issue in issues[:3]:  # cap noise per file
                typer.echo(f"    - {issue}")
            if len(issues) > 3:
                typer.echo(f"    - ... and {len(issues) - 3} more")
    raise typer.Exit(1 if bad else 0)


if __name__ == "__main__":
    app()
