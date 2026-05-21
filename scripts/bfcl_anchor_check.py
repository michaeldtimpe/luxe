"""BFCL anchor-comparison analyzer with hard ship gates.

Compares a new BFCL run directory against an anchor (default:
acceptance/bfcl/post_v1105_stage1_verify/rep_1). Output:
  - per-category pass-rate deltas
  - per-instance flip table (anchor PASS → new FAIL and the reverse)
  - hard gates: TOTAL >= --total-floor AND irrelevance == --irrelevance-floor

Codifies the Stage 1 deep-dive finding (v1.10.5 reproduced v1.8 anchor
byte-identically: 1119/1240 = 90.24%, irrelevance 240/240 = 100%).
Future BFCL cycles run this against the new run to catch drift.

Feature snapshots (--snapshot-out): JSONL per-problem with the verdict
inputs (anchor_pass, new_pass, anchor_reason, new_reason, deltas). The
v1.10.5b lesson — verify features at the source, not from hand
derivation — applies cross-bench.

Exit codes:
  0 — both hard gates clear
  1 — at least one hard gate failed
  2 — missing inputs / parse error

Usage:
  python -m scripts.bfcl_anchor_check \\
      --anchor acceptance/bfcl/post_v1105_stage1_verify/rep_1 \\
      --new acceptance/bfcl/<new_run>/rep_1 \\
      [--snapshot-out acceptance/bfcl/drift/<new>.jsonl] \\
      [--total-floor 90.00] [--irrelevance-floor 240]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

CATEGORIES = ("simple_python", "multiple", "parallel", "parallel_multiple", "irrelevance")


def load_results(run_dir: Path, categories: tuple[str, ...] | None = None) -> dict[str, dict]:
    """{problem_id: per-problem-json} across requested BFCL categories.

    `categories=None` loads all 5 (full anchor). A subset enables cheap
    probes — e.g. Stage 3 Phase 3b uses `("irrelevance",)` for a ~70 min
    dispatch-layer regression detector before n=75 burns 30h.
    """
    targets = categories if categories is not None else CATEGORIES
    out: dict[str, dict] = {}
    for cat in targets:
        d = run_dir / cat
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            try:
                r = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            r.setdefault("category", cat)
            out[r["id"]] = r
    return out


def compare(anchor: dict[str, dict], new: dict[str, dict]) -> dict:
    """Per-category roll-up + per-instance flip lists. Pure."""
    by_cat: dict[str, dict] = {
        c: {"both_P": 0, "both_F": 0, "F_to_P": [], "P_to_F": [],
            "anchor_pass": 0, "new_pass": 0, "n": 0}
        for c in CATEGORIES
    }
    ids = sorted(set(anchor) | set(new))
    for iid in ids:
        a = anchor.get(iid, {})
        n = new.get(iid, {})
        cat = a.get("category") or n.get("category") or "unknown"
        if cat not in by_cat:
            by_cat[cat] = {"both_P": 0, "both_F": 0, "F_to_P": [], "P_to_F": [],
                           "anchor_pass": 0, "new_pass": 0, "n": 0}
        ap = a.get("passed") is True
        np_ = n.get("passed") is True
        by_cat[cat]["n"] += 1
        if ap:
            by_cat[cat]["anchor_pass"] += 1
        if np_:
            by_cat[cat]["new_pass"] += 1
        if ap and np_:
            by_cat[cat]["both_P"] += 1
        elif (not ap) and (not np_):
            by_cat[cat]["both_F"] += 1
        elif ap and not np_:
            by_cat[cat]["P_to_F"].append(iid)
        elif np_ and not ap:
            by_cat[cat]["F_to_P"].append(iid)

    total_n = sum(c["n"] for c in by_cat.values())
    total_anchor_pass = sum(c["anchor_pass"] for c in by_cat.values())
    total_new_pass = sum(c["new_pass"] for c in by_cat.values())
    total_PtoF = sum(len(c["P_to_F"]) for c in by_cat.values())
    total_FtoP = sum(len(c["F_to_P"]) for c in by_cat.values())
    return {
        "per_category": by_cat,
        "total_n": total_n,
        "total_anchor_pass": total_anchor_pass,
        "total_new_pass": total_new_pass,
        "total_anchor_pct": (100 * total_anchor_pass / total_n) if total_n else 0.0,
        "total_new_pct": (100 * total_new_pass / total_n) if total_n else 0.0,
        "total_P_to_F": total_PtoF,
        "total_F_to_P": total_FtoP,
        "agreement_pct": (100 * (total_n - total_PtoF - total_FtoP) / total_n) if total_n else 0.0,
    }


def check_gates(report: dict, total_floor: float, irrelevance_floor: int) -> dict:
    """Boolean verdicts on the two hard gates."""
    irrel = report["per_category"].get("irrelevance", {})
    return {
        "total_pct_gate": report["total_new_pct"] >= total_floor,
        "total_pct_value": report["total_new_pct"],
        "total_pct_floor": total_floor,
        "irrelevance_gate": irrel.get("new_pass", 0) >= irrelevance_floor,
        "irrelevance_value": irrel.get("new_pass", 0),
        "irrelevance_floor": irrelevance_floor,
    }


def render(report: dict, gates: dict, label_a: str, label_b: str) -> str:
    out: list[str] = []
    out.append("=" * 72)
    out.append(f"BFCL anchor comparison: {label_a} (anchor) vs {label_b} (new)")
    out.append("=" * 72)
    out.append("")
    out.append(f"{'category':<20}{'anchor':>10}{'new':>10}{'Δ':>8}{'F→P':>6}{'P→F':>6}")
    out.append("-" * 60)
    for cat in CATEGORIES:
        c = report["per_category"].get(cat, {})
        if not c.get("n"):
            continue
        a = c["anchor_pass"]
        n_ = c["new_pass"]
        delta = n_ - a
        n_total = c["n"]
        out.append(f"{cat:<20}{a:>4}/{n_total:<5}{n_:>4}/{n_total:<5}{delta:>+8d}"
                   f"{len(c['F_to_P']):>6}{len(c['P_to_F']):>6}")
    out.append("-" * 60)
    out.append(
        f"{'TOTAL':<20}"
        f"{report['total_anchor_pass']:>4}/{report['total_n']:<5}"
        f"{report['total_new_pass']:>4}/{report['total_n']:<5}"
        f"{report['total_new_pass'] - report['total_anchor_pass']:>+8d}"
        f"{report['total_F_to_P']:>6}{report['total_P_to_F']:>6}"
    )
    out.append("")
    out.append(f"agreement: {report['agreement_pct']:.2f}%")
    out.append(f"anchor:    {report['total_anchor_pct']:.2f}%")
    out.append(f"new:       {report['total_new_pct']:.2f}%")
    out.append("")
    out.append("--- Hard gates ---")
    if gates.get("total_pct_skipped"):
        out.append(f"  TOTAL >= {gates['total_pct_floor']:.2f}%       : SKIP (subset run; TOTAL not comparable to full anchor)")
    else:
        tg = "PASS" if gates["total_pct_gate"] else "FAIL"
        out.append(f"  TOTAL >= {gates['total_pct_floor']:.2f}%       : {tg} ({gates['total_pct_value']:.2f}%)")
    ig = "PASS" if gates["irrelevance_gate"] else "FAIL"
    out.append(f"  irrelevance == {gates['irrelevance_floor']:>3} : {ig} ({gates['irrelevance_value']})")
    out.append("")
    # Per-instance flip details (only when there are flips)
    for cat in CATEGORIES:
        c = report["per_category"].get(cat, {})
        if c.get("P_to_F"):
            out.append(f"--- {cat}: P→F regressions ({len(c['P_to_F'])}) ---")
            for iid in c["P_to_F"][:25]:
                out.append(f"  - {iid}")
        if c.get("F_to_P"):
            out.append(f"--- {cat}: F→P recoveries ({len(c['F_to_P'])}) ---")
            for iid in c["F_to_P"][:25]:
                out.append(f"  + {iid}")
    return "\n".join(out)


def write_snapshots(anchor: dict[str, dict], new: dict[str, dict], path: Path,
                    label_a: str, label_b: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for iid in sorted(set(anchor) | set(new)):
            a = anchor.get(iid, {})
            n = new.get(iid, {})
            row = {
                "id": iid,
                "category": a.get("category") or n.get("category"),
                "anchor_label": label_a,
                "new_label": label_b,
                "anchor_pass": a.get("passed"),
                "new_pass": n.get("passed"),
                "anchor_reason": a.get("reason"),
                "new_reason": n.get("reason"),
                "anchor_wall_s": a.get("wall_s"),
                "new_wall_s": n.get("wall_s"),
            }
            f.write(json.dumps(row) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", type=Path, required=True,
                    help="Anchor BFCL run dir (contains per-category subdirs).")
    ap.add_argument("--new", type=Path, required=True,
                    help="New BFCL run dir to compare against the anchor.")
    ap.add_argument("--categories", nargs="+", choices=list(CATEGORIES), default=None,
                    help="Restrict comparison to a subset of BFCL v4 categories. "
                         "Default: all 5 (full anchor). Use 'irrelevance' alone for "
                         "the Stage 3 Phase 3b cheap dispatch-layer probe (~70 min). "
                         "When a strict subset is passed, --total-floor is skipped "
                         "(TOTAL is not comparable to the full-anchor 90.24%%).")
    ap.add_argument("--snapshot-out", type=Path, default=None,
                    help="Optional: emit per-problem snapshot JSONL.")
    ap.add_argument("--total-floor", type=float, default=90.00,
                    help="Hard gate: TOTAL pass rate (%%) must be >= this. "
                         "Skipped automatically when --categories is a strict subset.")
    ap.add_argument("--irrelevance-floor", type=int, default=240,
                    help="Hard gate: irrelevance pass count must be >= this.")
    ap.add_argument("--label-anchor", default="anchor")
    ap.add_argument("--label-new", default="new")
    args = ap.parse_args(argv)

    for p in (args.anchor, args.new):
        if not p.is_dir():
            print(f"FATAL: missing run dir: {p}", file=sys.stderr)
            return 2

    categories = tuple(args.categories) if args.categories else None
    anchor = load_results(args.anchor, categories=categories)
    new = load_results(args.new, categories=categories)
    if not anchor:
        print(f"FATAL: no per-problem JSONs found in anchor {args.anchor}", file=sys.stderr)
        return 2
    if not new:
        print(f"FATAL: no per-problem JSONs found in new {args.new}", file=sys.stderr)
        return 2

    report = compare(anchor, new)
    is_subset = categories is not None and len(categories) < len(CATEGORIES)
    gates = check_gates(report, args.total_floor, args.irrelevance_floor)
    if is_subset:
        # Subset runs cannot meaningfully compare TOTAL to the full-anchor floor.
        # Mark the TOTAL gate as skipped (preserves irrelevance gate semantics).
        gates["total_pct_gate"] = True
        gates["total_pct_skipped"] = True
    print(render(report, gates, args.label_anchor, args.label_new))

    if args.snapshot_out:
        write_snapshots(anchor, new, args.snapshot_out, args.label_anchor, args.label_new)
        print(f"wrote feature snapshots → {args.snapshot_out}")

    if not (gates["total_pct_gate"] and gates["irrelevance_gate"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
