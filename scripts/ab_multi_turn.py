#!/usr/bin/env python3
"""scripts/ab_multi_turn.py — exact A/B for the multi_turn enhanced (scoped GFS guidance)
vs clean baseline. Leverages 0-variance determinism: every difference is causal.

    python -m scripts.ab_multi_turn \
        --clean acceptance/bfcl/multi_turn_base/rep_1 \
        --enhanced acceptance/bfcl/multi_turn_base/enhanced_rep_1

Reports: overall + GorillaFileSystem (GFS) pass-rate deltas; the flip-set IDENTITIES
(fail→pass vs pass→fail) with reason migration; action-entropy (calls/problem) deltas;
and a payload-level byte-identity check on NON-GFS problems (the scoped lever must leave
them untouched — any difference is a scoping/state-bleed bug).
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from statistics import mean, median

DATA = Path.home() / ".luxe" / "bfcl-data"


def _load(rep: Path) -> dict[str, dict]:
    return {json.loads(Path(f).read_text())["id"]: json.loads(Path(f).read_text())
            for f in glob.glob(str(rep / "multi_turn_base" / "*.json"))}


def _ncalls(d: dict) -> int:
    return sum(len(s) for t in d.get("decoded_turns", []) for s in t)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", type=Path, required=True)
    ap.add_argument("--enhanced", type=Path, required=True)
    args = ap.parse_args()

    involved = {json.loads(l)["id"]: json.loads(l)["involved_classes"]
                for l in open(DATA / "BFCL_v4_multi_turn_base.json")}
    clean, enh = _load(args.clean), _load(args.enhanced)
    ids = sorted(set(clean) & set(enh))
    gfs = [i for i in ids if "GorillaFileSystem" in involved.get(i, [])]
    nongfs = [i for i in ids if "GorillaFileSystem" not in involved.get(i, [])]

    cp = sum(clean[i]["passed"] for i in ids); ep = sum(enh[i]["passed"] for i in ids)
    print(f"=== OVERALL: clean {cp}/{len(ids)}={cp/len(ids):.1%} → enhanced {ep}/{len(ids)}={ep/len(ids):.1%}  (Δ{ep-cp:+d}) ===")
    cg = sum(clean[i]["passed"] for i in gfs); eg = sum(enh[i]["passed"] for i in gfs)
    print(f"=== GorillaFileSystem ({len(gfs)}): clean {cg}/{len(gfs)}={cg/len(gfs):.0%} → enhanced {eg}/{len(gfs)}={eg/len(gfs):.0%}  (Δ{eg-cg:+d}) ===")

    # NON-GFS payload byte-identity (scoped lever must not touch them)
    diff_nongfs = [i for i in nongfs if clean[i].get("decoded_turns") != enh[i].get("decoded_turns")]
    print(f"\n=== non-GFS payload identity: {len(nongfs)-len(diff_nongfs)}/{len(nongfs)} byte-identical ===")
    if diff_nongfs:
        print(f"  !!! {len(diff_nongfs)} non-GFS problems DIFFER (scoping bug): {diff_nongfs[:10]}")
    else:
        print("  ✓ all non-GFS problems byte-identical (scoping correct by construction)")

    # flip-set on GFS
    fail2pass = [i for i in gfs if not clean[i]["passed"] and enh[i]["passed"]]
    pass2fail = [i for i in gfs if clean[i]["passed"] and not enh[i]["passed"]]
    print(f"\n=== GFS flip-set: {len(fail2pass)} fail→pass | {len(pass2fail)} pass→fail | net {len(fail2pass)-len(pass2fail):+d} ===")
    for i in fail2pass:
        print(f"  fail→PASS {i}  (was: {clean[i]['reason']})")
    for i in pass2fail:
        print(f"  PASS→fail {i}  (now: {enh[i]['reason']})  <-- regression, inspect")

    # action entropy
    for label, group in (("all", ids), ("GFS", gfs)):
        cc = [_ncalls(clean[i]) for i in group]; ec = [_ncalls(enh[i]) for i in group]
        print(f"\n=== calls/problem [{label}]: clean mean {mean(cc):.1f} med {median(cc):.0f} "
              f"→ enhanced mean {mean(ec):.1f} med {median(ec):.0f} ===")

    net = len(fail2pass) - len(pass2fail)
    print(f"\nNET GorillaFileSystem flips: {net:+d}  ({len(fail2pass)} fixed, {len(pass2fail)} broke)")
    print("NOTE: the ship decision is a judgment call, not mechanical. A small net WITH deterministic")
    print("regressions is a non-Pareto wash (over-action↔under-action trade) — keep clean default unless")
    print("the net is clearly material AND the regressions are diagnosably fixable. Non-GFS must be byte-identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
