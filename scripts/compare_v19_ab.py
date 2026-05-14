#!/usr/bin/env python3
"""Phase D A/B comparison: full-stack vs gate-only on n=75.

Reads two prediction sets and emits a ship-floor decision table:
  - empty_patch count (target ≤13)
  - strong count (target ≥18)
  - strong+plausible count (target ≥35)
  - confidence-collapse class size (target =0)
  - wrong→empty regressions vs v17 baseline (target =0)
  - careful-strong losses vs v18 baseline (target ≤1)

Usage:
  python -m scripts.compare_v19_ab \
    --full acceptance/swebench/post_specdd_v19_n75/rep_1/predictions.json \
    --gate-only acceptance/swebench/post_specdd_v19_n75_gate_only/rep_1/predictions.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from benchmarks.swebench.smoke_inspect import compare_predictions_to_gold
from luxe.agents.outcomes import classify_swebench_run, Intervention, FailureClass

WORKSPACE = Path.home() / ".luxe" / "swebench-workspace"
RUNS = Path.home() / ".luxe" / "runs"
GOLD = Path("benchmarks/swebench/subsets/raw/verified.jsonl")
V17 = Path("acceptance/v17_taxonomy/swebench_n75.json")
V18 = Path("acceptance/v18_taxonomy/swebench_n75.json")


def run_id_for(inst: str) -> str | None:
    log = WORKSPACE / inst / "log" / "stdout.log"
    if not log.is_file():
        return None
    rid = None
    for line in log.read_text().splitlines():
        if line.startswith("luxe maintain  run_id="):
            rid = line.split("=", 1)[1].strip()
    return rid


def classify_arm(preds_path: Path) -> dict:
    """Classify every instance via the v1.8 Track 5 taxonomy. Returns
    {instance_id: {tier, outcome, interventions, failure_chain}}."""
    verdicts = compare_predictions_to_gold(preds_path, GOLD)
    preds = json.loads(preds_path.read_text())
    preds_by_id = {p["instance_id"]: p for p in preds}
    out = {}
    for v in verdicts:
        inst = v.instance_id
        pred = preds_by_id.get(inst, {})
        has_patch = bool((pred.get("model_patch") or "").strip())
        run_id = run_id_for(inst)
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


def tier_counts(arm: dict) -> dict:
    out = {"strong": 0, "plausible": 0, "wrong_target": 0,
           "wrong_location": 0, "empty_patch": 0}
    for d in arm.values():
        t = d["tier"]
        if t in out:
            out[t] += 1
    return out


def confidence_collapse_count(arm: dict) -> int:
    return sum(1 for d in arm.values()
               if d["failure_chain"] and "CONFIDENCE_COLLAPSE" in d["failure_chain"])


def regression_counts(arm: dict, baseline_rows: list) -> dict:
    bl = {r["instance_id"]: r["tier"] for r in baseline_rows}
    wrong_to_empty = []   # baseline=wrong_*, arm=empty
    strong_to_empty = []  # baseline=strong, arm=empty
    plausible_to_empty = []
    for inst, d in arm.items():
        if d["tier"] != "empty_patch":
            continue
        bt = bl.get(inst)
        if bt in ("wrong_target", "wrong_location"):
            wrong_to_empty.append(inst)
        elif bt == "strong":
            strong_to_empty.append(inst)
        elif bt == "plausible":
            plausible_to_empty.append(inst)
    return {
        "wrong_to_empty": wrong_to_empty,
        "strong_to_empty": strong_to_empty,
        "plausible_to_empty": plausible_to_empty,
    }


def ship_floor_table(arm: dict, label: str) -> dict:
    tc = tier_counts(arm)
    cc = confidence_collapse_count(arm)
    v17_rows = json.loads(V17.read_text())["rows"]
    v18_rows = json.loads(V18.read_text())["rows"]
    v17_reg = regression_counts(arm, v17_rows)
    v18_reg = regression_counts(arm, v18_rows)
    return {
        "label": label,
        "empty_patch": tc["empty_patch"],
        "strong": tc["strong"],
        "strong_plus_plausible": tc["strong"] + tc["plausible"],
        "wrong_target": tc["wrong_target"],
        "confidence_collapse": cc,
        "wrong_to_empty_vs_v17": v17_reg["wrong_to_empty"],
        "strong_to_empty_vs_v18": v18_reg["strong_to_empty"],
        "plausible_to_empty_vs_v18": v18_reg["plausible_to_empty"],
        "all_strong_to_empty_vs_v18": v18_reg["strong_to_empty"],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--full", type=Path,
                   default=Path("acceptance/swebench/post_specdd_v19_n75/rep_1/predictions.json"))
    p.add_argument("--gate-only", type=Path,
                   default=Path("acceptance/swebench/post_specdd_v19_n75_gate_only/rep_1/predictions.json"))
    args = p.parse_args()

    arms = {}
    for label, path in [("full_stack", args.full), ("gate_only", args.gate_only)]:
        if not path.is_file():
            print(f"  ! missing: {path}")
            continue
        print(f"\nClassifying {label} ({path}) …")
        arms[label] = classify_arm(path)

    if not arms:
        print("No arms found — both prediction files missing.")
        return 1

    print()
    print("=" * 90)
    print("v1.9 SHIP-FLOOR A/B (n=75)")
    print("=" * 90)
    print(f"{'metric':38s} {'target':>10s}  {'full_stack':>12s}  {'gate_only':>12s}")
    print("-" * 90)
    sf = {label: ship_floor_table(arm, label) for label, arm in arms.items()}
    rows = [
        ("empty_patch", "≤13", "empty_patch"),
        ("strong", "≥18", "strong"),
        ("strong + plausible", "≥35", "strong_plus_plausible"),
        ("wrong_target", "info", "wrong_target"),
        ("CONFIDENCE_COLLAPSE class", "=0", "confidence_collapse"),
    ]
    for label, target, key in rows:
        f = sf.get("full_stack", {}).get(key, "—")
        g = sf.get("gate_only", {}).get(key, "—")
        print(f"{label:38s} {target:>10s}  {str(f):>12s}  {str(g):>12s}")

    print()
    print("Regression instances (v17 wrong_* → v19 empty):")
    for label in arms:
        rs = sf[label]["wrong_to_empty_vs_v17"]
        print(f"  {label}: {len(rs)} — {rs}")

    print()
    print("Regression instances (v18 strong → v19 empty):")
    for label in arms:
        rs = sf[label]["strong_to_empty_vs_v18"]
        print(f"  {label}: {len(rs)} — {rs}")

    print()
    print("Regression instances (v18 plausible → v19 empty):")
    for label in arms:
        rs = sf[label]["plausible_to_empty_vs_v18"]
        print(f"  {label}: {len(rs)} — {rs}")

    # Ship decision
    print()
    print("=" * 90)
    print("SHIP DECISION (HARD floors)")
    print("=" * 90)
    for label in arms:
        s = sf[label]
        floors = {
            "empty_patch ≤13": s["empty_patch"] <= 13,
            "strong ≥18": s["strong"] >= 18,
            "strong + plausible ≥35": s["strong_plus_plausible"] >= 35,
            "confidence_collapse =0": s["confidence_collapse"] == 0,
            "wrong→empty regressions =0": len(s["wrong_to_empty_vs_v17"]) == 0,
        }
        all_green = all(floors.values())
        status = "✓ SHIP" if all_green else "✗ HOLD"
        print(f"  {label}: {status}")
        for k, v in floors.items():
            print(f"    {'✓' if v else '✗'} {k}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
