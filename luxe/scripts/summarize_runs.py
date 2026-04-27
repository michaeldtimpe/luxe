"""Aggregate every local /luxe task run into a single CSV.

Walks `~/.luxe/tasks/*/state.json` and emits one row per run with the
top-level fields we'd need to recalibrate the Phase 7 budget tiers or
measure tool-adoption trends over time:

  task_id, created_at, status, agent_kinds, num_subtasks, finished_subtasks,
  max_wall_s, total_wall_s, total_prompt_tokens, total_completion_tokens,
  analyzer_calls, reader_calls, orientation_calls, other_calls, tool_breakdown

The `tool_breakdown` column is a semicolon-separated `name=count@wall_s`
list so you can pivot on specific tool adoption in a spreadsheet
without re-parsing JSON.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

TASKS_ROOT = Path.home() / ".luxe" / "tasks"

_ANALYZERS = {
    "lint", "typecheck", "security_scan", "deps_audit",
    "security_taint", "secrets_scan",
}
_READERS = {"read_file", "grep"}
_ORIENTATION = {"list_dir", "glob"}


def main(out_path: str | None = None) -> int:
    if not TASKS_ROOT.exists():
        print(f"no tasks directory at {TASKS_ROOT}", file=sys.stderr)
        return 1
    rows: list[dict[str, object]] = []
    for task_dir in sorted(TASKS_ROOT.iterdir()):
        state_path = task_dir / "state.json"
        if not state_path.exists():
            continue
        try:
            d = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            continue
        rows.append(_summarize(d))

    if not rows:
        print("no task state files found", file=sys.stderr)
        return 1

    fieldnames = list(rows[0].keys())
    sink = open(out_path, "w", newline="") if out_path else sys.stdout
    try:
        w = csv.DictWriter(sink, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    finally:
        if out_path:
            sink.close()
    print(
        f"wrote {len(rows)} rows to {out_path or 'stdout'}",
        file=sys.stderr,
    )
    return 0


def _summarize(state: dict) -> dict[str, object]:
    subs = state.get("subtasks", []) or []
    agent_kinds = sorted({s.get("agent", "") for s in subs if s.get("agent")})

    total_wall = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    finished = 0
    tool_counts: dict[str, int] = defaultdict(int)
    tool_wall: dict[str, float] = defaultdict(float)
    by_kind: dict[str, int] = defaultdict(int)

    for s in subs:
        if s.get("status") in ("done", "skipped"):
            finished += 1
        total_wall += float(s.get("wall_s", 0.0) or 0.0)
        prompt_tokens += int(s.get("prompt_tokens", 0) or 0)
        completion_tokens += int(s.get("completion_tokens", 0) or 0)
        for tc in s.get("tool_calls") or []:
            name = tc.get("name", "")
            tool_counts[name] += 1
            tool_wall[name] += float(tc.get("wall_s", 0.0) or 0.0)
            if name in _ANALYZERS:
                by_kind["analyzer"] += 1
            elif name in _READERS:
                by_kind["reader"] += 1
            elif name in _ORIENTATION:
                by_kind["orientation"] += 1
            else:
                by_kind["other"] += 1

    breakdown = ";".join(
        f"{n}={tool_counts[n]}@{tool_wall[n]:.1f}s"
        for n in sorted(tool_counts)
    )

    return {
        "task_id": state.get("id", ""),
        "created_at": state.get("created_at", ""),
        "status": state.get("status", ""),
        "agent_kinds": ",".join(agent_kinds),
        "num_subtasks": len(subs),
        "finished_subtasks": finished,
        "max_wall_s": state.get("max_wall_s", ""),
        "total_wall_s": round(total_wall, 1),
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "analyzer_calls": by_kind["analyzer"],
        "reader_calls": by_kind["reader"],
        "orientation_calls": by_kind["orientation"],
        "other_calls": by_kind["other"],
        "tool_breakdown": breakdown,
    }


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(out))
