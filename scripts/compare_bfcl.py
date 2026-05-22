#!/usr/bin/env python3
"""scripts/compare_bfcl.py — per-category pass-rate delta between two BFCL runs.

Usage:
    python -m scripts.compare_bfcl \
        --baseline acceptance/bfcl/post_specdd_v18_lever1/rep_1/summary.json \
        --candidate acceptance/bfcl/post_v1105_full_n1235_agent/rep_1/summary.json

Compares on pass_RATE, not raw counts: vendored BFCL v4 category sizes can
differ slightly from an older run (e.g. simple_python 400 → 399), so raw
passed-counts are not directly comparable. Prints per-category baseline rate,
candidate rate, and delta (pp), plus the weighted total. Read-only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

_CATS = ("simple_python", "multiple", "parallel", "parallel_multiple", "irrelevance")


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


def _rate(cat_block: dict) -> tuple[float, int, int]:
    n = int(cat_block.get("n", 0))
    passed = int(cat_block.get("passed", 0))
    rate = cat_block.get("pass_rate")
    rate = float(rate) if rate is not None else (passed / n if n else 0.0)
    return rate, passed, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, type=Path)
    ap.add_argument("--candidate", required=True, type=Path)
    args = ap.parse_args()

    base = _load(args.baseline)
    cand = _load(args.candidate)
    b_cats = base.get("categories", {})
    c_cats = cand.get("categories", {})

    print(f"baseline : {args.baseline}  (mode={base.get('mode','?')})")
    print(f"candidate: {args.candidate}  (mode={cand.get('mode','?')})")
    print()
    print(f"{'category':20} {'baseline':>16} {'candidate':>16} {'Δpp':>8}")
    cats = [c for c in _CATS if c in b_cats or c in c_cats]
    for c in cats:
        if c not in b_cats or c not in c_cats:
            present = "baseline" if c in b_cats else "candidate"
            print(f"{c:20} {'(only in '+present+')':>42}")
            continue
        br, bp, bn = _rate(b_cats[c])
        cr, cp, cn = _rate(c_cats[c])
        d = (cr - br) * 100.0
        flag = "  <-- regression" if d <= -2.0 else ("  <-- gain" if d >= 2.0 else "")
        print(f"{c:20} {bp:>4}/{bn:<4} {br:>6.2%} {cp:>4}/{cn:<4} {cr:>6.2%} {d:>+7.2f}{flag}")

    bt, ct = base.get("totals", {}), cand.get("totals", {})
    btr = float(bt.get("pass_rate", 0.0)); ctr = float(ct.get("pass_rate", 0.0))
    print()
    print(f"{'TOTAL':20} {bt.get('passed','?')}/{bt.get('n','?')} {btr:>6.2%}   "
          f"{ct.get('passed','?')}/{ct.get('n','?')} {ctr:>6.2%}   Δ={(ctr-btr)*100:+.2f}pp")
    print("\n(Δpp on pass-rate; category n may differ between runs — rate is the comparable axis.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
