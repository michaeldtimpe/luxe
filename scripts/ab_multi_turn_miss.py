#!/usr/bin/env python3
"""scripts/ab_multi_turn_miss.py — exact A/B for the Phase 2 reflect→repair stage
(LUXE_REFLECT) vs the clean baseline, on multi_turn_miss_func.

    python -m scripts.ab_multi_turn_miss \
        --clean   acceptance/bfcl/multi_turn_miss_func/clean_arm \
        --reflect acceptance/bfcl/multi_turn_miss_func/reflect_arm

Both dirs are bfcl run outputs (each holds a `multi_turn_<category>/` subdir of
per-problem JSONs). multi_turn at temp=0 is fully deterministic on this substrate, so
every difference between the arms is CAUSAL — attributable to the repair stage.

The ship gate (pre-registered, RESUME / plan Phase 3):
  - net fail→pass > 0  (the stage actually converts give-ups)
  - ZERO pass→fail regression
  - the repair path is a NO-OP where it does not fire:  every problem with
    `repair_turns == []` must be byte-identical to the clean arm. A violation is a
    state-bleed bug, not a result.
  - HARD KILL-WARNING: empty_turn → state/response mismatch reason migration. A
    give-up (empty_turn) that becomes an *instance_state_mismatch* /
    *execution_response_mismatch* under repair means the model acted WITHOUT grounding
    (benchmark-gaming the empty-turn signal), not that it correctly recovered. This is
    watched across ALL problems, not just flips (a fail→fail migration still signals
    the repair is producing wrong actions).
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from statistics import mean, median

_EMPTY = "multi_turn:empty_turn_model_response"
_MISMATCH = ("multi_turn:instance_state_mismatch", "multi_turn:execution_response_mismatch")


def _load(rep: Path, category: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in glob.glob(str(rep / category / "*.json")):
        d = json.loads(Path(f).read_text())
        out[d["id"]] = d
    if not out:
        raise SystemExit(f"no per-problem JSONs under {rep / category}/ — check --{'clean/--reflect'} paths")
    return out


def _ncalls(d: dict) -> int:
    return sum(len(s) for t in d.get("decoded_turns", []) for s in t)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", type=Path, required=True, help="clean arm (LUXE_REFLECT off)")
    ap.add_argument("--reflect", type=Path, required=True, help="reflect arm (LUXE_REFLECT=1)")
    ap.add_argument("--category", default="multi_turn_miss_func")
    args = ap.parse_args()

    clean = _load(args.clean, args.category)
    refl = _load(args.reflect, args.category)
    ids = sorted(set(clean) & set(refl))
    if len(ids) != len(clean) or len(ids) != len(refl):
        print(f"!! arm id-sets differ: clean={len(clean)} reflect={len(refl)} shared={len(ids)}")

    cp = sum(clean[i]["passed"] for i in ids)
    rp = sum(refl[i]["passed"] for i in ids)
    print(f"=== OVERALL [{args.category}]: clean {cp}/{len(ids)}={cp/len(ids):.1%} "
          f"→ reflect {rp}/{len(ids)}={rp/len(ids):.1%}  (Δ{rp-cp:+d}) ===")

    fired = [i for i in ids if refl[i].get("repair_turns")]
    print(f"\n=== repair fired on {len(fired)}/{len(ids)} problems ===")

    # The reflect path must be a pure no-op where it didn't fire.
    leaked = [i for i in ids if not refl[i].get("repair_turns")
              and refl[i].get("decoded_turns") != clean[i].get("decoded_turns")]
    if leaked:
        print(f"  !!! {len(leaked)} non-fired problems DIFFER from clean (state-bleed BUG): {leaked[:10]}")
    else:
        print("  ✓ all non-fired problems byte-identical to clean (repair is a clean no-op when off-target)")

    # Flip set.
    f2p = [i for i in ids if not clean[i]["passed"] and refl[i]["passed"]]
    p2f = [i for i in ids if clean[i]["passed"] and not refl[i]["passed"]]
    print(f"\n=== flip-set: {len(f2p)} fail→pass | {len(p2f)} pass→fail | net {len(f2p)-len(p2f):+d} ===")
    for i in f2p:
        attr = "repair" if refl[i].get("repair_turns") else "NOT-repair(!)"
        print(f"  fail→PASS {i}  (was: {clean[i]['reason']})  [{attr} turns={refl[i].get('repair_turns')}]")
    for i in p2f:
        print(f"  PASS→fail {i}  (now: {refl[i]['reason']})  [turns={refl[i].get('repair_turns')}]  <-- REGRESSION")

    # HARD kill-warning: empty_turn give-up → wrong-state mismatch (acting w/o grounding).
    migrated = [i for i in ids if clean[i]["reason"] == _EMPTY and refl[i]["reason"] in _MISMATCH]
    print(f"\n=== empty_turn → state/response-mismatch migration: {len(migrated)} "
          f"(HARD kill-warning if > 0) ===")
    for i in migrated:
        flip = "fail→fail" if not refl[i]["passed"] else "→pass"
        print(f"  {i}: {clean[i]['reason']} → {refl[i]['reason']}  ({flip})  [turns={refl[i].get('repair_turns')}]")

    # Action entropy (repair adds calls; watch for over-action).
    cc = [_ncalls(clean[i]) for i in ids]
    rc = [_ncalls(refl[i]) for i in ids]
    print(f"\n=== calls/problem: clean mean {mean(cc):.1f} med {median(cc):.0f} "
          f"→ reflect mean {mean(rc):.1f} med {median(rc):.0f} ===")
    if fired:
        ccf = [_ncalls(clean[i]) for i in fired]
        rcf = [_ncalls(refl[i]) for i in fired]
        print(f"    (fired subset only: clean mean {mean(ccf):.1f} → reflect mean {mean(rcf):.1f})")

    # Verdict.
    net = len(f2p) - len(p2f)
    ship = net > 0 and not p2f and not migrated and not leaked
    print("\n" + "=" * 70)
    print(f"NET fail→pass: {net:+d}  |  pass→fail regressions: {len(p2f)}  |  "
          f"empty→mismatch migrations: {len(migrated)}  |  no-op leaks: {len(leaked)}")
    print(f"SHIP GATE: {'PASS → recommend default-on' if ship else 'FAIL/HOLD → keep opt-in'}")
    if migrated:
        print("  ⚠ HARD KILL-WARNING tripped: repair is producing ungrounded actions (empty→mismatch).")
    print("NOTE: ship is a judgment call. A clean net>0 with 0 regressions/migrations is the only")
    print("path to default-on; anything else keeps the stage opt-in + documented (record the datapoint).")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
