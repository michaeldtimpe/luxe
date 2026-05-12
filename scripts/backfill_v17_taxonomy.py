#!/usr/bin/env python3
"""Backfill v17 SWE-bench + BFCL runs through the v1.8 taxonomy logger.

Validates that the classifier captures the actual v17 failure
distribution. Produces:
  - acceptance/v17_taxonomy/swebench_n75.json — per-instance outcomes
  - acceptance/v17_taxonomy/bfcl_n1240.json — per-problem outcomes
  - acceptance/v17_taxonomy/aggregate.json — combined aggregation

The output gives us a v1.7 baseline against which v1.8 numbers will be
compared in mechanism-level terms (failure_chain head counts,
intervention conversion rates) rather than aggregate scores alone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from benchmarks.swebench.smoke_inspect import (
    compare_predictions_to_gold,
)
from luxe.agents.outcomes import (
    aggregate_outcomes,
    classify_bfcl_run,
    classify_swebench_run,
)


WORKSPACE = Path.home() / ".luxe" / "swebench-workspace"
RUNS_DIR = Path.home() / ".luxe" / "runs"


def _run_id_for_swebench(instance_id: str) -> str | None:
    log = WORKSPACE / instance_id / "log" / "stdout.log"
    if not log.is_file():
        return None
    for line in log.read_text().splitlines():
        if line.startswith("luxe maintain  run_id="):
            return line.split("=", 1)[1].strip()
    return None


def backfill_swebench_n75() -> dict:
    preds_path = Path("acceptance/swebench/post_specdd_v17_early_bail_n75/rep_1/predictions.json")
    gold_source = Path("benchmarks/swebench/subsets/raw/verified.jsonl")
    if not preds_path.is_file():
        print(f"  predictions missing: {preds_path}")
        return {}

    verdicts = compare_predictions_to_gold(preds_path, gold_source)
    preds = json.loads(preds_path.read_text())
    preds_by_id = {p["instance_id"]: p for p in preds}

    rows = []
    outcomes_list = []
    for v in verdicts:
        inst = v.instance_id
        pred = preds_by_id.get(inst, {})
        has_patch = bool((pred.get("model_patch") or "").strip())
        run_id = _run_id_for_swebench(inst)
        events_path = (RUNS_DIR / run_id / "events.jsonl") if run_id else Path("/nonexistent")
        ep = classify_swebench_run(events_path, has_patch=has_patch, tier=v.tier)
        outcomes_list.append(ep)
        rows.append({
            "instance_id": inst,
            "tier": v.tier,
            "has_patch": has_patch,
            "run_id": run_id,
            **ep.to_dict(),
        })
    return {"rows": rows, "aggregate": aggregate_outcomes(outcomes_list)}


def backfill_bfcl_n1240() -> dict:
    root = Path("acceptance/bfcl/post_specdd_v17_lever1/rep_1")
    if not root.is_dir():
        print(f"  BFCL output missing: {root}")
        return {}

    # Load BFCL ground truth for parallel categories (for expected_call_count)
    from benchmarks.bfcl.adapter import load_ground_truth
    gt_by_category: dict[str, dict] = {}
    for cat in ("parallel", "parallel_multiple"):
        gt_by_category[cat] = load_ground_truth(cat)

    rows = []
    outcomes_list = []
    for category_dir in sorted(root.iterdir()):
        if not category_dir.is_dir():
            continue
        category = category_dir.name
        for pred_file in sorted(category_dir.glob("*.json")):
            d = json.loads(pred_file.read_text())
            pid = d["id"]
            passed = bool(d.get("passed"))
            actual_call_count = len(d.get("actual_calls", []))
            gt = gt_by_category.get(category, {}).get(pid)
            expected = len(gt) if (gt and isinstance(gt, list)) else None
            # No events.jsonl for BFCL agent mode (run_id is None in adapter)
            ep = classify_bfcl_run(None, category=category, passed=passed,
                                   actual_call_count=actual_call_count,
                                   expected_call_count=expected)
            outcomes_list.append(ep)
            rows.append({
                "problem_id": pid,
                "category": category,
                "passed": passed,
                "actual_calls": actual_call_count,
                "expected_calls": expected,
                **ep.to_dict(),
            })
    return {"rows": rows, "aggregate": aggregate_outcomes(outcomes_list)}


def main() -> int:
    out_dir = Path("acceptance/v17_taxonomy")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Backfilling SWE-bench n=75 (post_specdd_v17_early_bail_n75)...")
    swe = backfill_swebench_n75()
    (out_dir / "swebench_n75.json").write_text(json.dumps(swe, indent=2))
    if swe:
        agg = swe["aggregate"]
        print(f"  n={agg['n_total']}")
        print(f"  outcome_counts: {agg['outcome_counts']}")
        print(f"  failure_chain_heads: {agg['failure_chain_head_counts']}")
        print(f"  intervention_counts: {agg['intervention_counts']}")
        print(f"  unclassified_rate: {agg['unclassified_rate']:.1%}")

    print()
    print("Backfilling BFCL n=1240 (post_specdd_v17_lever1)...")
    bfcl = backfill_bfcl_n1240()
    (out_dir / "bfcl_n1240.json").write_text(json.dumps(bfcl, indent=2))
    if bfcl:
        agg = bfcl["aggregate"]
        print(f"  n={agg['n_total']}")
        print(f"  outcome_counts:")
        for outcome, count in sorted(agg["outcome_counts"].items(), key=lambda x: -x[1]):
            print(f"    {outcome:<35} {count}")
        print(f"  unclassified_rate: {agg['unclassified_rate']:.1%}")

    print()
    # Aggregate
    combined = {
        "swebench": swe.get("aggregate", {}),
        "bfcl": bfcl.get("aggregate", {}),
    }
    (out_dir / "aggregate.json").write_text(json.dumps(combined, indent=2))
    print(f"Wrote {out_dir}/aggregate.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
