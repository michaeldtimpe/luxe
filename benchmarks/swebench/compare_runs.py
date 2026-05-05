"""Pre/post run comparison — delta report for SpecDD Lever 2 evaluation.

Loads two `predictions.json` files (or per-instance summaries) and
emits a per-instance delta + summary table. Designed for the
post-Lever-2 vs pre-Lever-2 SWE-bench comparison but generalises to
any pair of preds-only runs against the same instance set.

Usage:
    python -m benchmarks.swebench.compare_runs \\
        --pre acceptance/swebench/pre_specdd_v141_n75/rep_1/predictions.json \\
        --post acceptance/swebench/post_specdd_v15_n10/rep_1/predictions.json \\
        --gold-source benchmarks/swebench/subsets/raw/verified.jsonl

Output sections:
    1. Per-instance delta (tier-by-tier change)
    2. Class-level transitions (e.g. empty_patch → plausible counts)
    3. Summary deltas (mechanical PASS, strong, strong+plausible)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks.swebench.smoke_inspect import (  # noqa: E402
    compare_to_gold,
    inspect_instance,
    load_gold_patches,
)


def _load_preds(path: Path) -> dict[str, str]:
    """Return {instance_id: model_patch} from a predictions.json file."""
    rows = json.loads(path.read_text())
    out: dict[str, str] = {}
    for r in rows:
        out[r["instance_id"]] = r.get("model_patch", "")
    return out


def _tier_for(iid: str, patch: str, gold: dict[str, str] | None) -> str:
    """Return the tier label (gold-proximity if available, mechanical otherwise)."""
    base = inspect_instance(iid, patch)
    if not base.passed:
        # First reason wins for the label.
        first = base.reasons[0]
        # Reasons starting with `touches_test_paths=` map to a stable tier.
        if first.startswith("touches_test_paths="):
            return "test_path_only"
        if first == "no_substantive_change":
            return "no_substantive"
        return first  # empty_patch / new_file_in_diff / ...
    if gold and iid in gold:
        v = compare_to_gold(iid, patch, gold[iid])
        return v.tier
    return "mechanical_pass"


def _classify_all(
    preds: dict[str, str],
    gold: dict[str, str] | None,
) -> dict[str, str]:
    return {iid: _tier_for(iid, patch, gold) for iid, patch in preds.items()}


_TIER_RANK = {
    "strong": 5,
    "plausible": 4,
    "wrong_shape": 3,
    "wrong_location": 2,
    "wrong_target": 1,
    "mechanical_pass": 1,  # passed mechanical gates, no gold available
    "no_substantive": 0,
    "test_path_only": 0,
    "new_file_in_diff": 0,
    "empty_patch": 0,
}


def _delta_arrow(pre: str, post: str) -> str:
    pre_r = _TIER_RANK.get(pre, 0)
    post_r = _TIER_RANK.get(post, 0)
    if post_r > pre_r:
        return "↑"
    if post_r < pre_r:
        return "↓"
    return "·"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pre", type=Path, required=True,
                   help="Pre-treatment predictions.json")
    p.add_argument("--post", type=Path, required=True,
                   help="Post-treatment predictions.json")
    p.add_argument("--gold-source", type=Path, default=None,
                   help="Optional verified.jsonl for gold-proximity tiering. "
                        "Without this, only mechanical classification is used.")
    args = p.parse_args()

    pre_preds = _load_preds(args.pre)
    post_preds = _load_preds(args.post)

    gold = None
    if args.gold_source and args.gold_source.is_file():
        gold = load_gold_patches(args.gold_source)

    pre_tier = _classify_all(pre_preds, gold)
    post_tier = _classify_all(post_preds, gold)

    common = sorted(set(pre_preds) & set(post_preds))
    pre_only = sorted(set(pre_preds) - set(post_preds))
    post_only = sorted(set(post_preds) - set(pre_preds))

    print(f"# Pre/post comparison")
    print(f"  pre  = {args.pre}  ({len(pre_preds)} instances)")
    print(f"  post = {args.post}  ({len(post_preds)} instances)")
    print(f"  common = {len(common)}, pre_only = {len(pre_only)}, post_only = {len(post_only)}")
    print()

    print(f"# Per-instance delta")
    transitions = Counter()
    for iid in common:
        a = pre_tier[iid]
        b = post_tier[iid]
        arr = _delta_arrow(a, b)
        if a != b:
            print(f"  {arr}  {iid:50s}  {a:18s} → {b}")
        transitions[(a, b)] += 1
    print()

    print(f"# Class-level transitions ({len(common)} common instances)")
    for (a, b), n in sorted(transitions.items(), key=lambda kv: (-kv[1], kv[0])):
        flag = "  " if a == b else (" ↑" if _TIER_RANK[b] > _TIER_RANK[a] else " ↓")
        print(f"  {flag} {a:18s} → {b:18s}  {n:3d}")
    print()

    print(f"# Summary deltas (common only)")
    for label, predicate in [
        ("strong (gold-match)", lambda t: t == "strong"),
        ("strong + plausible", lambda t: t in {"strong", "plausible"}),
        ("any non-empty patch", lambda t: t not in {"empty_patch", "new_file_in_diff", "test_path_only"}),
        ("empty_patch", lambda t: t == "empty_patch"),
        ("new_file_in_diff", lambda t: t == "new_file_in_diff"),
    ]:
        pre_n = sum(1 for iid in common if predicate(pre_tier[iid]))
        post_n = sum(1 for iid in common if predicate(post_tier[iid]))
        delta = post_n - pre_n
        sign = "+" if delta >= 0 else ""
        print(f"  {label:30s}  pre={pre_n:3d}  post={post_n:3d}  delta={sign}{delta}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
