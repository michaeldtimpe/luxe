"""3x3 cohort-shift analyzer — cycle-A 3 reps × cycle-B 3 reps.

The deciding ship gate for v1.10.5 (first cycle to clear all 6 archetypes
simultaneously). Extracted from ad-hoc `python -c` blocks in
`scripts/post_v110*_n75_pipeline.sh` into a stable CLI.

Per-instance verdicts:
  byte_identical       — tiers_a == tiers_b across all 3 reps.
  deterministic_gain   — all 3 reps of A at one tier, all 3 reps of B
                         at a strictly-better tier.
  deterministic_loss   — same as above, reversed. The hard gate.
  modal_gain           — median tier improved (B's median better than A's),
                         not strictly deterministic.
  modal_loss           — median tier regressed.
  noise                — variance in both cycles, medians comparable.

Codifies the v1.10.5b lesson (predicate features verified at the actual
event-emission point, not from hand-computed audit data) by emitting per-
instance feature snapshots: tiers, sets, ranks, verdict — the inputs to
the classification verdict. Future audits read from the snapshot, not
from human re-derivation.

Exit codes:
  0 — clean cohort shift (no deterministic losses)
  1 — at least one deterministic loss (the v1.10.5 ship-gate failure mode)
  2 — missing inputs / parse error

Usage:
  python -m scripts.cohort_shift_3x3 \\
      --cycle-a v1.10.4 acceptance/v1104_taxonomy/v1104_n75_full_stack_swebench.json \\
                         acceptance/v1104_taxonomy/v1104_n75_rep_2_full_stack_swebench.json \\
                         acceptance/v1104_taxonomy/v1104_n75_rep_3_full_stack_swebench.json \\
      --cycle-b v1.10.5 acceptance/v1105_taxonomy/v1105_n75_full_stack_swebench.json \\
                         acceptance/v1105_taxonomy/v1105_n75_rep_2_full_stack_swebench.json \\
                         acceptance/v1105_taxonomy/v1105_n75_rep_3_full_stack_swebench.json \\
      --snapshot-out acceptance/cohort_shift/v1104_vs_v1105.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Tier ranking, low = better. Anything not in this map gets rank 99
# (treated as worse than empty_patch) so unexpected tiers don't silently
# masquerade as gains.
TIER_RANK: dict[str, int] = {
    "strong": 1,
    "plausible": 2,
    "wrong_location": 3,
    "wrong_target": 4,
    "new_file_in_diff": 5,
    "empty_patch": 6,
}


def tier_rank(t: str) -> int:
    return TIER_RANK.get(t, 99)


def median_rank(tiers: tuple[str, ...]) -> float:
    ranks = sorted(tier_rank(t) for t in tiers)
    n = len(ranks)
    if n == 0:
        return 99.0
    if n % 2 == 1:
        return float(ranks[n // 2])
    return (ranks[n // 2 - 1] + ranks[n // 2]) / 2.0


def classify_instance(
    tiers_a: tuple[str, ...],
    tiers_b: tuple[str, ...],
) -> dict:
    """Per-instance cohort-shift verdict.

    Pure function — no I/O, no env. Tested directly.
    """
    set_a = frozenset(tiers_a)
    set_b = frozenset(tiers_b)
    a_uniform = len(set_a) == 1
    b_uniform = len(set_b) == 1
    med_a = median_rank(tiers_a)
    med_b = median_rank(tiers_b)
    rank_delta = med_b - med_a  # negative = improvement (B median better)

    if tiers_a == tiers_b:
        verdict = "byte_identical"
    elif a_uniform and b_uniform:
        a_only = next(iter(set_a))
        b_only = next(iter(set_b))
        if tier_rank(b_only) < tier_rank(a_only):
            verdict = "deterministic_gain"
        elif tier_rank(b_only) > tier_rank(a_only):
            verdict = "deterministic_loss"
        else:
            # Same uniform tier but element-order differs — treat as byte_identical-ish.
            verdict = "byte_identical"
    elif med_b < med_a:
        verdict = "modal_gain"
    elif med_b > med_a:
        verdict = "modal_loss"
    else:
        verdict = "noise"

    return {
        "tiers_a": list(tiers_a),
        "tiers_b": list(tiers_b),
        "set_a": sorted(set_a),
        "set_b": sorted(set_b),
        "a_uniform_tier": next(iter(set_a)) if a_uniform else None,
        "b_uniform_tier": next(iter(set_b)) if b_uniform else None,
        "median_rank_a": med_a,
        "median_rank_b": med_b,
        "rank_delta": rank_delta,
        "verdict": verdict,
    }


def load_taxonomy(path: Path) -> dict[str, str]:
    """{instance_id: tier} from a taxonomy JSON."""
    data = json.loads(path.read_text())
    return {r["instance_id"]: r["tier"] for r in data["rows"]}


def build_instance_tiers(rep_taxonomies: list[dict[str, str]]) -> dict[str, tuple[str, ...]]:
    """{instance_id: (tier_rep1, tier_rep2, tier_rep3)} from N rep taxonomies."""
    instance_ids = sorted(set().union(*[set(t.keys()) for t in rep_taxonomies]))
    out: dict[str, tuple[str, ...]] = {}
    for iid in instance_ids:
        out[iid] = tuple(t.get(iid, "MISSING") for t in rep_taxonomies)
    return out


def cohort_shift(
    cycle_a: dict[str, tuple[str, ...]],
    cycle_b: dict[str, tuple[str, ...]],
) -> dict[str, dict]:
    """Per-instance cohort-shift verdicts across two cycles' 3-rep arrays."""
    ids = sorted(set(cycle_a) | set(cycle_b))
    return {
        iid: classify_instance(
            cycle_a.get(iid, tuple()),
            cycle_b.get(iid, tuple()),
        )
        for iid in ids
    }


