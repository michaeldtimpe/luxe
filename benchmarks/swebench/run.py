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

from .adapter import (  # noqa: E402
    SweBenchInvocationResult,
    run_instance,
    write_predictions,
)
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

    # Count cached vs fresh up front so the startup banner is honest.
    cached_count = 0
    for instance in instances:
        sp = args.output / f"{instance.instance_id}.json"
        if sp.exists():
            try:
                cached_summary = json.loads(sp.read_text())
                if "model_patch" in cached_summary:
                    cached_count += 1
            except (json.JSONDecodeError, OSError):
                pass
    if cached_count:
        print(f"  {cached_count}/{len(instances)} already on disk, "
              f"will run {len(instances) - cached_count} fresh", flush=True)

    runtime_wall = 0.0
    runtime_done = 0

    for i, instance in enumerate(instances):
        inst_summary_path = args.output / f"{instance.instance_id}.json"

        cached = None
        if inst_summary_path.exists():
            try:
                cached = json.loads(inst_summary_path.read_text())
                # Legacy summaries (pre-resume) don't carry the patch;
                # treat them as a re-run candidate.
                if "model_patch" not in cached:
                    cached = None
            except (json.JSONDecodeError, OSError):
                cached = None

        if cached is not None:
            result = SweBenchInvocationResult(
                instance_id=cached["instance_id"],
                model_patch=cached.get("model_patch", ""),
                wall_s=float(cached.get("wall_s", 0.0)),
                rc=int(cached.get("rc", 0)),
                error=cached.get("error", ""),
            )
            results.append(result)
            present = "✓" if result.model_patch.strip() else "✗"
            print(f"  [{i+1}/{len(instances)}] {instance.instance_id} "
                  f"(cached)  {present}", flush=True)
            continue

        t0 = time.time()
        if runtime_done > 0:
            avg_s = runtime_wall / runtime_done
            remaining_fresh = (len(instances) - i) - max(
                0, cached_count - i  # cached items still ahead
            )
            eta_min = (avg_s * max(0, remaining_fresh)) / 60
            eta_str = f" / ETA {eta_min:.0f}m (avg {avg_s:.0f}s)"
        else:
            eta_str = ""
        elapsed_min = (t0 - started) / 60
        print(f"  [{i+1}/{len(instances)}] {instance.instance_id} "
              f"(elapsed {elapsed_min:.0f}m{eta_str})", flush=True)

        result = run_instance(
            instance, args.work_dir,
            config=args.config,
            timeout_s=args.per_instance_timeout,
        )
        results.append(result)

        elapsed = time.time() - t0
        runtime_wall += elapsed
        runtime_done += 1

        # Save per-instance summary, including the model_patch so a
        # crashed/Ctrl-C run can resume by skipping completed instances.
        # ensure_repo() does `git clean -fdx` on every iteration, so we
        # cannot re-derive the patch from the workspace after the fact.
        inst_summary_path.parent.mkdir(parents=True, exist_ok=True)
        inst_summary_path.write_text(json.dumps({
            "instance_id": result.instance_id,
            "wall_s": result.wall_s,
            "rc": result.rc,
            "patch_lines": result.model_patch.count("\n"),
            "patch_present": bool(result.model_patch.strip()),
            "error": result.error,
            "model_patch": result.model_patch,
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
