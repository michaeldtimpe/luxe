#!/usr/bin/env python3
"""Backfill v18 SWE-bench + BFCL runs through the taxonomy logger.
Same shape as backfill_v17_taxonomy.py but pointing at v18 outputs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from benchmarks.swebench.smoke_inspect import compare_predictions_to_gold
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


def backfill_swebench(preds_path: Path, gold_source: Path) -> dict:
    if not preds_path.is_file():
        return {}
    verdicts = compare_predictions_to_gold(preds_path, gold_source)
    preds = json.loads(preds_path.read_text())
    preds_by_id = {p["instance_id"]: p for p in preds}
    outcomes_list = []
    rows = []
    for v in verdicts:
        inst = v.instance_id
        pred = preds_by_id.get(inst, {})
        has_patch = bool((pred.get("model_patch") or "").strip())
        run_id = _run_id_for_swebench(inst)
        events_path = (RUNS_DIR / run_id / "events.jsonl") if run_id else Path("/nonexistent")
        ep = classify_swebench_run(events_path, has_patch=has_patch, tier=v.tier)
        outcomes_list.append(ep)
        rows.append({"instance_id": inst, "tier": v.tier, **ep.to_dict()})
    return {"rows": rows, "aggregate": aggregate_outcomes(outcomes_list)}


def backfill_bfcl(root: Path) -> dict:
    if not root.is_dir():
        return {}
    from benchmarks.bfcl.adapter import load_ground_truth
    gt_by_category = {cat: load_ground_truth(cat) for cat in ("parallel", "parallel_multiple")}
    outcomes_list = []
    rows = []
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
            ep = classify_bfcl_run(None, category=category, passed=passed,
                                   actual_call_count=actual_call_count,
                                   expected_call_count=expected)
            outcomes_list.append(ep)
            rows.append({"id": pid, "category": category, **ep.to_dict()})
    return {"rows": rows, "aggregate": aggregate_outcomes(outcomes_list)}


def main() -> int:
    out_dir = Path("acceptance/v18_taxonomy")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Backfilling SWE-bench v1.8 n=75...")
    swe = backfill_swebench(
        Path("acceptance/swebench/post_specdd_v18_n75/rep_1/predictions.json"),
        Path("benchmarks/swebench/subsets/raw/verified.jsonl"),
    )
    (out_dir / "swebench_n75.json").write_text(json.dumps(swe, indent=2))
    if swe:
        agg = swe["aggregate"]
        print(f"  outcome_counts: {agg['outcome_counts']}")
        print(f"  failure_chain_heads: {agg['failure_chain_head_counts']}")
        print(f"  intervention_counts: {agg['intervention_counts']}")

    print()
    print("Backfilling BFCL v1.8 n=1240...")
    bfcl = backfill_bfcl(Path("acceptance/bfcl/post_specdd_v18_lever1/rep_1"))
    (out_dir / "bfcl_n1240.json").write_text(json.dumps(bfcl, indent=2))
    if bfcl:
        agg = bfcl["aggregate"]
        for outcome, count in sorted(agg["outcome_counts"].items(), key=lambda x: -x[1]):
            print(f"  {outcome:<35} {count}")
        print(f"  unclassified_rate: {agg['unclassified_rate']:.1%}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
