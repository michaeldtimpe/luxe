"""Analyze the v1.10.1 n=14 smoke against the v1.10 n=75 baseline subset.

For each of the 14 v19_smoke instances, this script:
1. Walks events.jsonl to detect habituation_exit / early_bail_exploratory
   fires.
2. Compares the v1.10.1 outcome to v1.10's full-stack outcome.
3. Surfaces:
   - habituation_exit fires (W2 cohort)
   - exploratory variant fires (W3 cohort)
   - new regressions vs v1.10 (v1.10 non-empty → v1.10.1 empty)
   - new recoveries vs v1.10 (v1.10 empty → v1.10.1 non-empty)
4. Verdict line: PASS if (a) zero new regressions, (b) at least one
   habituation_exit fired, (c) at least one exploratory variant fired.

Usage:
    python scripts/analyze_v1101_smoke.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SMOKE_PREDS = REPO / "acceptance/swebench/v1101_smoke_n14/rep_1/predictions.json"
V110_TAX = REPO / "acceptance/v110_taxonomy/v110_n75_full_stack_swebench.json"
WORKSPACE = Path.home() / ".luxe" / "swebench-workspace"
RUNS = Path.home() / ".luxe" / "runs"


def run_id_for(instance_id: str) -> str | None:
    log = WORKSPACE / instance_id / "log" / "stdout.log"
    if not log.is_file():
        return None
    for line in log.read_text().splitlines():
        if line.startswith("luxe maintain  run_id="):
            return line.split("=", 1)[1].strip()
    return None


def load_events(run_id: str) -> list[dict]:
    p = RUNS / run_id / "events.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main() -> int:
    if not SMOKE_PREDS.is_file():
        print(f"  ! smoke predictions missing: {SMOKE_PREDS}", file=sys.stderr)
        return 1
    if not V110_TAX.is_file():
        print(f"  ! v1.10 baseline taxonomy missing: {V110_TAX}", file=sys.stderr)
        return 1

    smoke_preds = json.loads(SMOKE_PREDS.read_text())
    v110_rows = {r["instance_id"]: r for r in json.loads(V110_TAX.read_text())["rows"]}

    print(f"=== v1.10.1 n=14 smoke analysis ===\n")

    # Per-instance walk
    habituation_fires: list[str] = []
    exploratory_fires: list[dict] = []
    soft_anchor_fires: list[dict] = []
    commit_imperative_fires: list[dict] = []
    new_regressions: list[dict] = []
    new_recoveries: list[dict] = []
    no_change: list[str] = []

    print(f"{'instance':<48} {'v110_tier':<15} {'v1101_has_patch':>16}  events")
    for p in smoke_preds:
        iid = p["instance_id"]
        v1101_has = bool((p.get("model_patch") or "").strip())
        v110_row = v110_rows.get(iid)
        v110_has = v110_row.get("has_patch") if v110_row else None
        v110_tier = v110_row.get("tier") if v110_row else "?"

        run_id = run_id_for(iid)
        events = load_events(run_id) if run_id else []

        # Detect intervention msg variants + habituation
        habituated = False
        msg_variants: list[str] = []
        for evt in events:
            kind = evt.get("kind")
            if kind == "habituation_exit":
                habituated = True
                habituation_fires.append(iid)
            elif kind == "early_bail_fired":
                v = evt.get("msg_variant")
                msg_variants.append(v)
                if v == "exploratory":
                    exploratory_fires.append({"iid": iid, "score": evt.get("convergence_score")})
                elif v == "soft_anchor":
                    soft_anchor_fires.append({"iid": iid, "score": evt.get("convergence_score")})
                elif v == "commit_imperative":
                    commit_imperative_fires.append({"iid": iid, "score": evt.get("convergence_score")})

        # Classify cross-cycle change
        if v110_has and not v1101_has:
            new_regressions.append({"iid": iid, "v110_tier": v110_tier})
        elif not v110_has and v1101_has:
            new_recoveries.append({"iid": iid, "v110_tier": v110_tier})
        else:
            no_change.append(iid)

        marker = "✓" if v1101_has else "✗"
        print(f"  {iid:<48} {v110_tier:<15} {str(v1101_has):>16}  {len(events):>4}"
              + (f"  [habit]" if habituated else "")
              + (f"  [var={','.join(msg_variants)}]" if msg_variants else ""))

    n = len(smoke_preds)
    n_with_patch = sum(1 for p in smoke_preds if (p.get("model_patch") or "").strip())
    print(f"\n  Total: {n_with_patch}/{n} with non-empty patch")

    print(f"\n--- Intervention event summary ---")
    print(f"  habituation_exit fires:       {len(habituation_fires)} {habituation_fires}")
    print(f"  exploratory variant fires:    {len(exploratory_fires)}")
    for f in exploratory_fires:
        print(f"    - {f['iid']}  score={f['score']}")
    print(f"  soft_anchor variant fires:    {len(soft_anchor_fires)}")
    print(f"  commit_imperative variant:    {len(commit_imperative_fires)}")

    print(f"\n--- Cross-cycle comparison (v1.10.1 vs v1.10 baseline) ---")
    print(f"  no change:        {len(no_change)} (both produced/didn't produce a patch)")
    print(f"  recovered:        {len(new_recoveries)}")
    for r in new_recoveries:
        print(f"    + {r['iid']}  (v110 tier={r['v110_tier']})")
    print(f"  regressed:        {len(new_regressions)}")
    for r in new_regressions:
        print(f"    - {r['iid']}  (v110 tier={r['v110_tier']})")

    # Verdict
    print(f"\n=== verdict ===")
    no_regressions = len(new_regressions) == 0
    has_habituation = len(habituation_fires) >= 1
    has_exploratory = len(exploratory_fires) >= 1
    all_pass = no_regressions and has_habituation and has_exploratory
    print(f"  {'✓' if no_regressions else '✗'} zero new regressions vs v1.10")
    print(f"  {'✓' if has_habituation else '✗'} habituation_exit fires ≥ 1")
    print(f"  {'✓' if has_exploratory else '✗'} exploratory variant fires ≥ 1")
    print(f"\n  smoke ship-gate: {'✓ PASS - proceed to n=75' if all_pass else '✗ HOLD - investigate before n=75'}")
    return 0 if all_pass else 2


if __name__ == "__main__":
    sys.exit(main())