def render(verdicts: dict[str, dict], label_a: str, label_b: str) -> str:
    out: list[str] = []
    counter: Counter[str] = Counter(v["verdict"] for v in verdicts.values())
    out.append("=" * 72)
    out.append(f"3x3 cohort shift: {label_a} → {label_b}")
    out.append("=" * 72)
    out.append("")
    out.append(f"{'verdict':<24}{'n':>5}")
    out.append("-" * 30)
    order = [
        "byte_identical",
        "deterministic_gain",
        "modal_gain",
        "noise",
        "modal_loss",
        "deterministic_loss",
    ]
    for v in order:
        out.append(f"{v:<24}{counter.get(v, 0):>5}")
    out.append("-" * 30)
    out.append(f"{'TOTAL':<24}{sum(counter.values()):>5}")
    out.append("")
    for v in ("deterministic_loss", "deterministic_gain", "modal_loss", "modal_gain"):
        ids = sorted(iid for iid, d in verdicts.items() if d["verdict"] == v)
        if not ids:
            continue
        out.append(f"--- {v} ({len(ids)}) ---")
        for iid in ids:
            d = verdicts[iid]
            a = "/".join(d["tiers_a"]) or "MISSING"
            b = "/".join(d["tiers_b"]) or "MISSING"
            out.append(f"  {iid:<42}  {a:>30}  →  {b}")
        out.append("")
    return "\n".join(out)


def write_snapshots(verdicts: dict[str, dict], path: Path, label_a: str, label_b: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for iid, d in sorted(verdicts.items()):
            row = {"instance_id": iid, "label_a": label_a, "label_b": label_b, **d}
            f.write(json.dumps(row) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--cycle-a",
        nargs="+",
        required=True,
        metavar=("LABEL", "REP_JSON"),
        help="Cycle A: label followed by N taxonomy JSON paths (one per rep).",
    )
    ap.add_argument(
        "--cycle-b",
        nargs="+",
        required=True,
        metavar=("LABEL", "REP_JSON"),
        help="Cycle B: label followed by N taxonomy JSON paths (one per rep).",
    )
    ap.add_argument("--snapshot-out", type=Path, default=None)
    args = ap.parse_args(argv)

    if len(args.cycle_a) < 2 or len(args.cycle_b) < 2:
        print("FATAL: --cycle-a and --cycle-b each need LABEL plus >=1 rep paths.", file=sys.stderr)
        return 2

    label_a = args.cycle_a[0]
    label_b = args.cycle_b[0]
    paths_a = [Path(p) for p in args.cycle_a[1:]]
    paths_b = [Path(p) for p in args.cycle_b[1:]]

    for p in paths_a + paths_b:
        if not p.is_file():
            print(f"FATAL: missing taxonomy: {p}", file=sys.stderr)
            return 2

    taxonomies_a = [load_taxonomy(p) for p in paths_a]
    taxonomies_b = [load_taxonomy(p) for p in paths_b]

    cycle_a = build_instance_tiers(taxonomies_a)
    cycle_b = build_instance_tiers(taxonomies_b)
    verdicts = cohort_shift(cycle_a, cycle_b)

    print(render(verdicts, label_a, label_b))

    if args.snapshot_out:
        write_snapshots(verdicts, args.snapshot_out, label_a, label_b)
        print(f"wrote feature snapshots → {args.snapshot_out}")

    deterministic_losses = sum(1 for d in verdicts.values() if d["verdict"] == "deterministic_loss")
    return 1 if deterministic_losses > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
