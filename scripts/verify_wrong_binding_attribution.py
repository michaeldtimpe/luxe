"""WS2 deep-dive (READ-ONLY): counterfactual attribution of the gt_value_mismatch bucket.

`analyze_acted_but_wrong.py` flags `gt_value_mismatch` by diffing call-strings — a HEURISTIC
that over-counts (BFCL grades final STATE, not exact calls). This script replaces that
eyeball with a bench-as-truth measurement: for each gt_value_mismatch failure, substitute the
GT value(s) back into the model's calls and RE-RUN the vendored state checker. If fixing the
binding alone flips fail→PASS, the wrong-binding was DECISIVE (genuine + actionable); if it
still fails, the binding was not the cause (benign / symptom of an omission the state-checker
ignores). This is the real size of the actionable wrong-binding axis.

Sanity gate: re-grading the unmodified `decoded_turns` must reproduce the stored verdict
(the grader is deterministic) — else the counterfactual can't be trusted.

Read-only. Needs the sizing manifest (run `analyze_acted_but_wrong` first) + the gitignored
m5 rep dirs + the BFCL data dir. Usage:
    .venv/bin/python -m scripts.verify_wrong_binding_attribution [--list]
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.bfcl.adapter import load_ground_truth, load_problems  # noqa: E402
from benchmarks.bfcl.grade import grade_multi_turn  # noqa: E402
from benchmarks.bfcl.multi_turn.executor import to_call_string  # noqa: E402

from scripts.analyze_acted_but_wrong import parse_call  # noqa: E402

_REPS = {
    "multi_turn_miss_func": "acceptance/bfcl/multi_turn_miss_func/m5_rep_1/multi_turn_miss_func",
    "multi_turn_miss_param": "acceptance/bfcl/multi_turn_miss_param/m5_rep_1/multi_turn_miss_param",
}
_MANIFEST = ROOT / "acceptance" / "bfcl" / "wrong_binding" / "sizing_manifest.json"


def substitute_gt_values(decoded_turns: list, mismatches: list[dict]) -> list:
    """Return a copy of `decoded_turns` with each flagged mismatch's arg set to its GT value.

    A mismatch is `{fn, param, model, gt, kind?}`. For `kind='omitted_arg'` the GT arg is
    ADDED where missing; otherwise the param is overwritten on calls to `fn` whose current
    value equals the recorded model value (string-insensitive). Only the flagged params
    change — a minimal, targeted edit so a re-grade flip is attributable to the binding.
    """
    dt = copy.deepcopy(decoded_turns)
    for turn in dt:
        for step in turn:
            for i, cs in enumerate(step):
                parsed = parse_call(cs) if isinstance(cs, str) else None
                if not parsed:
                    continue
                name, args = parsed
                changed = False
                for mm in mismatches:
                    if mm["fn"] != name:
                        continue
                    param, mval, gval = mm["param"], mm.get("model"), mm["gt"]
                    if mm.get("kind") == "omitted_arg":
                        if param not in args:
                            args[param] = gval
                            changed = True
                    elif param in args and (args[param] == mval
                                            or str(args[param]).strip() == str(mval).strip()):
                        args[param] = gval
                        changed = True
                if changed:
                    step[i] = to_call_string(name, args)
    return dt


def main() -> int:
    ap = argparse.ArgumentParser(description="Counterfactual attribution of gt_value_mismatch (read-only).")
    ap.add_argument("--manifest", type=Path, default=_MANIFEST)
    ap.add_argument("--list", action="store_true", help="Print each decisive case + its mismatches.")
    args = ap.parse_args()
    if not args.manifest.is_file():
        print(f"ERROR: manifest not found: {args.manifest} (run analyze_acted_but_wrong first)")
        return 2
    man = json.loads(args.manifest.read_text())

    total_gvm = decisive = pure = pure_decisive = repro_ok = repro_n = 0
    by_sub: dict[str, list[int]] = {}  # subtype → [#failures_with_it, #decisive_with_it]
    decisive_rows: list[tuple[str, str, list[dict]]] = []

    for cat, info in man.get("categories", {}).items():
        rep = ROOT / _REPS[cat]
        if not rep.is_dir():
            print(f"  (rep dir not found: {rep})")
            continue
        probs = {p["id"]: p for p in load_problems(cat)}
        gtm = load_ground_truth(cat)
        for pid, d in info["detail"].items():
            if d["bucket"] != "gt_value_mismatch":
                continue
            rec = json.loads((rep / f"{pid}.json").read_text())
            # sanity: unmodified re-grade reproduces the stored fail
            repro_n += 1
            base = grade_multi_turn(rec["decoded_turns"], gtm[pid], probs[pid])
            repro_ok += int(base.passed == bool(rec.get("passed")))
            total_gvm += 1
            is_pure = not d["omissions"] and not d["extras"]
            pure += int(is_pure)
            subs = sorted({m["subtype"] for m in d["mismatches"]})
            for s in subs:
                by_sub.setdefault(s, [0, 0])[0] += 1
            r = grade_multi_turn(substitute_gt_values(rec["decoded_turns"], d["mismatches"]),
                                 gtm[pid], probs[pid])
            if r.passed:
                decisive += 1
                pure_decisive += int(is_pure)
                for s in subs:
                    by_sub[s][1] += 1
                decisive_rows.append((pid, ",".join(subs), d["mismatches"]))

    a = sum(v["n_acted_but_wrong"] for v in man.get("categories", {}).values())
    print(f"sanity: unmodified re-grade reproduces stored verdict for {repro_ok}/{repro_n} gt_value_mismatch pids")
    print(f"\nA (acted-but-wrong) = {a}")
    print(f"gt_value_mismatch (heuristic flag) = {total_gvm}  ({total_gvm/a:.1%} of A)")
    print(f"DECISIVE wrong-binding (GT-value substitution flips fail→PASS) = {decisive}  ({decisive/a:.1%} of A)")
    print(f"  of which PURE (binding was the ONLY call-diff) = {pure_decisive}   [pure total = {pure}]")
    print("\n  by subtype  [#flagged_failures, #decisive_failures]:")
    for s, v in sorted(by_sub.items(), key=lambda kv: -kv[1][1]):
        print(f"    {s:14s} {v}")
    print("\nPre-registered gate: ESCALATE needs DECISIVE >=30 cases AND >=20% of A AND a dominant "
          f"separable addressable cluster. Here {decisive} (<30) / {decisive/a:.1%} (<20%) → BANK.")

    if args.list:
        print("\nDecisive cases:")
        for pid, subs, mms in sorted(decisive_rows, key=lambda r: (r[0].split('_miss_')[1])):
            print(f"  {pid} [{subs}]")
            for m in mms:
                mo, gv = repr(m["model"]), repr(m["gt"])
                print(f"    {m['fn']}.{m['param']} [{m['subtype']}]: model={mo[:50]} gt={gv[:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
