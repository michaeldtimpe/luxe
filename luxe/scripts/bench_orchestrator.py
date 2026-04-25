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
import hashlib
import json
import math
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

from cli.registry import load_config
from cli.session import Session
from cli.tasks import Orchestrator, Task, load, plan
from cli.tasks.model import task_id as new_task_id
from cli.tools import fs as _fs

# cc-canary-style behavioral signal: which tool names count as
# orientation/grounding (reads) vs. mutation (edits). The ratio of the
# two catches drift the wall-time / token-count metrics can't see —
# collapse to <1 means the agent is over-writing without grounding;
# spike to >20 means it's spinning on lookup.
_READ_TOOL_NAMES = frozenset({"read_file", "glob", "grep", "list_dir"})
_EDIT_TOOL_NAMES = frozenset({"write_file", "edit_file"})

# Inflection detection: composite_health is z-scored against the trailing
# window of size W; a row whose value diverges by more than K stdevs is
# flagged. Single sliding window; pure stdlib.
_INFLECTION_WINDOW = 10
_INFLECTION_K = 1.5


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


def _tool_behavior(tool_calls: list[Any]) -> dict[str, Any]:
    """Derive cc-canary-style behavioral signal from a subtask's
    ToolCall list: reads-per-edit and tool-loop ratio. Returns the
    aggregate counts so callers can sum them across subtasks before
    computing the ratio at the totals level (avoids ratio-of-means
    bias)."""
    reads = edits = repeated = total = 0
    prev_key: str | None = None
    for tc in tool_calls:
        name = getattr(tc, "name", "") or ""
        args = getattr(tc, "arguments", {}) or {}
        try:
            args_blob = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_blob = repr(args)
        key = f"{name}|{hashlib.sha1(args_blob.encode()).hexdigest()[:12]}"
        if name in _READ_TOOL_NAMES:
            reads += 1
        if name in _EDIT_TOOL_NAMES:
            edits += 1
        if prev_key is not None and key == prev_key:
            repeated += 1
        prev_key = key
        total += 1
    return {
        "reads": reads,
        "edits": edits,
        "tool_loop_repeats": repeated,
        "tool_calls_seen": total,
        "reads_per_edit": round(reads / max(edits, 1), 2),
        "tool_loop_ratio": round(repeated / total, 3) if total else 0.0,
    }


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
        # cc-canary-style behavioral aggregates. The ratio is computed
        # from the summed numerators/denominators below to avoid the
        # mean-of-ratios bias when subtasks have very different sizes.
        "reads": 0,
        "edits": 0,
        "tool_loop_repeats": 0,
        "reads_per_edit": 0.0,
        "tool_loop_ratio": 0.0,
    }
    # Cache hits/misses are event-level — the orchestrator emits them
    # on "end" events; reconstruct from log.jsonl when available.
    cache_by_sub = _read_cache_events(task)
    for sub in task.subtasks:
        beh = _tool_behavior(sub.tool_calls)
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
            "reads_per_edit": beh["reads_per_edit"],
            "tool_loop_ratio": beh["tool_loop_ratio"],
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
        totals["reads"] += beh["reads"]
        totals["edits"] += beh["edits"]
        totals["tool_loop_repeats"] += beh["tool_loop_repeats"]
    totals["wall_s"] = round(totals["wall_s"], 1)
    totals["reads_per_edit"] = round(totals["reads"] / max(totals["edits"], 1), 2)
    totals["tool_loop_ratio"] = (
        round(totals["tool_loop_repeats"] / totals["tool_calls"], 3)
        if totals["tool_calls"] else 0.0
    )

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


