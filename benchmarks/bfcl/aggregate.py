"""Post-hoc summary aggregator for BFCL runs.

Usage:
    python -m benchmarks.bfcl.aggregate --output acceptance/bfcl/pre_specdd_v141/rep_1/

Walks every <category>/*.json under output_dir and rebuilds summary.json
from the per-problem files. Useful when run.py was invoked multiple
times for different categories against the same output dir (each
invocation overwrites summary.json with only its own categories).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True, type=Path,
                   help="BFCL output dir to aggregate.")
    args = p.parse_args()

    if not args.output.is_dir():
        print(f"  output dir not found: {args.output}")
        return 2

    summary = {"categories": {}, "totals": {}}
    grand_pass = 0
    grand_total = 0
    grand_wall = 0.0
    grand_prompt = 0
    grand_completion = 0

    for cat_dir in sorted(p for p in args.output.iterdir() if p.is_dir()):
        category = cat_dir.name
        per_problem = sorted(cat_dir.glob("*.json"))
        if not per_problem:
            continue
        cat_pass = 0
        cat_wall = 0.0
        cat_prompt = 0
        cat_completion = 0
        for path in per_problem:
            try:
                row = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            cat_pass += int(bool(row.get("passed")))
            cat_wall += float(row.get("wall_s", 0))
            cat_prompt += int(row.get("prompt_tokens", 0))
            cat_completion += int(row.get("completion_tokens", 0))
        n = len(per_problem)
        summary["categories"][category] = {
            "n": n,
            "passed": cat_pass,
            "pass_rate": (cat_pass / n) if n else 0.0,
            "total_wall_s": cat_wall,
            "avg_wall_s": (cat_wall / n) if n else 0.0,
            "total_prompt_tokens": cat_prompt,
            "total_completion_tokens": cat_completion,
        }
        grand_pass += cat_pass
        grand_total += n
        grand_wall += cat_wall
        grand_prompt += cat_prompt
        grand_completion += cat_completion

    summary["totals"] = {
        "n": grand_total,
        "passed": grand_pass,
        "pass_rate": (grand_pass / grand_total) if grand_total else 0.0,
        "total_wall_s": grand_wall,
        "total_prompt_tokens": grand_prompt,
        "total_completion_tokens": grand_completion,
    }
    out_path = args.output / "summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"Aggregated {grand_total} problems across {len(summary['categories'])} categories")
    for cat, stats in summary["categories"].items():
        print(f"  {cat}: {stats['passed']}/{stats['n']} = {stats['pass_rate']:.2%}  avg_wall={stats['avg_wall_s']:.1f}s")
    print(f"  TOTAL: {grand_pass}/{grand_total} = {summary['totals']['pass_rate']:.2%}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
