"""Walk ~/.luxe/tasks/T-…/state.json and label each task with its
(repo, backend) by joining against an overnight phase's
multi_turn_reviews.log. Produces a single JSONL summary the composite
verdict script can consume.

Why: each `--only multi_turn_reviews` invocation overwrites the
state.json's `result.runs` list with just THAT sub-chunk's record.
The actual /review records persist in ~/.luxe/tasks/ but aren't
labeled with the backend. This stitcher pairs them back up by
chronological proximity to the `=== <ts> run <repo>_<backend> ===`
markers in multi_turn_reviews.log.

Usage:

    uv run python scripts/aggregate_multi_turn.py \\
        --phase overnight_2026-04-26T11-46-44

    # Or to a custom output file:
    uv run python scripts/aggregate_multi_turn.py \\
        --phase overnight_2026-04-26T11-46-44 \\
        --out results/overnight_2026-04-26T11-46-44/multi_turn_runs.jsonl

The output JSONL has one line per (task, repo, backend) with:
    task_id, repo, backend, status, wall_s, subtasks_done,
    subtasks_total, started_at, blocked_subtasks (count).

Idempotent — re-running rewrites the JSONL from the live filesystem.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
TASKS_ROOT = Path.home() / ".luxe" / "tasks"

# Match log lines like:  === 2026-04-26T15:06:40 run elara_ollama ===
_RUN_RE = re.compile(
    r"=== (?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) run (?P<repo>[a-z0-9-]+)_(?P<backend>[a-z]+) ==="
)


def _load_run_markers(log_path: Path) -> list[dict]:
    """Each marker = one (--only multi_turn_reviews) invocation. We use
    the marker timestamp to pair against tasks created within a small
    window after it."""
    if not log_path.exists():
        return []
    markers = []
    for line in log_path.read_text().splitlines():
        m = _RUN_RE.search(line)
        if not m:
            continue
        markers.append({
            "ts": datetime.fromisoformat(m["ts"]),
            "repo": m["repo"],
            "backend": m["backend"],
        })
    return markers


def _walk_tasks() -> list[dict]:
    """Read every task that looks like a /review run. Returns one
    dict per task with its created-at timestamp + summary stats."""
    out = []
    for d in sorted(TASKS_ROOT.iterdir() if TASKS_ROOT.exists() else []):
        if not d.name.startswith("T-"):
            continue
        sj = d / "state.json"
        if not sj.exists():
            continue
        try:
            s = json.loads(sj.read_text())
        except Exception:  # noqa: BLE001
            continue
        # T-YYYYMMDDTHHMMSS-xxxxxx → datetime
        try:
            ts_part = d.name.split("-")[1]
            ts = datetime.strptime(ts_part, "%Y%m%dT%H%M%S")
        except Exception:  # noqa: BLE001
            continue
        rp_path = d / "repo_path"
        repo = ""
        if rp_path.exists():
            try:
                repo = Path(rp_path.read_text().strip()).name
            except Exception:  # noqa: BLE001
                repo = ""
        subs = s.get("subtasks", [])
        out.append({
            "task_id": d.name,
            "started_at": ts,
            "repo": repo,
            "status": s.get("status"),
            "wall_s": round(sum(x.get("wall_s", 0) for x in subs), 1),
            "subtasks_total": len(subs),
            "subtasks_done": sum(1 for x in subs if x.get("status") == "done"),
            "blocked_subtasks": sum(1 for x in subs if x.get("status") == "blocked"),
        })
    return out


def _join(markers: list[dict], tasks: list[dict],
          window_s: int = 60) -> list[dict]:
    """For each marker, find the task whose created-at is within
    `window_s` AFTER the marker AND whose repo matches. The harness
    typically launches start_review_task within 1–5s of writing the
    marker line, so a 60s window is generous but not lossy.

    Tasks that don't match any marker are emitted with backend='?' —
    these are usually pre-fix runs left over from earlier iterations.
    """
    annotated = []
    used_tasks = set()
    for m in markers:
        candidates = [
            t for t in tasks
            if t["task_id"] not in used_tasks
            and t["repo"] == m["repo"]
            and m["ts"] <= t["started_at"] <= m["ts"] + timedelta(seconds=window_s)
        ]
        if not candidates:
            # No task matched — probably the run failed at start_review_task
            continue
        # Pick the earliest matching task (closest to the marker).
        chosen = min(candidates, key=lambda t: t["started_at"])
        used_tasks.add(chosen["task_id"])
        annotated.append({**chosen, "backend": m["backend"],
                          "marker_ts": m["ts"].isoformat()})
    # Stable output: ordered by marker timestamp.
    annotated.sort(key=lambda r: r["marker_ts"])
    return annotated


def _serialize(records: list[dict]) -> list[str]:
    """Convert datetime fields to ISO strings for JSONL."""
    out = []
    for r in records:
        copy = dict(r)
        if isinstance(copy.get("started_at"), datetime):
            copy["started_at"] = copy["started_at"].isoformat()
        out.append(json.dumps(copy))
    return out


def main(
    phase: str = typer.Option(..., "--phase",
        help="Overnight phase directory name, e.g. overnight_2026-04-26T11-46-44"),
    out: Path = typer.Option(None, "--out",
        help="Output JSONL path. Defaults to results/<phase>/multi_turn_runs.jsonl"),
    window_s: int = typer.Option(60, "--window-s",
        help="Marker→task pairing window (seconds)."),
) -> None:
    phase_dir = ROOT / "results" / phase
    if not phase_dir.exists():
        typer.echo(f"phase dir missing: {phase_dir}", err=True)
        raise typer.Exit(2)

    log_path = phase_dir / "multi_turn_reviews.log"
    out_path = out or (phase_dir / "multi_turn_runs.jsonl")

    markers = _load_run_markers(log_path)
    tasks = _walk_tasks()
    records = _join(markers, tasks, window_s=window_s)

    out_path.write_text("\n".join(_serialize(records)) + ("\n" if records else ""))

    typer.echo(f"markers: {len(markers)}  tasks: {len(tasks)}  joined: {len(records)}")
    typer.echo(f"wrote {out_path}")

    # Brief grid for sanity.
    typer.echo("\nrepo × backend grid:")
    typer.echo(f"  {'repo':16s}  {'backend':10s}  {'status':10s}  {'subs':>7s}  {'block':>5s}  {'wall(min)':>9s}")
    for r in records:
        typer.echo(
            f"  {r['repo']:16s}  {r['backend']:10s}  {r['status']:10s}  "
            f"{r['subtasks_done']:>2d}/{r['subtasks_total']:<2d}    "
            f"{r['blocked_subtasks']:>4d}   {r['wall_s']/60:>9.1f}"
        )


if __name__ == "__main__":
    typer.run(main)
