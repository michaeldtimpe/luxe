#!/usr/bin/env python3
"""Audit the 18 v3 empty_patch instances from the SWE-bench n=75 run.

Classifies each run by its abort_reason / terminal events, extracts per-run
summary metrics for early-bail threshold derivation, and dumps per-step rows
for distribution analysis. See plan `bubbly-plotting-gosling.md` Phase B.1.

Findings 2026-05-11 — the 18 decompose into FOUR classes, not one:
  no_abort           10 — model clean-exited with prose, never wrote
  context_exhausted   4 — prompt > 32k tokens (FIXED by 32k→48k bump 2026-05-10)
  stuck_loop          3 — _MAX_CONSECUTIVE_REPEAT_STEPS fired at step 17-22
  max_steps_reached   1 — hit 30-step cap

The "agent_bailed → all 18" Explore-agent claim was wrong. Early-bail at
step >= 4 with reads >= 4 catches 7 of 10 no_abort + intercepts the 3
stuck_loop and 1 max_steps cases BEFORE their terminal detectors fire.
Short-trace no_abort (3 cases: 14096, 3187, 10614 — exit at step 2-3 with
8000+ completion tokens) need a different signal; deferred.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

# Instance → run_id mapping derived from
# ~/.luxe/swebench-workspace/<instance>/log/stdout.log first line.
INSTANCES: dict[str, str] = {
    "astropy__astropy-13453": "6dd4264aaf0f",
    "astropy__astropy-13977": "d46e588ae44e",
    "astropy__astropy-14096": "241957f554dd",
    "astropy__astropy-8707": "a77fc5686b19",
    "django__django-11734": "4beafcc474f1",
    "matplotlib__matplotlib-13989": "91ee92029a99",
    "matplotlib__matplotlib-20488": "02b089569139",
    "matplotlib__matplotlib-20826": "24364ed57610",
    "matplotlib__matplotlib-24870": "1af1ee034cb7",
    "mwaskom__seaborn-3069": "35294c1192ef",
    "mwaskom__seaborn-3187": "b8c32a0f5d3a",
    "psf__requests-6028": "13e34d00afb3",
    "pydata__xarray-2905": "3cbe51b65475",
    "pydata__xarray-6938": "0f7102694275",
    "pylint-dev__pylint-4604": "b8ad4f994774",
    "sphinx-doc__sphinx-10323": "6ac36fe5f9ba",
    "sphinx-doc__sphinx-10435": "5869cbdca240",
    "sphinx-doc__sphinx-10614": "aa8154a5fd82",
}

WRITE_TOOLS = {"write_file", "edit_file"}
RUNS_DIR = Path.home() / ".luxe" / "runs"


def classify_abort(reason: str) -> str:
    if not reason:
        return "no_abort"
    r = reason.lower()
    if "prompt too long" in r or "max context window" in r:
        return "context_exhausted"
    if "backend error" in r:
        return "backend_error"
    if "stuck" in r:
        return "stuck_loop"
    return "other"


def audit_run(run_id: str) -> dict:
    path = RUNS_DIR / run_id / "events.jsonl"
    if not path.is_file():
        return {"run_id": run_id, "trace_missing": True}

    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    tool_calls = [e for e in events if e.get("kind") == "tool_call" and e.get("phase") == "main"]
    summary = next((e for e in events if e.get("kind") == "single_mode_done"), {})

    reads = sum(1 for tc in tool_calls if tc.get("name", "").strip() not in WRITE_TOOLS)
    writes = sum(1 for tc in tool_calls if tc.get("name", "").strip() in WRITE_TOOLS)
    first_write_step = next(
        (tc["step"] for tc in tool_calls if tc.get("name", "").strip() in WRITE_TOOLS),
        None,
    )

    return {
        "run_id": run_id,
        "trace_missing": False,
        "total_tool_calls": len(tool_calls),
        "total_reads": reads,
        "total_writes": writes,
        "first_write_step": first_write_step,
        "total_steps": (tool_calls[-1]["step"] + 1) if tool_calls else 0,
        "completion_tokens": summary.get("completion_tokens", 0),
        "prompt_tokens": summary.get("prompt_tokens", 0),
        "aborted": summary.get("aborted", False),
        "abort_reason": summary.get("abort_reason", ""),
        "abort_class": classify_abort(summary.get("abort_reason", "")),
        "wall_s": summary.get("wall_s", 0.0),
    }


def emit_step_rows(run_id: str, instance_id: str, writer: csv.writer) -> None:
    path = RUNS_DIR / run_id / "events.jsonl"
    if not path.is_file():
        return
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    tool_calls = [e for e in events if e.get("kind") == "tool_call" and e.get("phase") == "main"]
    reads_so_far = 0
    writes_so_far = 0
    for tc in tool_calls:
        name = tc.get("name", "").strip()
        if name in WRITE_TOOLS:
            writes_so_far += 1
        else:
            reads_so_far += 1
        writer.writerow([
            instance_id,
            run_id,
            tc.get("step"),
            name,
            reads_so_far,
            writes_so_far,
            tc.get("bytes_out", 0),
        ])


def main() -> int:
    summaries = []
    out_dir = Path("acceptance/v17_audit")
    out_dir.mkdir(parents=True, exist_ok=True)

    rows_path = out_dir / "v3_empties_per_step.csv"
    with rows_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["instance_id", "run_id", "step", "tool_name",
                    "reads_so_far", "writes_so_far", "bytes_out"])
        for instance_id, run_id in INSTANCES.items():
            summary = audit_run(run_id)
            summary["instance_id"] = instance_id
            summaries.append(summary)
            emit_step_rows(run_id, instance_id, w)

    summary_path = out_dir / "v3_empties_summary.csv"
    with summary_path.open("w", newline="") as f:
        cols = ["instance_id", "run_id", "abort_class", "aborted", "total_steps",
                "total_tool_calls", "total_reads", "total_writes",
                "first_write_step", "completion_tokens", "prompt_tokens",
                "wall_s", "abort_reason"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in summaries:
            w.writerow({k: s.get(k, "") for k in cols})

    print(f"Wrote {summary_path}")
    print(f"Wrote {rows_path}")
    print()
    print("Per-instance summary:")
    print(f"{'instance':<35} {'class':<18} {'steps':>5} {'reads':>5} {'writes':>6} {'first_wr':>8} {'compl_tok':>9} {'prompt_tok':>10}")
    for s in sorted(summaries, key=lambda x: (x.get("abort_class") or "", x["instance_id"])):
        print(f"{s['instance_id']:<35} {s.get('abort_class', '?'):<18} "
              f"{s.get('total_steps', 0):>5} {s.get('total_reads', 0):>5} "
              f"{s.get('total_writes', 0):>6} {str(s.get('first_write_step', '-')):>8} "
              f"{s.get('completion_tokens', 0):>9} {s.get('prompt_tokens', 0):>10}")
    print()
    klass_counts = {}
    for s in summaries:
        klass_counts[s.get("abort_class", "?")] = klass_counts.get(s.get("abort_class", "?"), 0) + 1
    print("Abort-class tally:")
    for k, v in sorted(klass_counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<20} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
