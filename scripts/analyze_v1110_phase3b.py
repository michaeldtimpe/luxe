"""v1.11 Phase 3b — per-signal ablation attribution.

Reads:
  - acceptance/swebench/v1110_phase3b_no_write_off/rep_1/predictions.json
  - acceptance/swebench/v1110_phase3b_score_trend_off/rep_1/predictions.json
  - acceptance/swebench/v1110_archetype_n6/rep_{1,2,3}/predictions.json
    (Phase 3a baseline, all signals on, 3 reps)

For each archetype, compares each ablation's single-rep tier against the
Phase 3a 3-rep modal tier. Reports which signal is causally responsible
for any observed change.

Phase 3b gate: NO ablation produces a deterministic regression vs the
v1.10.5 baseline (the Phase 3a gate already verified the all-signals-on
case; this verifies the ablations don't make things worse).

Usage:
    python -m scripts.analyze_v1110_phase3b
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.cohort_shift_3x3 import TIER_RANK, classify_instance, tier_rank
from scripts.compare_v110 import classify_arm

ARCHETYPES = [
    "sphinx-doc__sphinx-10435",
    "matplotlib__matplotlib-14623",
    "psf__requests-5414",
    "psf__requests-1921",
    "sphinx-doc__sphinx-10323",
    "sympy__sympy-12419",
]
PHASE3A_REPS = [
    REPO / f"acceptance/swebench/v1110_archetype_n6/rep_{r}/predictions.json"
    for r in (1, 2, 3)
]
PHASE3B_RUNS = {
    "no_write_off": REPO / "acceptance/swebench/v1110_phase3b_no_write_off/rep_1/predictions.json",
    "score_trend_off": REPO / "acceptance/swebench/v1110_phase3b_score_trend_off/rep_1/predictions.json",
}
V1105_TAX_PATHS = [
    REPO / "acceptance/v1105_taxonomy/v1105_n75_full_stack_swebench.json",
    REPO / "acceptance/v1105_taxonomy/v1105_n75_rep_2_full_stack_swebench.json",
    REPO / "acceptance/v1105_taxonomy/v1105_n75_rep_3_full_stack_swebench.json",
]


def modal_tier(tiers: list[str]) -> str:
    """Most-common tier in a multi-rep list. Ties → best-ranked (lowest rank)."""
    if not tiers:
        return "MISSING"
    counts = Counter(tiers)
    max_count = max(counts.values())
    candidates = [t for t, c in counts.items() if c == max_count]
    return min(candidates, key=tier_rank)


def load_tiers_per_instance(rep_paths: list[Path]) -> dict[str, list[str]]:
    """Per-instance list of tiers across N rep predictions.json files."""
    per_instance: dict[str, list[str]] = {iid: [] for iid in ARCHETYPES}
    for p in rep_paths:
        if not p.is_file():
            return {}
        rows = classify_arm(p)
        for iid in ARCHETYPES:
            t = rows.get(iid, {}).get("tier", "MISSING")
            per_instance[iid].append(t)
    return per_instance


def load_v1105_baseline_tiers() -> dict[str, tuple[str, ...]]:
    taxonomies = []
    for p in V1105_TAX_PATHS:
        if not p.is_file():
            return {}
        rows = json.loads(p.read_text())["rows"]
        taxonomies.append({r["instance_id"]: r["tier"] for r in rows})
    out: dict[str, tuple[str, ...]] = {}
    for iid in ARCHETYPES:
        out[iid] = tuple(t.get(iid, "MISSING") for t in taxonomies)
    return out


def main() -> int:
    print("=" * 78)
    print("v1.11 Phase 3b — per-signal ablation attribution")
    print("=" * 78)

    phase3a = load_tiers_per_instance(PHASE3A_REPS)
    v1105 = load_v1105_baseline_tiers()
    if not phase3a or not v1105:
        print("FATAL: missing Phase 3a or v1.10.5 baseline data", file=sys.stderr)
        return 2

    ablations: dict[str, dict[str, str]] = {}
    for label, path in PHASE3B_RUNS.items():
        if not path.is_file():
            print(f"FATAL: missing ablation predictions: {path}", file=sys.stderr)
            return 2
        rows = classify_arm(path)
        ablations[label] = {iid: rows.get(iid, {}).get("tier", "MISSING") for iid in ARCHETYPES}

    print()
    header = f'{"archetype":<32}{"v1.10.5 (3reps)":<24}{"3a all-on modal":<18}{"3b no_write_off":<18}{"3b score_trend_off":<20}'
    print(header)
    print("-" * len(header))
    det_regressions_per_ablation = {"no_write_off": 0, "score_trend_off": 0}
    for iid in ARCHETYPES:
        base = v1105[iid]
        p3a_modal = modal_tier(phase3a[iid])
        ab_a = ablations["no_write_off"][iid]
        ab_b = ablations["score_trend_off"][iid]
        print(f"{iid:<32}{'/'.join(base):<24}{p3a_modal:<18}{ab_a:<18}{ab_b:<20}")
        # Deterministic-regression: ablation tier is STRICTLY WORSE than
        # the worst tier observed across all v1.10.5 reps (i.e., the
        # ablation outcome was never seen in v1.10.5's distribution, so
        # variance alone cannot explain it).
        base_worst = max((tier_rank(t) for t in base), default=0)
        if tier_rank(ab_a) > base_worst:
            det_regressions_per_ablation["no_write_off"] += 1
        if tier_rank(ab_b) > base_worst:
            det_regressions_per_ablation["score_trend_off"] += 1

    print()
    print("--- Phase 3b gate (no ablation should produce a deterministic regression) ---")
    for k, n in det_regressions_per_ablation.items():
        status = "PASS" if n == 0 else "FAIL"
        print(f"  ablation {k:<20} det-regressions vs v1.10.5: {n}   {status}")

    # Attribution analysis
    print()
    print("--- Attribution (when does the ablation tier differ from the all-on Phase 3a modal?) ---")
    for iid in ARCHETYPES:
        p3a_modal = modal_tier(phase3a[iid])
        a_differs = ablations["no_write_off"][iid] != p3a_modal
        b_differs = ablations["score_trend_off"][iid] != p3a_modal
        if a_differs or b_differs:
            tag = []
            if a_differs:
                tag.append("no_write→DIFF")
            if b_differs:
                tag.append("score_trend→DIFF")
            print(f"  {iid}: {' | '.join(tag)}")
    if not any(
        ablations["no_write_off"][iid] != modal_tier(phase3a[iid])
        or ablations["score_trend_off"][iid] != modal_tier(phase3a[iid])
        for iid in ARCHETYPES
    ):
        print("  (no attribution signal — all ablations match Phase 3a all-on; expected at "
              "archetype trajectory lengths where modulation stays at 1.0)")

    if any(n > 0 for n in det_regressions_per_ablation.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
