"""v1.11 Phase 3a — archetype-6 outcomes vs v1.10.5 baseline.

Reads:
  - acceptance/swebench/v1110_archetype_n6/rep_{1,2,3}/predictions.json
  - acceptance/v1105_taxonomy/v1105_n75[_rep_2|_rep_3]_full_stack_swebench.json

Generates v1.11 taxonomies on-the-fly (compare_v110.classify_arm) then
runs cohort_shift_3x3 logic filtered to the 6 archetype instance_ids.

Pass gate: ≥4/6 archetypes preserve OR improve their v1.10.5 baseline
tier; 0 deterministic_loss.

Usage:
    python -m scripts.analyze_v1110_phase3a
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.compare_v110 import classify_arm
from scripts.cohort_shift_3x3 import (
    build_instance_tiers,
    classify_instance,
    render,
)

V1110_REPS = [
    REPO / "acceptance/swebench/v1110_archetype_n6/rep_1/predictions.json",
    REPO / "acceptance/swebench/v1110_archetype_n6/rep_2/predictions.json",
    REPO / "acceptance/swebench/v1110_archetype_n6/rep_3/predictions.json",
]
V1105_TAX_PATHS = [
    REPO / "acceptance/v1105_taxonomy/v1105_n75_full_stack_swebench.json",
    REPO / "acceptance/v1105_taxonomy/v1105_n75_rep_2_full_stack_swebench.json",
    REPO / "acceptance/v1105_taxonomy/v1105_n75_rep_3_full_stack_swebench.json",
]
ARCHETYPES = [
    "sphinx-doc__sphinx-10435",
    "matplotlib__matplotlib-14623",
    "psf__requests-5414",
    "psf__requests-1921",
    "sphinx-doc__sphinx-10323",
    "sympy__sympy-12419",
]


def load_v1105_archetype_tiers() -> dict[str, tuple[str, ...]]:
    """Filter v1.10.5 baseline 3-rep taxonomies to just the 6 archetypes."""
    taxonomies = []
    for p in V1105_TAX_PATHS:
        if not p.is_file():
            print(f"FATAL: missing v1.10.5 baseline taxonomy: {p}", file=sys.stderr)
            return {}
        rows = json.loads(p.read_text())["rows"]
        taxonomies.append({r["instance_id"]: r["tier"] for r in rows})
    full = build_instance_tiers(taxonomies)
    return {iid: full[iid] for iid in ARCHETYPES if iid in full}


def load_v1110_archetype_tiers() -> dict[str, tuple[str, ...]]:
    """Classify each v1.11 predictions.json on-the-fly via compare_v110.classify_arm."""
    taxonomies = []
    for p in V1110_REPS:
        if not p.is_file():
            print(f"FATAL: missing v1.11 predictions: {p}", file=sys.stderr)
            return {}
        rows = classify_arm(p)
        taxonomies.append({iid: r["tier"] for iid, r in rows.items()})
    full = build_instance_tiers(taxonomies)
    return {iid: full[iid] for iid in ARCHETYPES if iid in full}


def main() -> int:
    print("=" * 72)
    print("v1.11 Phase 3a — archetype-6 preflight (vs v1.10.5 baseline)")
    print("=" * 72)

    cycle_a = load_v1105_archetype_tiers()
    cycle_b = load_v1110_archetype_tiers()
    if not cycle_a or not cycle_b:
        return 2

    print(f"\nv1.10.5 archetypes covered: {len(cycle_a)} / 6")
    print(f"v1.11   archetypes covered: {len(cycle_b)} / 6")

    verdicts = {}
    for iid in ARCHETYPES:
        a = cycle_a.get(iid, tuple())
        b = cycle_b.get(iid, tuple())
        verdicts[iid] = classify_instance(a, b)

    print(render(verdicts, "v1.10.5", "v1.11"))

    # Phase 3a pass gate
    det_loss = sum(1 for v in verdicts.values() if v["verdict"] == "deterministic_loss")
    preserve_or_improve = sum(
        1 for v in verdicts.values()
        if v["verdict"] in ("byte_identical", "deterministic_gain", "modal_gain", "noise")
    )
    print("\n--- Phase 3a gate ---")
    print(f"  preserve-or-improve: {preserve_or_improve} / 6   "
          f"(required: >= 4)   {'PASS' if preserve_or_improve >= 4 else 'FAIL'}")
    print(f"  deterministic_loss:  {det_loss}                  "
          f"(required: == 0)   {'PASS' if det_loss == 0 else 'FAIL'}")

    if preserve_or_improve < 4 or det_loss > 0:
        print("\nPhase 3a FAILED — investigate before Phase 4 n=14 smoke")
        return 1
    print("\nPhase 3a PASSED — clear to proceed to Phase 3b ablation sweep")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
