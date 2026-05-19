#!/usr/bin/env python3
"""Suppression audit for v1.10.4 design diligence.

The v1.10.3 W3 silent-suppression band fires ~111 `early_bail_suppressed_diffuse`
events per rep across 75 instances. Per the v1.10.3 deep-dive, the band is
non-Pareto: sphinx-10435 archetype (1 suppression → soft_anchor bail → empty
patch) is broken while matplotlib-14623 archetype (many suppressions → no
bail → wrong_target) is design-accepted.

This script classifies every suppression event in v1.10.3 rep_1/rep_2/rep_3
and characterizes the failure-mode distribution. The output drives the
v1.10.4 design candidate choice (B: count-based escalation, D: first-event
exploratory, E: breadth-signal-conditional, or a hybrid).

Classification:
  HARMLESS   — final tier was strong/plausible (success). The suppression
               did not prevent a working patch.
  HARMFUL    — final tier was empty_patch AND a subsequent intervention
               (soft_anchor, commit_imperative) fired. The suppression
               allowed a wrong-timing message to terminate the trajectory.
  ORPHANED   — final tier was empty_patch AND no subsequent intervention
               fired. The suppression was on a fail-deterministic
               trajectory; outcome not attributable to suppression.
  OUTCOME_W  — final tier was wrong_target/wrong_location. Mixed signal;
               model committed but to wrong locus. Not directly informative.

Per-trajectory archetypes:
  T_SPHINX_10435 — 1 suppression at step 4, soft_anchor at step 5,
                   empty_patch. The load-bearing failure mode.
  T_5414         — N>=3 suppressions, ANY non-empty patch within the
                   convergence band (model converged on first plausible
                   idea without case-analysis breadth). Need Docker
                   harness data to detect Docker-fail vs Docker-pass.
  T_1921         — N>=3 suppressions, then soft_anchor at step >=6,
                   non-empty patch. Suppression bought runway then the
                   right message arrived. The preserve-me case.

Usage:
  python -m scripts.audit_v1103_suppression                # full report
  python -m scripts.audit_v1103_suppression --archetype-detail
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

RUNS_DIR = Path.home() / ".luxe" / "runs"
WRITE_TOOLS = {"write_file", "edit_file"}

REPS = [
    ("v1.10.2 r1", "acceptance/swebench/post_specdd_v1102_n75/rep_1",
                   "acceptance/v1102_taxonomy/v1102_n75_full_stack_swebench.json"),
    ("v1.10.2 r2", "acceptance/swebench/post_specdd_v1102_n75/rep_2",
                   "acceptance/v1102_taxonomy/v1102_n75_rep_2_full_stack_swebench.json"),
    ("v1.10.2 r3", "acceptance/swebench/post_specdd_v1102_n75/rep_3",
                   "acceptance/v1102_taxonomy/v1102_n75_rep_3_full_stack_swebench.json"),
    ("v1.10.3 r1", "acceptance/swebench/post_specdd_v1103_n75/rep_1", None),
    ("v1.10.3 r2", "acceptance/swebench/post_specdd_v1103_n75/rep_2",
                   "acceptance/v1103_taxonomy/v1103_n75_rep_2_full_stack_swebench.json"),
    ("v1.10.3 r3", "acceptance/swebench/post_specdd_v1103_n75/rep_3",
                   "acceptance/v1103_taxonomy/v1103_n75_rep_3_full_stack_swebench.json"),
]

ARCHETYPES = [
    "sphinx-doc__sphinx-10435",
    "matplotlib__matplotlib-14623",
    "psf__requests-5414",
    "psf__requests-1921",
]


def load_events(run_id: str) -> list[dict] | None:
    p = RUNS_DIR / run_id / "events.jsonl"
    if not p.is_file():
        return None
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_taxonomy(path: str | None, predictions_path: Path) -> dict[str, dict]:
    """Return {instance_id: row} for taxonomy. If path is None,
    classify on the fly via compare_v110.classify_arm."""
    if path is not None and Path(path).is_file():
        rows = json.loads(Path(path).read_text())["rows"]
        return {r["instance_id"]: r for r in rows}
    # Fallback: classify from predictions
    from scripts.compare_v110 import classify_arm
    return classify_arm(predictions_path)


def trajectory_summary(events: list[dict], instance_id: str, rep_label: str,
                       tier: str) -> dict:
    """Build per-trajectory summary record for classification."""
    suppressions = []  # list of (step, score, recent_path_diversity)
    bail_fires = []    # list of (step, msg_variant)
    density_fires = []
    other_msgs = []
    tool_calls = []
    final_diff = None

    for e in events:
        k = e.get("kind", "")
        if k == "early_bail_suppressed_diffuse":
            suppressions.append((
                e.get("step", 0),
                e.get("convergence_score", 0.0),
                e.get("recent_path_diversity", -1),
            ))
        elif k == "early_bail_fired":
            bail_fires.append((e.get("step", 0), e.get("msg_variant", "")))
        elif k == "action_density_gate_fired":
            density_fires.append(e.get("step", 0))
        elif k == "tool_call" and e.get("phase") == "main":
            tool_calls.append((e.get("step", 0), e.get("name", ""), e.get("path", "")))
        elif k == "diff_stat":
            final_diff = (e.get("additions", 0), e.get("deletions", 0))

    writes = [tc for tc in tool_calls if tc[1] in WRITE_TOOLS]
    first_write_step = writes[0][0] if writes else None
    n_files_touched = len({tc[2] for tc in tool_calls if tc[2]})
    max_step = max((tc[0] for tc in tool_calls), default=0)

    # Archetype tagging
    archetype = None
    if len(suppressions) == 1 and bail_fires and bail_fires[0][1] == "soft_anchor" \
            and tier == "empty_patch":
        archetype = "T_SPHINX_10435"
    elif len(suppressions) >= 3 and bail_fires and bail_fires[0][1] == "soft_anchor" \
            and tier in ("strong", "plausible"):
        archetype = "T_1921"
    elif len(suppressions) >= 3 and tier in ("strong", "plausible", "wrong_target",
                                              "wrong_location"):
        archetype = "T_5414_CANDIDATE"   # need Docker to confirm pass/fail vs v1.10.2

    return {
        "instance_id": instance_id,
        "rep": rep_label,
        "tier": tier,
        "n_suppressions": len(suppressions),
        "suppression_steps": [s[0] for s in suppressions],
        "n_bail_fires": len(bail_fires),
        "bail_variants": [b[1] for b in bail_fires],
        "first_bail_step": bail_fires[0][0] if bail_fires else None,
        "first_bail_variant": bail_fires[0][1] if bail_fires else None,
        "n_density_fires": len(density_fires),
        "first_write_step": first_write_step,
        "n_files_touched": n_files_touched,
        "max_step": max_step,
        "final_diff": final_diff,
        "archetype": archetype,
    }


def classify_per_suppression(traj: dict) -> str:
    """Per-trajectory verdict on the suppression(s)."""
    tier = traj["tier"]
    n_s = traj["n_suppressions"]
    bail_vars = traj["bail_variants"]
    if n_s == 0:
        return None  # no suppression to classify
    if tier in ("strong", "plausible"):
        return "HARMLESS"
    if tier == "empty_patch":
        if any(v in ("soft_anchor", "commit_imperative") for v in bail_vars):
            return "HARMFUL"
        return "ORPHANED"
    if tier in ("wrong_target", "wrong_location"):
        return "OUTCOME_W"
    return "UNKNOWN"


def gather_rep(rep_label: str, rep_dir: str, taxonomy_path: str | None) -> list[dict]:
    """Build trajectory summaries for all 75 instances in a rep."""
    rep_path = Path(rep_dir)
    manifest_path = rep_path / "run_id_manifest.json"
    preds_path = rep_path / "predictions.json"
    if not manifest_path.is_file() or not preds_path.is_file():
        print(f"  ! {rep_label}: missing manifest or predictions", file=sys.stderr)
        return []
    manifest = json.loads(manifest_path.read_text())
    tax = load_taxonomy(taxonomy_path, preds_path)

    out = []
    for instance_id, info in manifest.items():
        run_id = info.get("run_id")
        if not run_id:
            continue
        events = load_events(run_id)
        if events is None:
            continue
        tier = tax.get(instance_id, {}).get("tier", "MISSING")
        traj = trajectory_summary(events, instance_id, rep_label, tier)
        traj["run_id"] = run_id
        traj["verdict"] = classify_per_suppression(traj)
        out.append(traj)
    return out


def render_summary(all_traj: list[dict]) -> None:
    # Restrict to v1.10.3 reps (suppression is W3-specific)
    v3 = [t for t in all_traj if t["rep"].startswith("v1.10.3")]
    n_with_supp = [t for t in v3 if t["n_suppressions"] > 0]

    print("=" * 78)
    print("v1.10.3 suppression audit")
    print("=" * 78)
    print(f"Total trajectories surveyed: {len(v3)}")
    print(f"  with ≥1 suppression:     {len(n_with_supp)}")
    print()

    # Verdict distribution
    verdicts = Counter(t["verdict"] for t in n_with_supp if t["verdict"])
    print("Per-trajectory verdict (only trajectories with ≥1 suppression):")
    for v in ["HARMLESS", "HARMFUL", "ORPHANED", "OUTCOME_W"]:
        n = verdicts.get(v, 0)
        pct = n / max(1, len(n_with_supp)) * 100
        print(f"  {v:<12}  {n:>4}  ({pct:5.1f}%)")
    print()

    # Suppression count distribution among HARMFUL
    print("HARMFUL trajectories — suppression count distribution:")
    harmful = [t for t in n_with_supp if t["verdict"] == "HARMFUL"]
    n_supp_dist = Counter(t["n_suppressions"] for t in harmful)
    for c in sorted(n_supp_dist.keys()):
        n = n_supp_dist[c]
        print(f"  n_suppressions={c}: {n} trajectories")
    print()

    # First-bail step distribution among HARMFUL
    print("HARMFUL trajectories — first_bail_step distribution:")
    fb_dist = Counter(t["first_bail_step"] for t in harmful)
    for step in sorted(fb_dist.keys(), key=lambda x: (x is None, x)):
        n = fb_dist[step]
        print(f"  first_bail_step={step}: {n} trajectories")
    print()

    # Listed HARMFUL instances
    print("HARMFUL trajectories:")
    harmful_by_inst = defaultdict(list)
    for t in harmful:
        harmful_by_inst[t["instance_id"]].append(t)
    for inst, ts in sorted(harmful_by_inst.items()):
        reps = [t["rep"].split()[-1] for t in ts]
        suppressions = [str(t["n_suppressions"]) for t in ts]
        first_bails = [str(t["first_bail_step"]) for t in ts]
        bail_vars = [str(t["first_bail_variant"]) for t in ts]
        print(f"  {inst:<40}  reps={reps}  n_supp={suppressions}  "
              f"first_bail_step={first_bails}  bail_var={bail_vars}")
    print()

    # Archetype frequency
    print("Archetype tag distribution (v1.10.3 only):")
    arch = Counter(t["archetype"] for t in n_with_supp if t["archetype"])
    for a, n in sorted(arch.items(), key=lambda x: -x[1]):
        print(f"  {a}: {n}")
    print()

    # Design implication summary
    print("=" * 78)
    print("Design implications:")
    print("=" * 78)
    # If most HARMFUL trajectories have n_supp == 1 → Candidate D fits
    if harmful:
        median_supp = sorted([t["n_suppressions"] for t in harmful])[len(harmful)//2]
        first_supp_share = sum(1 for t in harmful if t["n_suppressions"] == 1) / len(harmful)
        print(f"  HARMFUL median n_suppressions: {median_supp}")
        print(f"  HARMFUL share with n_suppressions==1: {first_supp_share*100:.1f}%")
        print()
        if first_supp_share > 0.5:
            print("  → Candidate D (first-event-only exploratory) fits the majority pattern.")
        elif median_supp >= 3:
            print("  → Candidate B (count-based escalation at N>=3) fits the majority pattern.")
        else:
            print("  → Mixed: hybrid (D + B) likely required.")


def render_archetype_detail(all_traj: list[dict]) -> None:
    """Per-archetype trajectory across all 6 reps."""
    print()
    print("=" * 78)
    print("ARCHETYPE TRAJECTORIES (all 6 reps)")
    print("=" * 78)
    for inst in ARCHETYPES:
        print(f"\n### {inst}\n")
        for t in [x for x in all_traj if x["instance_id"] == inst]:
            print(f"  {t['rep']}:")
            print(f"    tier={t['tier']} max_step={t['max_step']} "
                  f"first_write_step={t['first_write_step']} "
                  f"files_touched={t['n_files_touched']} "
                  f"final_diff={t['final_diff']}")
            print(f"    suppressions: {t['n_suppressions']} at steps {t['suppression_steps']}")
            print(f"    bail_fires: {t['n_bail_fires']} variants={t['bail_variants']} "
                  f"first_bail_step={t['first_bail_step']}")
            print(f"    density_fires: {t['n_density_fires']}")
            print(f"    verdict: {t['verdict']}  archetype_tag: {t['archetype']}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--archetype-detail", action="store_true",
                   help="Emit per-archetype trajectory across all 6 reps.")
    p.add_argument("--csv", type=Path, default=None,
                   help="Write per-trajectory rows to a CSV.")
    args = p.parse_args()

    all_traj: list[dict] = []
    for rep_label, rep_dir, tax_path in REPS:
        all_traj.extend(gather_rep(rep_label, rep_dir, tax_path))

    render_summary(all_traj)
    if args.archetype_detail:
        render_archetype_detail(all_traj)

    if args.csv:
        import csv as _csv
        with args.csv.open("w", newline="") as f:
            cols = ["rep", "instance_id", "tier", "verdict", "archetype",
                    "n_suppressions", "n_bail_fires", "first_bail_step",
                    "first_bail_variant", "n_density_fires", "first_write_step",
                    "n_files_touched", "max_step", "run_id"]
            w = _csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for t in all_traj:
                w.writerow({k: t.get(k, "") for k in cols})
        print(f"\nWrote {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
