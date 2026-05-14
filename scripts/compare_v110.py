#!/usr/bin/env python3
"""v1.10 — mechanism-level primary metric + ship-floor evaluator.

v1.9 demonstrated that aggregate `empty_patch` count moves slowly even
when named mechanisms are resolved — multiple latent failure modes
contribute to one aggregate, and interventions fix one mechanism while
exposing another. v1.10 reframes the primary metric around mechanism
resolution:

    v1.10 PRIMARY := (
        CONFIDENCE_COLLAPSE = 0
        AND ABSTAIN_AFTER_INTERVENTION ≤ N
        AND intervention_conversion_rate ≥ X%
    )

`empty_patch` is demoted to a derived secondary; success is the
stability of the agent's internal state post-intervention rather than
the final file output alone.

**Denominator stability** (critical): `intervention_conversion_rate` is
computed AMONG intervention-fired trajectories only, not all
trajectories. Otherwise future trigger-policy changes (the convergence-
score gating in Item 2) distort apparent gains by changing the
denominator rather than the numerator.

Usage:
  python -m scripts.compare_v110 \\
    --predictions acceptance/swebench/post_specdd_v110_n75/rep_1/predictions.json

Compares the v1.10 predictions against v1.9 full-stack baseline by
default. Emits ship-floor table + the per-component breakdown.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from benchmarks.swebench.smoke_inspect import compare_predictions_to_gold
from luxe.agents.outcomes import (
    FailureClass,
    Intervention,
    classify_swebench_run,
)

WORKSPACE = Path.home() / ".luxe" / "swebench-workspace"
RUNS = Path.home() / ".luxe" / "runs"
GOLD = Path("benchmarks/swebench/subsets/raw/verified.jsonl")
V19_FULL = Path("acceptance/swebench/post_specdd_v19_n75/rep_1/predictions.json")


def run_id_for(inst: str) -> str | None:
    log = WORKSPACE / inst / "log" / "stdout.log"
    if not log.is_file():
        return None
    rid = None
    for line in log.read_text().splitlines():
        if line.startswith("luxe maintain  run_id="):
            rid = line.split("=", 1)[1].strip()
    return rid


def _load_manifest(preds_path: Path) -> dict[str, str]:
    """If a sibling run_id_manifest.json exists, return {instance_id: run_id}.
    Otherwise return {}. Manifests are written by
    scripts/save_run_id_manifest.py right after a bench completes —
    they preserve the workspace state so taxonomy classification stays
    correct even after a later run overwrites the workspace
    stdout.log."""
    manifest_path = preds_path.parent / "run_id_manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return {}
    return {k: v["run_id"] for k, v in data.items() if v.get("run_id")}


def classify_arm(preds_path: Path) -> dict:
    """Classify every instance via the v1.8 Track 5 taxonomy.

    Prefers a sibling run_id_manifest.json if present (preserves
    pre-overwrite state); falls back to live workspace lookup."""
    verdicts = compare_predictions_to_gold(preds_path, GOLD)
    preds = json.loads(preds_path.read_text())
    preds_by_id = {p["instance_id"]: p for p in preds}
    manifest = _load_manifest(preds_path)
    out = {}
    for v in verdicts:
        inst = v.instance_id
        pred = preds_by_id.get(inst, {})
        has_patch = bool((pred.get("model_patch") or "").strip())
        run_id = manifest.get(inst) or run_id_for(inst)
        events_path = (RUNS / run_id / "events.jsonl") if run_id else Path("/nonexistent")
        ep = classify_swebench_run(events_path, has_patch=has_patch, tier=v.tier)
        out[inst] = {
            "tier": v.tier,
            "has_patch": has_patch,
            "patch_len": len(pred.get("model_patch") or ""),
            "outcome": ep.outcome.value,
            "interventions": [i.value for i in ep.interventions_fired],
            "failure_chain": ([c.value for c in ep.failure_chain]
                              if ep.failure_chain else None),
        }
    return out


def load_pre_classified_taxonomy(path: Path) -> dict:
    """Load a previously-saved taxonomy artifact
    (acceptance/v19_taxonomy/*.json shape: {"rows": [{instance_id, tier,
    has_patch, outcome, interventions, failure_chain}, ...]}). Used as
    the baseline when live re-classification is unsafe (workspace
    stdout.log has been overwritten by a subsequent run)."""
    d = json.loads(path.read_text())
    out = {}
    for r in d.get("rows", []):
        inst = r["instance_id"]
        out[inst] = {
            "tier": r.get("tier"),
            "has_patch": r.get("has_patch"),
            "patch_len": r.get("patch_len", 0),
            "outcome": r.get("outcome"),
            "interventions": r.get("interventions") or [],
            "failure_chain": r.get("failure_chain"),
        }
    return out


def primary_metric(arm: dict) -> dict:
    """Compute the v1.10 composite primary metric over an arm.

    Components:
      - confidence_collapse: count of CONFIDENCE_COLLAPSE in failure
        chains (target = 0).
      - abstain_after_intervention: count of trajectories with
        ABSTAIN_AFTER_INTERVENTION in chain (target = ≤N; default N=5
        for n=75 scale, ~7% of run).
      - intervention_conversion_rate: among trajectories where any
        commitment-style intervention fired (EARLY_BAIL, WRITE_PRESSURE,
        ACTION_DENSITY_GATE), the fraction that produced a non-empty
        patch. Target = ≥X% (default X=50).

    Denominator stability: conversion rate's denominator is exactly the
    intervention-fired set; changing trigger policy changes the
    denominator AND numerator together so the rate stays comparable.
    """
    cc_count = 0
    abstain_count = 0
    intervention_fired = []  # list of (instance_id, has_patch)
    commitment_set = {
        Intervention.EARLY_BAIL.value,
        Intervention.WRITE_PRESSURE.value,
        Intervention.ACTION_DENSITY_GATE.value,
    }
    for inst, d in arm.items():
        chain = d.get("failure_chain") or []
        if FailureClass.CONFIDENCE_COLLAPSE.value in chain:
            cc_count += 1
        if FailureClass.ABSTAIN_AFTER_INTERVENTION.value in chain:
            abstain_count += 1
        if set(d.get("interventions") or []) & commitment_set:
            intervention_fired.append((inst, d.get("has_patch", False)))

    n_fired = len(intervention_fired)
    n_converted = sum(1 for _, has_patch in intervention_fired if has_patch)
    conversion_rate = (n_converted / n_fired) if n_fired > 0 else 0.0

    return {
        "confidence_collapse": cc_count,
        "abstain_after_intervention": abstain_count,
        "intervention_fired_count": n_fired,
        "intervention_converted_count": n_converted,
        "intervention_conversion_rate": conversion_rate,
    }


def derived_secondary(arm: dict) -> dict:
    tier_counts: Counter[str] = Counter()
    for d in arm.values():
        tier_counts[d["tier"]] += 1
    return {
        "empty_patch": tier_counts.get("empty_patch", 0),
        "strong": tier_counts.get("strong", 0),
        "plausible": tier_counts.get("plausible", 0),
        "wrong_target": tier_counts.get("wrong_target", 0),
        "wrong_location": tier_counts.get("wrong_location", 0),
        "strong_plus_plausible": tier_counts.get("strong", 0) + tier_counts.get("plausible", 0),
    }


def regression_set(target: dict, baseline: dict) -> dict:
    """Cases where baseline produced a non-empty patch but target
    produced empty — by-tier breakdown."""
    out = {
        "strong_to_empty": [],
        "plausible_to_empty": [],
        "wrong_to_empty": [],
    }
    for inst, d in target.items():
        if d["tier"] != "empty_patch":
            continue
        bt = baseline.get(inst, {}).get("tier")
        if bt == "strong":
            out["strong_to_empty"].append(inst)
        elif bt == "plausible":
            out["plausible_to_empty"].append(inst)
        elif bt in ("wrong_target", "wrong_location"):
            out["wrong_to_empty"].append(inst)
    return out


def ship_floors(metric: dict,
                strong_to_empty_vs_v19: list[str],
                *,
                abstain_max: int = 5,
                conversion_min: float = 0.50) -> dict:
    """Apply ship-floor predicates and return per-floor pass/fail."""
    return {
        "CONFIDENCE_COLLAPSE = 0": metric["confidence_collapse"] == 0,
        f"ABSTAIN_AFTER_INTERVENTION ≤ {abstain_max}":
            metric["abstain_after_intervention"] <= abstain_max,
        f"intervention_conversion_rate ≥ {conversion_min:.0%}":
            metric["intervention_conversion_rate"] >= conversion_min,
        "no strong→empty regressions vs v1.9":
            len(strong_to_empty_vs_v19) == 0,
    }


def render_table(label: str, metric: dict, secondary: dict, regressions: dict) -> str:
    lines = [
        f"=== {label} ===",
        f"  PRIMARY (mechanism-level):",
        f"    CONFIDENCE_COLLAPSE                 {metric['confidence_collapse']}",
        f"    ABSTAIN_AFTER_INTERVENTION          {metric['abstain_after_intervention']}",
        f"    intervention_fired                  {metric['intervention_fired_count']}",
        f"    intervention_converted              {metric['intervention_converted_count']}",
        f"    intervention_conversion_rate        "
        f"{metric['intervention_conversion_rate']:.1%}",
        f"  SECONDARY (derived from per-instance tiers):",
        f"    empty_patch                         {secondary['empty_patch']}",
        f"    strong                              {secondary['strong']}",
        f"    plausible                           {secondary['plausible']}",
        f"    wrong_target                        {secondary['wrong_target']}",
        f"    wrong_location                      {secondary['wrong_location']}",
        f"    strong + plausible                  {secondary['strong_plus_plausible']}",
        f"  REGRESSIONS vs v1.9 full-stack baseline:",
        f"    strong → empty                      "
        f"{len(regressions['strong_to_empty'])} {regressions['strong_to_empty']}",
        f"    plausible → empty                   "
        f"{len(regressions['plausible_to_empty'])} {regressions['plausible_to_empty']}",
        f"    wrong → empty                       "
        f"{len(regressions['wrong_to_empty'])} {regressions['wrong_to_empty']}",
    ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--predictions", type=Path, required=True,
                   help="v1.10 predictions.json (the run to evaluate).")
    p.add_argument("--baseline", type=Path, default=V19_FULL,
                   help="Baseline predictions.json (default: v1.9 full-stack). "
                        "Live-classified — workspace stdout.log must still "
                        "point at the baseline run_ids. Use --baseline-taxonomy "
                        "instead if the workspace has been overwritten.")
    p.add_argument("--baseline-taxonomy", type=Path, default=None,
                   help="Pre-classified baseline taxonomy JSON "
                        "(e.g. acceptance/v19_taxonomy/full_stack_swebench_n75.json). "
                        "Overrides --baseline when set; safer when "
                        "the bench workspace has been overwritten.")
    p.add_argument("--label", default="v1.10",
                   help="Label for the target arm in the report.")
    p.add_argument("--abstain-max", type=int, default=5,
                   help="Ship floor for ABSTAIN_AFTER_INTERVENTION (default 5).")
    p.add_argument("--conversion-min", type=float, default=0.50,
                   help="Ship floor for intervention conversion rate (default 0.50).")
    args = p.parse_args()

    if not args.predictions.is_file():
        print(f"  ! predictions missing: {args.predictions}", file=sys.stderr)
        return 1

    print(f"Classifying target ({args.predictions}) …", file=sys.stderr)
    target = classify_arm(args.predictions)

    if args.baseline_taxonomy is not None:
        if not args.baseline_taxonomy.is_file():
            print(f"  ! baseline taxonomy missing: {args.baseline_taxonomy}",
                  file=sys.stderr)
            return 1
        print(f"Loading pre-classified baseline ({args.baseline_taxonomy}) …",
              file=sys.stderr)
        baseline = load_pre_classified_taxonomy(args.baseline_taxonomy)
    else:
        if not args.baseline.is_file():
            print(f"  ! baseline missing: {args.baseline}", file=sys.stderr)
            return 1
        print(f"Classifying baseline ({args.baseline}) …", file=sys.stderr)
        baseline = classify_arm(args.baseline)

    metric = primary_metric(target)
    secondary = derived_secondary(target)
    regressions = regression_set(target, baseline)
    baseline_metric = primary_metric(baseline)
    baseline_secondary = derived_secondary(baseline)

    print()
    print(render_table(args.label, metric, secondary, regressions))
    print()
    print(render_table("v1.9 full-stack (baseline)", baseline_metric,
                       baseline_secondary,
                       {"strong_to_empty": [], "plausible_to_empty": [],
                        "wrong_to_empty": []}))

    print()
    print("=" * 70)
    print("v1.10 SHIP DECISION (composite primary metric — HARD)")
    print("=" * 70)
    floors = ship_floors(metric, regressions["strong_to_empty"],
                         abstain_max=args.abstain_max,
                         conversion_min=args.conversion_min)
    all_green = all(floors.values())
    print(f"  {args.label}: {'✓ SHIP' if all_green else '✗ HOLD'}")
    for desc, ok in floors.items():
        print(f"    {'✓' if ok else '✗'} {desc}")

    print()
    print(f"  baseline conversion_rate (for context): "
          f"{baseline_metric['intervention_conversion_rate']:.1%}")
    print(f"  baseline CONFIDENCE_COLLAPSE: {baseline_metric['confidence_collapse']}")
    print(f"  baseline ABSTAIN_AFTER_INTERVENTION: "
          f"{baseline_metric['abstain_after_intervention']}")

    return 0 if all_green else 2


if __name__ == "__main__":
    sys.exit(main())
