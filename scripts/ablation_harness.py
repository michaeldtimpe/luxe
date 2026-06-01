#!/usr/bin/env python
"""Run the SWE-bench Docker harness against a predictions.json file.

Standalone wrapper around `benchmarks.swebench.harness.run_harness`. Used
by `scripts/ablation_run_cell.sh` to score SWE-bench preds with the
official FAIL_TO_PASS pass-rate evaluation, since the package has no
CLI of its own.

Usage:
  python scripts/ablation_harness.py \\
      --predictions acceptance/.../predictions.json \\
      --output-dir acceptance/.../harness \\
      --run-id <cell_name>_<benchmark_subset> \\
      [--max-workers 2] [--timeout 1800]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ablation_harness.py")
    p.add_argument("--predictions", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--run-id", required=True)
    p.add_argument("--max-workers", type=int, default=2)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--cache-level", default="env", choices=["none", "base", "env", "instance"])
    args = p.parse_args(argv)

    if not args.predictions.exists():
        print(f"missing predictions: {args.predictions}", file=sys.stderr)
        return 2

    from benchmarks.swebench.harness import run_harness, write_harness_summary

    args.output_dir.mkdir(parents=True, exist_ok=True)
    print(f"  harness: predictions={args.predictions} → {args.output_dir}")
    print(f"  run_id={args.run_id} max_workers={args.max_workers} timeout={args.timeout}s")

    results = run_harness(
        predictions_path=args.predictions,
        output_dir=args.output_dir,
        run_id=args.run_id,
        max_workers=args.max_workers,
        timeout_per_instance_s=args.timeout,
        cache_level=args.cache_level,
    )
    summary_path = args.output_dir.parent / "harness_summary.json"
    write_harness_summary(results, summary_path)

    n = len(results)
    n_resolved = sum(1 for r in results if r.get("resolved"))
    n_errored = sum(1 for r in results if r.get("error"))
    rate = n_resolved / n if n else 0.0
    print(f"  harness DONE: resolved {n_resolved}/{n} ({rate:.2%}), errored {n_errored}")
    print(f"  summary written: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
