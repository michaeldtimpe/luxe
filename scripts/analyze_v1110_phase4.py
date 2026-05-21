"""v1.11 Phase 4 — n=14 smoke vs v1.10.5 n=75 (filtered).

Reads:
  - acceptance/swebench/v1110_smoke_n14/rep_{1,2,3}/predictions.json
  - acceptance/v1105_taxonomy/v1105_n75[_rep_2|_rep_3]_full_stack_swebench.json
    (filtered to the 14 smoke-subset instances)

Runs cohort_shift_3x3 logic. Phase 4 gate: ≤ 1 new regression vs v1.10.5
(per plan; more lenient than Phase 3a's 0-loss because n=14 includes
high-variance instances).

Usage:
    python -m scripts.analyze_v1110_phase4
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.cohort_shift_3x3 import build_instance_tiers, classify_instance, render
from scripts.compare_v110 import classify_arm

PHASE4_REPS = [
    REPO / f"acceptance/swebench/v1110_smoke_n14/rep_{r}/predictions.json"
    for r in (1, 2, 3)
]
V1105_TAX_PATHS = [
    REPO / "acceptance/v1105_taxonomy/v1105_n75_full_stack_swebench.json",
    REPO / "acceptance/v1105_taxonomy/v1105_n75_rep_2_full_stack_swebench.json",
    REPO / "acceptance/v1105_taxonomy/v1105_n75_rep_3_full_stack_swebench.json",
]
SMOKE_SUBSET = REPO / "benchmarks/swebench/subsets/v19_smoke_n14.json"


def main() -> int:
    print("=" * 72)
    print("v1.11 Phase 4 — n=14 smoke (vs v1.10.5 n=75 filtered)")
    print("=" * 72)

    instance_ids = json.loads(SMOKE_SUBSET.read_text())["instance_ids"]
    print(f"\nSmoke subset n=14 instances: {len(instance_ids)}")

    # Load v1.11 Phase 4 reps and v1.10.5 baseline, both filtered to subset.
    cycle_b_taxs = []
    for p in PHASE4_REPS:
        if not p.is_file():
            print(f"FATAL: missing v1.11 Phase 4 predictions: {p}", file=sys.stderr)
            return 2
        rows = classify_arm(p)
        cycle_b_taxs.append({iid: r["tier"] for iid, r in rows.items() if iid in instance_ids})
    cycle_b = build_instance_tiers(cycle_b_taxs)
    cycle_b = {iid: cycle_b[iid] for iid in instance_ids if iid in cycle_b}

    cycle_a_taxs = []
    for p in V1105_TAX_PATHS:
        if not p.is_file():
            print(f"FATAL: missing v1.10.5 baseline taxonomy: {p}", file=sys.stderr)
            return 2
        rows = json.loads(p.read_text())["rows"]
        cycle_a_taxs.append({r["instance_id"]: r["tier"] for r in rows if r["instance_id"] in instance_ids})
    cycle_a = build_instance_tiers(cycle_a_taxs)
    cycle_a = {iid: cycle_a[iid] for iid in instance_ids if iid in cycle_a}

    verdicts = {iid: classify_instance(cycle_a.get(iid, tuple()), cycle_b.get(iid, tuple()))
                for iid in instance_ids}

    print(render(verdicts, "v1.10.5", "v1.11"))

    # Phase 4 gate
    det_loss = sum(1 for v in verdicts.values() if v["verdict"] == "deterministic_loss")
    modal_loss = sum(1 for v in verdicts.values() if v["verdict"] == "modal_loss")
    new_regressions = det_loss + modal_loss
    print()
    print("--- Phase 4 gate (n=14 smoke; <= 1 new regression vs v1.10.5) ---")
    print(f"  deterministic_loss: {det_loss}")
    print(f"  modal_loss:         {modal_loss}")
    print(f"  total regressions:  {new_regressions}   "
          f"{'PASS (<= 1)' if new_regressions <= 1 else 'FAIL (> 1)'}")
    return 0 if new_regressions <= 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
