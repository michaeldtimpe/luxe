#!/usr/bin/env python3
"""scripts/parity_multi_turn.py — multi_turn BFCL Phase 3 parity gate.

Confirms luxe's VENDORED multi_turn grader (run in MyEnv during generation) agrees
with the OFFICIAL bfcl_eval scorer on the SAME decoded predictions. Because the
vendored checker is a verbatim copy, a per-problem disagreement points at either
(a) environment drift — the involved classes behaving differently under MyEnv's deps
vs the stale .venv's (e.g. mpmath/numpy/Decimal), or (b) a vendoring/wiring bug. A
*shared* failure on both is generation signal (the model just got it wrong), not a bug.

RUN WITH THE STALE .venv PYTHON (it has bfcl_eval; MyEnv must NOT):
    .venv/bin/python -m scripts.parity_multi_turn --pred acceptance/bfcl/mt_parity/rep_1/

Reads each per-problem prediction JSON (carrying `decoded_turns` + luxe's vendored
`passed` verdict), re-grades `decoded_turns` with the official checker, and compares
per-problem PASS/FAIL. Exit 0 iff every comparison matches.
"""

from __future__ import annotations

import argparse
import glob
import itertools
import json
from pathlib import Path

# OFFICIAL scorer — only importable in the stale .venv (has bfcl_eval).
from bfcl_eval.eval_checker.multi_turn_eval.multi_turn_checker import (
    multi_turn_checker as official_checker,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True, type=Path,
                    help="Prediction dir (…/rep_1/), expects multi_turn_base/<id>.json.")
    ap.add_argument("--data", type=Path, default=Path.home() / ".luxe" / "bfcl-data")
    args = ap.parse_args()

    probs = {json.loads(l)["id"]: json.loads(l)
             for l in open(args.data / "BFCL_v4_multi_turn_base.json")}
    gts = {json.loads(l)["id"]: json.loads(l)["ground_truth"]
           for l in open(args.data / "possible_answer" / "BFCL_v4_multi_turn_base.json")}

    seq = itertools.count()  # unique model_name per call (globals() instance isolation)
    n = match = mismatch = 0
    rows = []
    for f in sorted(glob.glob(str(args.pred / "multi_turn_base" / "*.json"))):
        d = json.loads(Path(f).read_text())
        pid = d.get("id")
        decoded = d.get("decoded_turns")
        if pid not in probs or decoded is None:
            continue
        n += 1
        luxe_pass = bool(d.get("passed"))
        luxe_reason = d.get("reason", "")
        try:
            r = official_checker(decoded, gts[pid], probs[pid],
                                 "multi_turn_base", f"parity_{next(seq)}")
            off_pass = r.get("valid") is True
            off_reason = "all_turns_matched" if off_pass else r.get("error_type", "unknown")
        except Exception as e:  # noqa: BLE001
            off_pass, off_reason = None, f"checker_error: {type(e).__name__}: {e}"
        if off_pass == luxe_pass:
            match += 1
        else:
            mismatch += 1
            rows.append((pid, luxe_pass, luxe_reason, off_pass, off_reason))

    print(f"\n=== multi_turn parity: {match}/{n} match | {mismatch} mismatch ===")
    # informational: how many the model actually passed (generation signal)
    passed = sum(1 for f in glob.glob(str(args.pred / "multi_turn_base" / "*.json"))
                 if json.loads(Path(f).read_text()).get("passed"))
    print(f"(generation signal: luxe vendored grader passed {passed}/{n})")
    for pid, lp, lr, op, orr in rows:
        print(f"  MISMATCH {pid}: luxe={lp}({lr}) official={op}({orr})")
    if mismatch == 0:
        print("\nPARITY CLEAN — vendored grader (MyEnv) == official scorer (stale .venv) "
              "on all real predictions. Grader faithful + environment-equivalent.")
        return 0
    print("\nTRIAGE: a luxe!=official disagreement is a bug (env drift in an involved "
          "class, or vendoring/wiring) — inspect the mismatched ids' decoded_turns + "
          "involved_classes. (Shared failures are generation signal, not mismatches.)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
