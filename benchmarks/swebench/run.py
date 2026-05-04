"""SWE-bench Verified runner — preds-only mode (no harness; no Docker).

PRELIMINARY (2026-05-03). Runs the luxe agent against a frozen instance
list, captures `model_patch` per instance, writes `predictions.json` in
the harness format. The Docker harness step (which actually scores
predictions against FAIL_TO_PASS / PASS_TO_PASS) is NOT invoked here —
that's `harness.py` and runs once Docker is confirmed.

This lets you validate the agent integration end-to-end (clone, agent,
diff extraction) before paying the Docker setup cost.

Usage:
    # Smoke (3 trivial instances, ~30 min wall):
    python -m benchmarks.swebench.run --smoke 3 \\
        --output acceptance/swebench/smoke_<date>/

    # Full pre-SpecDD baseline:
    python -m benchmarks.swebench.run \\
        --subset benchmarks/swebench/subsets/v1_baseline_n75.json \\
        --output acceptance/swebench/pre_specdd_v141/rep_1/
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from .adapter import run_instance, write_predictions  # noqa: E402
from .fixtures import SweBenchInstance, load_instances_from_json  # noqa: E402
from .stratify import read_subset  # noqa: E402


def _filter_to_subset(
    all_instances: list[SweBenchInstance],
    subset_ids: list[str],
) -> list[SweBenchInstance]:
    """Return instances whose instance_id is in subset_ids, preserving subset order."""
    by_id = {i.instance_id: i for i in all_instances}
    out = []
    for sid in subset_ids:
        if sid in by_id:
            out.append(by_id[sid])
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", type=Path,
                   default=Path("benchmarks/swebench/subsets/raw/verified.jsonl"),
                   help="Local SWE-bench Verified JSONL dump.")
    p.add_argument("--subset", type=Path, default=None,
                   help="Frozen subset JSON (instance_ids list).")
    p.add_argument("--smoke", type=int, default=None,
                   help="Run the first N instances of the dataset (debug).")
    p.add_argument("--output", required=True, type=Path,
                   help="Output dir. predictions.json + per-instance logs land here.")
    p.add_argument("--work-dir", type=Path,
                   default=Path.home() / ".luxe" / "swebench-workspace",
                   help="Repo clones live here.")
    p.add_argument("--config", type=Path,
                   default=Path("configs/single_64gb_swebench.yaml"),
                   help="Pin a luxe config. Defaults to the SWE-bench-specific "
                        "config (swebench_strict_only overlay). Pass "
                        "configs/single_64gb.yaml to opt out.")
    p.add_argument("--per-instance-timeout", type=int, default=1800,
                   help="Seconds per instance before kill.")
    p.add_argument("--model-name", default="luxe-qwen3.6-35b-a3b-6bit",
                   help="Model identifier for the predictions.json rows.")
    args = p.parse_args()

    if not args.dataset.is_file():
        print(f"  dataset not found: {args.dataset}")
        print(f"  run: python -c \"from datasets import load_dataset; "
              f"load_dataset('princeton-nlp/SWE-bench_Verified', split='test')"
              f".to_json('{args.dataset}')\"")
        return 2

    all_instances = load_instances_from_json(args.dataset)
    if args.subset:
        ids = read_subset(args.subset)
        instances = _filter_to_subset(all_instances, ids)
    elif args.smoke:
        instances = all_instances[: args.smoke]
    else:
        instances = all_instances

    args.output.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    print(f"running {len(instances)} instances; work_dir={args.work_dir}")
    print(f"output={args.output}")

    results = []
    started = time.time()

    for i, instance in enumerate(instances):
        t0 = time.time()
        elapsed_min = (t0 - started) / 60
        eta_min = (elapsed_min / max(i, 1)) * (len(instances) - i) if i > 0 else 0.0
        print(f"  [{i+1}/{len(instances)}] {instance.instance_id} "
              f"(elapsed {elapsed_min:.0f}m / ETA {eta_min:.0f}m)", flush=True)

        result = run_instance(
            instance, args.work_dir,
            config=args.config,
            timeout_s=args.per_instance_timeout,
        )
        results.append(result)

        # Save per-instance summary
        inst_summary = args.output / f"{instance.instance_id}.json"
        inst_summary.parent.mkdir(parents=True, exist_ok=True)
        inst_summary.write_text(json.dumps({
            "instance_id": result.instance_id,
            "wall_s": result.wall_s,
            "rc": result.rc,
            "patch_lines": result.model_patch.count("\n"),
            "patch_present": bool(result.model_patch.strip()),
            "error": result.error,
        }, indent=2))

        wall = time.time() - t0
        present = "✓" if result.model_patch.strip() else "✗"
        print(f"      {present} patch_lines={result.model_patch.count(chr(10))} "
              f"wall={wall:.0f}s rc={result.rc}", flush=True)

    # Aggregate predictions
    preds_path = args.output / "predictions.json"
    write_predictions(results, preds_path, model_name=args.model_name)

    # Summary
    n_with_patch = sum(1 for r in results if r.model_patch.strip())
    summary = {
        "n": len(instances),
        "n_with_patch": n_with_patch,
        "patch_rate": (n_with_patch / len(instances)) if instances else 0.0,
        "total_wall_s": sum(r.wall_s for r in results),
        "model_name": args.model_name,
        "started_at": started,
        "finished_at": time.time(),
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))

    print()
    print(f"SWE-bench preds-only run: {n_with_patch}/{len(instances)} produced a non-empty patch")
    print(f"predictions.json written to {preds_path}")
    print("next: feed predictions.json to the Docker harness for FAIL_TO_PASS scoring")
    return 0


if __name__ == "__main__":
    sys.exit(main())
