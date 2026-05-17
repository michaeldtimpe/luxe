"""Cross-rep variance analysis for the v1.10.2 n=75 baseline.

Generates per-instance and roll-up volatility tables across N reps. Pure
inspector-tier (predictions + taxonomy); Docker harness is intentionally
out of scope here — Docker is a separate decision gate run once across
the union of rep predictions.

Usage::

    python -m scripts.variance_v1102_3rep \\
        --rep acceptance/v1102_taxonomy/v1102_n75_full_stack_swebench.json \\
        --rep acceptance/v1102_taxonomy/v1102_n75_rep_2_full_stack_swebench.json

Repeat ``--rep`` for each taxonomy. Order in the args determines the
column order in the report (rep_1, rep_2, rep_3, ...).
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

TIER_ORDER = [
    "strong",
    "plausible",
    "wrong_target",
    "wrong_location",
    "empty_patch",
    "new_file_in_diff",
]


def load_rows(path: Path) -> dict[str, dict]:
    rows = json.loads(path.read_text())["rows"]
    return {r["instance_id"]: r for r in rows}


def summarize_arm(rows: dict[str, dict]) -> dict:
    """Roll up tier counts + key derived counters."""
    tier_counts: Counter[str] = Counter()
    empty_patch = 0
    strong = 0
    plausible = 0
    for r in rows.values():
        tier_counts[r["tier"]] += 1
        if r["tier"] == "strong":
            strong += 1
        elif r["tier"] == "plausible":
            plausible += 1
        elif r["tier"] == "empty_patch":
            empty_patch += 1
    return {
        "n": len(rows),
        "tier_counts": dict(tier_counts),
        "strong": strong,
        "plausible": plausible,
        "strong_plus_plausible": strong + plausible,
        "empty_patch": empty_patch,
    }


def flip_table(reps: list[dict[str, dict]], labels: list[str]) -> tuple[list[dict], dict]:
    """Per-instance tier trace across all reps.

    Returns (per_instance_rows, summary_stats). per_instance_rows
    contains only instances that flipped tier at least once across reps."""
    assert len(reps) == len(labels)
    instance_ids = sorted(set().union(*[set(r.keys()) for r in reps]))
    per_instance = []
    n_flipped = 0
    n_stable = 0
    flip_kinds: Counter[str] = Counter()
    for iid in instance_ids:
        tiers = [r.get(iid, {}).get("tier", "MISSING") for r in reps]
        if len(set(tiers)) > 1:
            n_flipped += 1
            # Categorize flip: e.g., "strong<->plausible", "empty<->non-empty"
            uniq = tuple(sorted(set(tiers)))
            flip_kinds["/".join(uniq)] += 1
            per_instance.append({
                "instance_id": iid,
                "tiers": dict(zip(labels, tiers)),
                "patch_lens": {
                    lbl: r.get(iid, {}).get("patch_len") for lbl, r in zip(labels, reps)
                },
                "outcomes": {
                    lbl: r.get(iid, {}).get("outcome") for lbl, r in zip(labels, reps)
                },
            })
        else:
            n_stable += 1
    return per_instance, {
        "n_instances": len(instance_ids),
        "n_stable": n_stable,
        "n_flipped": n_flipped,
        "flip_fraction": n_flipped / max(1, len(instance_ids)),
        "flip_kinds": dict(flip_kinds),
    }


def render_summary(summaries: list[dict], labels: list[str]) -> str:
    out = ["", "=" * 72, "Per-rep roll-up", "=" * 72, ""]
    header = f"{'metric':<28}" + "".join(f"{lbl:>10}" for lbl in labels)
    out.append(header)
    out.append("-" * len(header))
    keys = ["strong", "plausible", "strong_plus_plausible", "empty_patch"]
    for k in keys:
        row = f"{k:<28}" + "".join(f"{s[k]:>10}" for s in summaries)
        out.append(row)
    out.append("")
    for t in TIER_ORDER:
        if any(t in s["tier_counts"] for s in summaries):
            row = f"  tier:{t:<22}" + "".join(
                f"{s['tier_counts'].get(t, 0):>10}" for s in summaries)
            out.append(row)
    return "\n".join(out)


def render_flips(per_instance: list[dict], stats: dict, labels: list[str]) -> str:
    out = ["", "=" * 72, "Per-instance volatility", "=" * 72, ""]
    out.append(f"n_instances={stats['n_instances']}  "
               f"stable={stats['n_stable']}  "
               f"flipped={stats['n_flipped']}  "
               f"flip_fraction={stats['flip_fraction']:.1%}")
    out.append("")
    out.append("Flip-kind tallies (tier set across reps):")
    for k, v in sorted(stats["flip_kinds"].items(), key=lambda x: -x[1]):
        out.append(f"  {v:>3}  {k}")
    out.append("")
    out.append("Flipped instances:")
    col_w = 40
    header = f"{'instance':<{col_w}}" + "".join(f"{lbl:>14}" for lbl in labels)
    out.append(header)
    out.append("-" * len(header))
    for row in per_instance:
        line = f"{row['instance_id']:<{col_w}}"
        for lbl in labels:
            tier = row["tiers"].get(lbl, "?")
            line += f"{tier:>14}"
        out.append(line)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rep", action="append", required=True,
                    type=Path,
                    help="Path to a rep's taxonomy JSON (rows-of-dict shape). "
                         "Pass once per rep, in chronological order.")
    ap.add_argument("--labels", default="",
                    help="Comma-separated rep labels (default: rep_1, rep_2, ...).")
    args = ap.parse_args()

    if args.labels:
        labels = [s.strip() for s in args.labels.split(",")]
        assert len(labels) == len(args.rep), \
            "labels count must match rep count"
    else:
        labels = [f"rep_{i+1}" for i in range(len(args.rep))]

    reps = [load_rows(p) for p in args.rep]
    summaries = [summarize_arm(r) for r in reps]
    per_instance, flip_stats = flip_table(reps, labels)

    print(render_summary(summaries, labels))
    print(render_flips(per_instance, flip_stats, labels))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
