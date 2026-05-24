"""Phase 1 (verify-ONLY, OFFLINE): does the champion separate its own gaps from
correct work on multi_turn?  The reflection-cycle GO/NO-GO gate.

Replays the reflect verify pass over STORED baseline transcripts (no generation
re-run) and measures, on three cohorts:
  - genuine_giveup  → expect gap=True   (DETECTION)
  - pass sample     → expect gap=False  (FALSE-GAP; the load-bearing metric)
  - alt_completion  → expect gap=False  (diagnostic: verify should recognize the
                      user already got their answer via a different route)

Pre-registered gate (locked 2026-05-24; multi_turn floor):
    proceed to Phase 2  IFF  detection ≥ 40%  AND  false_gap ≤ 20%
    fire policy from false_gap:  ≤5% always-on | 5–20% gated-only | >20% kill

The pass sample is OBJECTIVE (state-checker passes only), frozen + fixed-seed +
category-stratified (defends the false-gap threshold against sample drift). It is
printed for the ~25% human spot-check the plan requires.

Usage:
    .venv/bin/python -m scripts.measure_reflect_phase1 [--smoke N] [--pass-per-cat 30]
        [--model qwen3.6-35b-a3b-6bit] [--base-url http://127.0.0.1:8000]
Needs oMLX up (one short verify completion per problem; ~100 calls total).
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / "src"))

from luxe.agents import reflect as R          # noqa: E402
from luxe.backend import Backend              # noqa: E402

_REP = {
    "multi_turn_miss_func": "acceptance/bfcl/multi_turn_miss_func/m5_rep_1/multi_turn_miss_func",
    "multi_turn_miss_param": "acceptance/bfcl/multi_turn_miss_param/m5_rep_1/multi_turn_miss_param",
}
_SEED = 20260524  # frozen


def _record(category: str, pid: str) -> dict[str, Any]:
    return json.loads((ROOT / _REP[category] / f"{pid}.json").read_text())


def _verify_record(backend: Backend, category: str, pid: str) -> R.Verdict:
    rec = _record(category, pid)
    task, out = R.multi_turn_verify_context(rec.get("transcript", []), rec.get("decoded_turns", []))
    # max_tokens uses reflect.verify's default (generous — the champion reasons before
    # the verdict; parse_verdict extracts the LAST verdict object).
    return R.verify(backend, driver="multi_turn", task=task, output=out, num_ctx=32768)


def _frozen_pass_sample(manifest: dict[str, Any], per_cat: int) -> list[tuple[str, str]]:
    rng = random.Random(_SEED)
    out: list[tuple[str, str]] = []
    for cat, info in manifest["categories"].items():
        ids = sorted(info["pass_ids"])
        rng.shuffle(ids)
        out.extend((cat, pid) for pid in ids[:per_cat])
    return out


def _cat_of(pid: str) -> str:
    return "multi_turn_miss_func" if "miss_func" in pid else "multi_turn_miss_param"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path,
                    default=ROOT / "acceptance" / "bfcl" / "reflect_phase0" / "convertible_manifest.json")
    ap.add_argument("--labels", type=Path,
                    default=ROOT / "acceptance" / "bfcl" / "reflect_phase0" / "giveup_labels.json")
    ap.add_argument("--pass-per-cat", type=int, default=30)
    ap.add_argument("--smoke", type=int, default=None, help="Cap each cohort to N (oMLX smoke).")
    ap.add_argument("--model", default="qwen3.6-35b-a3b-6bit")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "acceptance" / "bfcl" / "reflect_phase1" / "verify_only_result.json")
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    labels = json.loads(args.labels.read_text())["labels"]
    backend = Backend(model=args.model, base_url=args.base_url)
    if not backend.health():
        print(f"ERROR: oMLX not healthy at {args.base_url}. Start it, then re-run.")
        return 2

    # Cohorts grounded in the HAND LABELS (not the unreliable structural heuristic).
    unmet = [(_cat_of(p), p) for p, v in labels.items() if v["label"] == "unmet"]
    met = [(_cat_of(p), p) for p, v in labels.items() if v["label"] == "met"]
    passes = _frozen_pass_sample(manifest, args.pass_per_cat)

    print(f"Phase 1 verify-only — model={args.model}")
    print(f"  cohorts (hand-labeled): unmet={len(unmet)} met={len(met)} pass_sample={len(passes)}")

    # Verify EVERY problem once; verdicts are saved per-pid so detection/false-gap can be
    # recomputed instantly against revised labels (a few are borderline, pending spot-check).
    all_items = {pid: cat for cat, pid in (unmet + met + passes)}
    if args.smoke:
        all_items = dict(list(all_items.items())[: args.smoke * 3])
    verdicts: dict[str, dict] = {}
    for i, (pid, cat) in enumerate(all_items.items()):
        v = _verify_record(backend, cat, pid)
        verdicts[pid] = {"gap": v.gap, "ok": v.ok, "cat": cat,
                         "specificity": [d.specificity for d in v.deficiencies]}
        if (i + 1) % 15 == 0:
            print(f"    verified {i+1}/{len(all_items)}", flush=True)

    def _rate(items: list[tuple[str, str]], axis: str | None = None) -> tuple[int, int, int]:
        sub = [(c, p) for c, p in items if p in verdicts and (axis is None or c == axis)]
        gaps = sum(1 for c, p in sub if verdicts[p]["gap"])
        errs = sum(1 for c, p in sub if not verdicts[p]["ok"])
        return gaps, len(sub), errs

    print("\n" + "=" * 66)
    metrics: dict[str, dict] = {}
    for label, items, want in [("DETECTION (unmet→gap)", unmet, "high"),
                               ("FALSE-GAP (pass→gap)", passes, "low"),
                               ("MET-ABSTAIN (met→gap)", met, "low")]:
        g, n, e = _rate(items)
        gf, nf, _ = _rate(items, "multi_turn_miss_func")
        gp, np_, _ = _rate(items, "multi_turn_miss_param")
        rate = g / n if n else 0.0
        metrics[label] = {"gap": g, "n": n, "rate": rate, "errors": e,
                          "func": [gf, nf], "param": [gp, np_]}
        print(f"{label:24s} {rate:5.1%}  ({g}/{n}, err={e})  [func {gf}/{nf} | param {gp}/{np_}]  want {want}")

    det = metrics["DETECTION (unmet→gap)"]
    fg = metrics["FALSE-GAP (pass→gap)"]
    det_func = det["func"][0] / det["func"][1] if det["func"][1] else 0.0
    false_gap = fg["rate"]
    # Pre-registered gate: per-axis detection floor (func 40%), false-gap ceiling 20%.
    # (miss_param detection is moot — only ~4 unmet — so the gate keys on miss_func.)
    gate = det_func >= 0.40 and false_gap <= 0.20
    fire = ("always-on allowed" if false_gap <= 0.05
            else "gated-only" if false_gap <= 0.20 else "KILL (false-gap > 20%)")
    print("-" * 66)
    print(f"miss_func detection={det_func:.1%} [floor 40%]  false_gap={false_gap:.1%} [ceiling 20%]")
    print(f"GATE: {'PASS → proceed to Phase 2' if gate else 'FAIL → KILL + bank negative'}   FIRE: {fire}")
    print("=" * 66)

    result = {"model": args.model, "seed": _SEED, "smoke": args.smoke,
              "metrics": metrics, "miss_func_detection": det_func,
              "false_gap": false_gap, "gate_pass": gate, "fire_policy": fire,
              "verdicts": verdicts}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(f"\nresult → {args.out}  (verdicts saved per-pid; recompute against revised labels offline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