def _t(row: dict[str, Any] | None, key: str, default: float = 0.0) -> float:
    """Read a totals field with backwards-compat. Old rows lack the new
    behavioral fields; treat their absence as a neutral 0 so historical
    rows render without crashing."""
    if not row:
        return default
    val = (row.get("totals") or {}).get(key, default)
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _composite_health(row: dict[str, Any]) -> float:
    """Single-axis derived score in the same direction for every input
    (higher = healthier). Ratios picked to keep magnitudes comparable
    without needing per-feature normalization at compute time — the
    inflection check normalizes against the trailing window."""
    t = row.get("totals") or {}
    wall_s = float(t.get("wall_s") or 0.0)
    schema_rej = float(t.get("schema_rejects") or 0.0)
    loop = float(t.get("tool_loop_ratio") or 0.0)
    hits = float(t.get("cache_hits") or 0.0)
    misses = float(t.get("cache_misses") or 0.0)
    cache_rate = hits / (hits + misses) if (hits + misses) else 0.0
    # Lower wall/schema_rej/loop = healthier, so subtract them; higher
    # cache_rate = healthier, so add it. Wall scaled by /60 to put it on
    # a per-minute axis comparable to the other 0-1ish ratios.
    return cache_rate - (wall_s / 60.0) - schema_rej - loop


def _inflection_flag(
    rows: list[dict[str, Any]], idx: int
) -> str:
    """Return ' ⚠ INFLECTION' if row[idx]'s composite_health diverges
    from the trailing window by more than K stdevs; '' otherwise.
    Returns '' if the window has fewer than 3 prior rows (not enough
    signal to compute a meaningful stdev)."""
    window = rows[max(0, idx - _INFLECTION_WINDOW): idx]
    if len(window) < 3:
        return ""
    prior = [_composite_health(r) for r in window]
    mu = statistics.fmean(prior)
    try:
        sigma = statistics.stdev(prior)
    except statistics.StatisticsError:
        return ""
    if math.isnan(sigma):
        return ""
    # Floor sigma so a perfectly-flat baseline still flags large
    # absolute swings — without this, the first divergent row after a
    # run of identical scores produces sigma=0 and is silently missed.
    sigma_eff = max(sigma, abs(mu) * 0.05, 0.1)
    cur = _composite_health(rows[idx])
    if abs(cur - mu) > _INFLECTION_K * sigma_eff:
        return f"  ⚠ INFLECTION (Δ={cur - mu:+.2f}, σ={sigma_eff:.2f})"
    return ""


def cmd_show(args: argparse.Namespace) -> int:
    all_rows = _read_history()
    if not all_rows:
        print(f"no history yet — write to {HISTORY_PATH}")
        return 0
    rows = all_rows[-args.n:]
    base_idx = len(all_rows) - len(rows)
    # Print oldest-first so deltas line up left→right, but flag the
    # most recent row at the bottom so grep-style tailing works.
    prev: dict[str, Any] | None = None
    for j, row in enumerate(rows):
        t = row["totals"]
        label = row.get("label") or row["task_id"]
        head = f"{row['ts']}  commit={row['commit']}  label={label}"
        if row.get("target"):
            head += f"  target={row['target']}"
        # Inflection is computed against the *full* history, not just
        # the tailed window, so a 1-row tail can still be flagged.
        head += _inflection_flag(all_rows, base_idx + j)
        print(head)
        rows_out = [
            ("wall_s        ", t["wall_s"], _t(prev, "wall_s"), True),
            ("tool_calls    ", t["tool_calls"], _t(prev, "tool_calls"), True),
            ("cache_hits    ", t["cache_hits"], _t(prev, "cache_hits"), False),
            ("cache_misses  ", t["cache_misses"], _t(prev, "cache_misses"), True),
            ("schema_rejects", t["schema_rejects"], _t(prev, "schema_rejects"), True),
            ("prompt_tok    ", t["prompt_tokens"], _t(prev, "prompt_tokens"), True),
            ("completion_tok", t["completion_tokens"], _t(prev, "completion_tokens"), True),
            # cc-canary-style behavioral signals. Older rows render the
            # value as — when the totals dict lacks the key.
            ("reads_per_edit", t.get("reads_per_edit", "—"),
             _t(prev, "reads_per_edit"), False),
            ("tool_loop_ratio", t.get("tool_loop_ratio", "—"),
             _t(prev, "tool_loop_ratio"), True),
        ]
        for name, cur, p, lower_better in rows_out:
            if cur == "—":
                print(f"  {name} —")
                continue
            delta = _fmt_delta(float(cur), p, better_when_lower=lower_better) if p else ""
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
