#!/usr/bin/env python3
"""scripts/variance_multi_turn.py — 3-rep variance for the multi_turn_base baseline.

Joins the per-problem PASS/FAIL across rep_1/rep_2/rep_3 under
`acceptance/bfcl/multi_turn_base/` and reports:
  * aggregate pass-rate per rep + range (is the 63% stable?),
  * per-problem consistency (3/3 or 0/3 = deterministic; 1/3 or 2/3 = a flip),
  * the flip set (the variance-class problems) + per-class flip counts.

At temp=0 the question is whether multi_turn generation is deterministic on the oMLX
substrate (like SWE-bench's strong/plausible tiers) or carries borderline noise.

    python -m scripts.variance_multi_turn [--root acceptance/bfcl/multi_turn_base]
"""

from __future__ import annotations

import argparse
import glob
import json
from collections import defaultdict
from pathlib import Path

DATA = Path.home() / ".luxe" / "bfcl-data"


def _load_rep(rep_dir: Path) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for f in glob.glob(str(rep_dir / "multi_turn_base" / "*.json")):
        d = json.loads(Path(f).read_text())
        out[d["id"]] = bool(d.get("passed"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("acceptance/bfcl/multi_turn_base"))
    args = ap.parse_args()

    reps = {}
    for r in ("rep_1", "rep_2", "rep_3"):
        d = args.root / r
        loaded = _load_rep(d)
        if loaded:
            reps[r] = loaded
    if len(reps) < 2:
        print(f"need ≥2 reps under {args.root}; found {list(reps)}")
        return 1

    classes = {json.loads(l)["id"]: json.loads(l)["involved_classes"]
               for l in open(DATA / "BFCL_v4_multi_turn_base.json")}

    print(f"=== reps present: {list(reps)} ===")
    for r, m in reps.items():
        p = sum(m.values())
        print(f"  {r}: {p}/{len(m)} = {p/len(m):.1%}")
    rates = [sum(m.values()) / len(m) for m in reps.values()]
    print(f"  pass-rate range: [{min(rates):.1%}, {max(rates):.1%}]  spread={max(rates)-min(rates):.1%}")

    # per-problem consistency over the ids present in ALL reps
    common = set.intersection(*[set(m) for m in reps.values()])
    stable_pass = stable_fail = flips = 0
    flip_ids = []
    cls_flip = defaultdict(int)
    for pid in common:
        verdicts = [reps[r][pid] for r in reps]
        if all(verdicts):
            stable_pass += 1
        elif not any(verdicts):
            stable_fail += 1
        else:
            flips += 1
            flip_ids.append((pid, [int(v) for v in verdicts]))
            for c in classes.get(pid, []):
                cls_flip[c] += 1

    nrep = len(reps)
    print(f"\n=== per-problem consistency over {len(common)} shared problems ({nrep} reps) ===")
    print(f"  stable PASS (all reps): {stable_pass}")
    print(f"  stable FAIL (all reps): {stable_fail}")
    print(f"  FLIPS (variance):       {flips}  ({flips/len(common):.1%})")
    if flips == 0:
        print("  → multi_turn generation is DETERMINISTIC at temp=0 on this substrate.")
    else:
        print("\n  flip set (id: per-rep pass):")
        for pid, v in sorted(flip_ids):
            print(f"    {pid:24} {v}  classes={classes.get(pid, [])}")
        print("\n  flips by involved class:")
        for c, n in sorted(cls_flip.items(), key=lambda kv: -kv[1]):
            print(f"    {c:20} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
