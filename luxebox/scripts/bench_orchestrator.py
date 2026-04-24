#!/usr/bin/env python3
"""Append-only performance history for luxe's task orchestrator.

Motivates: the review/refactor pipelines are inference-bound, so the
metrics that matter across commits aren't decode tok/s (that belongs
to the model layer), they're the orchestration-level signals: how
much wall time was spent, how many tool calls were issued, how many
cache hits collapsed cross-subtask repeats, how many tool calls got
rejected by schema validation.

Each run appends one record to `results/orchestrator_bench/history.jsonl`.
Records are stamped with the current git rev so a regression between
commits has an audit trail.

Three subcommands:

    import <task-id>       Pull a finished ~/.luxe/tasks/<id> into history.
                           Use this to backfill baselines (e.g. the
                           pre-fix elara run) without re-running.
    run <goal>             Plan + run a fresh task, appending results.
                           Requires Ollama running + the review model
                           pulled. Pass `--cwd <path>` to target a repo.
    show [-n N]            Tail the history, newest first, with deltas
                           vs. the prior record so regressions are
                           visible at a glance.

The idea is you run `import` once on your baseline trace, then `run`
after each perf-relevant change, and `show` tells you whether the
change helped, hurt, or was a wash."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from luxe.registry import load_config
from luxe.session import Session
from luxe.tasks import Orchestrator, Task, load, plan
from luxe.tasks.model import task_id as new_task_id
from luxe.tools import fs as _fs


HISTORY_PATH = (
    Path(__file__).resolve().parent.parent
    / "results"
    / "orchestrator_bench"
    / "history.jsonl"
)


def _git_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            text=True,
        )
        return out.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _task_to_record(task: Task, *, target: str | None = None) -> dict[str, Any]:
    """Summarise a finished Task into a history record. Cross-cut
    totals at the top, per-subtask breakdown below for when a
    regression is localised to one phase."""
    subs_out: list[dict[str, Any]] = []
    totals = {
        "wall_s": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tool_calls": 0,
        "cache_hits": 0,  # only populated on runs with the fix applied
        "cache_misses": 0,
        "schema_rejects": 0,
        "near_cap_turns": 0,
    }
    # Cache hits/misses are event-level — the orchestrator emits them
    # on "end" events; reconstruct from log.jsonl when available.
    cache_by_sub = _read_cache_events(task)
    for sub in task.subtasks:
        entry: dict[str, Any] = {
            "index": sub.index,
            "title": sub.title,
            "status": sub.status,
            "agent": sub.agent,
            "wall_s": round(sub.wall_s, 1),
            "tool_calls": sub.tool_calls_total,
            "steps": sub.steps_taken,
            "prompt_tokens": sub.prompt_tokens,
            "completion_tokens": sub.completion_tokens,
            "near_cap_turns": sub.near_cap_turns,
            "schema_rejects": getattr(sub, "schema_rejects", 0),
        }
        cache = cache_by_sub.get(sub.id)
        if cache:
            entry["cache_hits"] = cache["hits"]
            entry["cache_misses"] = cache["misses"]
            totals["cache_hits"] += cache["hits"]
            totals["cache_misses"] += cache["misses"]
        subs_out.append(entry)
        totals["wall_s"] += sub.wall_s
        totals["prompt_tokens"] += sub.prompt_tokens
        totals["completion_tokens"] += sub.completion_tokens
        totals["tool_calls"] += sub.tool_calls_total
        totals["schema_rejects"] += getattr(sub, "schema_rejects", 0)
        totals["near_cap_turns"] += sub.near_cap_turns
    totals["wall_s"] = round(totals["wall_s"], 1)

    return {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "commit": _git_commit(),
        "task_id": task.id,
        "goal": task.goal,
        "target": target or _read_repo_path(task),
        "status": task.status,
        "n_subtasks": len(task.subtasks),
        "totals": totals,
        "subtasks": subs_out,
    }


def _read_repo_path(task: Task) -> str | None:
    """/review writes the target repo path to `<task-dir>/repo_path` so
    the orchestrator's cwd is recoverable after the fact. Fall back to
    None when the file is absent (older tasks, non-review goals)."""
    try:
        return (task.dir() / "repo_path").read_text().strip()
    except Exception:  # noqa: BLE001
        return None


def _read_cache_events(task: Task) -> dict[str, dict[str, int]]:
    """Pull per-subtask cache hit/miss counts from `log.jsonl`. Older
    runs (pre-fix) won't have the fields — in which case we return {}
    and the caller records misses = tool_calls (everything was a miss
    before the cache existed)."""
    out: dict[str, dict[str, int]] = {}
    log = task.dir() / "log.jsonl"
    if not log.exists():
        return out
    for line in log.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") != "end" or "subtask" not in ev:
            continue
        if "cache_hits" in ev or "cache_misses" in ev:
            out[ev["subtask"]] = {
                "hits": int(ev.get("cache_hits", 0)),
                "misses": int(ev.get("cache_misses", 0)),
            }
    return out


def _append(record: dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _read_history() -> list[dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in HISTORY_PATH.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# ── subcommands ──────────────────────────────────────────────────────


def cmd_import(args: argparse.Namespace) -> int:
    task = load(args.task_id)
    if task is None:
        print(f"no task found: {args.task_id}", file=sys.stderr)
        return 2
    record = _task_to_record(task)
    if args.label:
        record["label"] = args.label
    _append(record)
    print(
        f"imported {task.id}: wall={record['totals']['wall_s']}s "
        f"tool_calls={record['totals']['tool_calls']} "
        f"schema_rejects={record['totals']['schema_rejects']} "
        f"cache_hits={record['totals']['cache_hits']}"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.cwd:
        _fs.set_repo_root(args.cwd)
    goal = args.goal
    tid = new_task_id()
    subtasks = plan(goal, cfg, tid)
    task = Task(id=tid, goal=goal, subtasks=subtasks)
    session = Session() if args.session else None
    orch = Orchestrator(cfg, session=session)
    orch.run(task)
    record = _task_to_record(task, target=str(args.cwd) if args.cwd else None)
    if args.label:
        record["label"] = args.label
    _append(record)
    print(
        f"ran {task.id}: wall={record['totals']['wall_s']}s "
        f"tool_calls={record['totals']['tool_calls']} "
        f"cache_hits={record['totals']['cache_hits']} "
        f"schema_rejects={record['totals']['schema_rejects']}"
    )
    return 0


def _fmt_delta(cur: float, prev: float, *, better_when_lower: bool = True) -> str:
    if prev == 0:
        return ""
    delta = cur - prev
    pct = (delta / prev) * 100 if prev else 0
    direction = "↓" if delta < 0 else ("↑" if delta > 0 else "·")
    # Lower is better for wall/tokens/misses; higher is better for hits.
    improved = (delta < 0) if better_when_lower else (delta > 0)
    mark = "✓" if improved else ("✗" if delta != 0 else " ")
    return f"  ({direction}{abs(pct):4.1f}% {mark})"


def cmd_show(args: argparse.Namespace) -> int:
    rows = _read_history()
    if not rows:
        print(f"no history yet — write to {HISTORY_PATH}")
        return 0
    rows = rows[-args.n:]
    # Print oldest-first so deltas line up left→right, but flag the
    # most recent row at the bottom so grep-style tailing works.
    prev: dict[str, Any] | None = None
    for row in rows:
        t = row["totals"]
        label = row.get("label") or row["task_id"]
        head = f"{row['ts']}  commit={row['commit']}  label={label}"
        if row.get("target"):
            head += f"  target={row['target']}"
        print(head)
        rows_out = [
            ("wall_s       ", t["wall_s"], prev and prev["totals"]["wall_s"], True),
            ("tool_calls   ", t["tool_calls"], prev and prev["totals"]["tool_calls"], True),
            ("cache_hits   ", t["cache_hits"], prev and prev["totals"]["cache_hits"], False),
            ("cache_misses ", t["cache_misses"], prev and prev["totals"]["cache_misses"], True),
            ("schema_rejects", t["schema_rejects"], prev and prev["totals"]["schema_rejects"], True),
            ("prompt_tok   ", t["prompt_tokens"], prev and prev["totals"]["prompt_tokens"], True),
            ("completion_tok", t["completion_tokens"], prev and prev["totals"]["completion_tokens"], True),
        ]
        for name, cur, p, lower_better in rows_out:
            delta = _fmt_delta(cur, p, better_when_lower=lower_better) if p else ""
            print(f"  {name} {cur}{delta}")
        print()
        prev = row
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="import a finished ~/.luxe/tasks/<id>")
    imp.add_argument("task_id")
    imp.add_argument("--label", help="optional human-readable label (e.g. 'baseline')")

    run = sub.add_parser("run", help="plan + run a task now, append to history")
    run.add_argument("goal", help="what the task should do")
    run.add_argument("--cwd", help="override repo root (defaults to current dir)")
    run.add_argument("--session", action="store_true", help="persist session log")
    run.add_argument("--label", help="optional human-readable label")

    show = sub.add_parser("show", help="print history with deltas")
    show.add_argument("-n", type=int, default=10, help="how many rows (default 10)")

    args = p.parse_args(argv)
    if args.cmd == "import":
        return cmd_import(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "show":
        return cmd_show(args)
    p.error(f"unknown cmd {args.cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
