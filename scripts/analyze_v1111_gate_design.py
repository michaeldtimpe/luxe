#!/usr/bin/env python3
"""scripts/analyze_v1111_gate_design.py — v1.11.1 Phase A' offline gate design.

Mines the retained v1.10.5 BASELINE Phase D event streams
(acceptance/swebench/post_specdd_v1105_n75, 3 reps x 75 = 225 runs) — the
uncontaminated trajectories the v1.11.1 stall gate would perturb — and tests
candidate stall predicates for SELECTIVITY before any code or bench is written.

WHY BASELINE, NOT LEVER-ON
  The v1.11 lever-ON arm (post_v111_n75) is contaminated after the reverted gate
  first fired (post-fire steps diverge from baseline). Candidate predicates are
  evaluated at fire-time from PRE-fire history, so the clean source is the
  baseline arm joined to baseline tiers (acceptance/v1105_taxonomy). The lever-ON
  arm is used only to CROSS-VALIDATE that the offline single-step reconstruction
  reproduces the actual reverted-gate fire points (soft_anchor_collapse_promote_fired).

CLASSES — re-derived from v1105_taxonomy, NOT carried-forward Phase A labels
  band universe : trajectories that entered the score<LOW band at step>=
                  _COLLAPSE_MIN_STEP (the only steps where the lever can act).
  RECOVERY (negative; gate MUST NOT fire): band-entering trajectories whose
                  baseline tier is strong/plausible. Firing on these = premature-
                  commitment tier demotion = the v1.11 failure. Behaviour-defined
                  population; xarray-3305 + pylint-4661 are named sentinels WITHIN it.
  STALL (target; gate SHOULD fire): band-entering trajectories whose baseline tier
                  is empty_patch (core) / wrong_target / wrong_location (weak).

CANDIDATE GATES — evaluated per wall step t>=_COLLAPSE_MIN_STEP with conv<LOW
  C1 temporal-persistence : trend<=0 for K consecutive wall steps (and a strict
                  trend<0 variant), counter RESET on any positive trend so the
                  window never spans a prior recovery arc. Sweep K in {2,3,4,5}.
  C2 breadth-saturation   : no new distinct successfully-touched file path in the
                  last J wall steps. Sweep J in {2,3,4}. Plus a re-expansion flag:
                  did a new distinct path appear AFTER the C2 fire step (i.e. the
                  saturation was transient consolidation, not exhaustion)?

score_trend + the LOW/min-step thresholds are imported from luxe.agents.convergence
so the offline reconstruction matches production byte-for-byte.

DECISION GATE A': a predicate clears iff it fires on STALL trajectories while
firing on ZERO RECOVERY trajectories (sentinels included). Prefer C2 if close.

OUTPUTS (to acceptance/v1111_gate_design/)
  * run_id_manifest.json — saved FIRST (per feedback_save_run_id_manifest_after_every_bench)
  * report to stdout.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

# Production signal functions/thresholds — imported so reconstruction == runtime.
from luxe.agents.convergence import (  # noqa: E402
    score_trajectory_trend,
    _SCORE_TREND_WINDOW,
    _COLLAPSE_MIN_STEP,
    _COLLAPSE_CONV_CEILING,
)

RUNS_DIR = Path(os.path.expanduser("~/.luxe/runs"))
OUT_DIR = Path("acceptance/v1111_gate_design")
LOW = _COLLAPSE_CONV_CEILING  # 0.10
MIN_STEP = _COLLAPSE_MIN_STEP  # 6

BASELINE_MANIFESTS = [
    "acceptance/swebench/post_specdd_v1105_n75/rep_1/run_id_manifest.json",
    "acceptance/swebench/post_specdd_v1105_n75/rep_2/run_id_manifest.json",
    "acceptance/swebench/post_specdd_v1105_n75/rep_3/run_id_manifest.json",
]
BASELINE_TAXONOMY = [
    "acceptance/v1105_taxonomy/v1105_n75_full_stack_swebench.json",
    "acceptance/v1105_taxonomy/v1105_n75_rep_2_full_stack_swebench.json",
    "acceptance/v1105_taxonomy/v1105_n75_rep_3_full_stack_swebench.json",
]
LEVERON_MANIFESTS = [
    "acceptance/swebench/post_v111_n75/rep_1/run_id_manifest.json",
    "acceptance/swebench/post_v111_n75/rep_2/run_id_manifest.json",
    "acceptance/swebench/post_v111_n75/rep_3/run_id_manifest.json",
]

SENTINELS_RECOVERY = {"pydata__xarray-3305", "pylint-dev__pylint-4661"}
SENTINELS_STALL = {"pylint-dev__pylint-4604", "matplotlib__matplotlib-14623",
                   "sphinx-doc__sphinx-10323"}

K_SWEEP = [2, 3, 4, 5]
J_SWEEP = [2, 3, 4]
# A path counts as "successfully touched" breadth only if the tool returned
# output. Excludes grep (path=None) and failed/empty reads. Screening heuristic.
_MIN_BYTES_FOR_TOUCH = 1


def _coerce_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _coerce_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def load_taxonomy_tiers(files):
    """rep_index (0-based) -> {instance_id: tier}."""
    out = {}
    for i, f in enumerate(files):
        rows = json.loads(Path(f).read_text())["rows"]
        out[i] = {r["instance_id"]: r["tier"] for r in rows}
    return out


def parse_run(run_dir: Path):
    """Reconstruct per-step convergence series + distinct-file breadth series
    from one baseline event stream. Returns None if unreadable."""
    ev = run_dir / "events.jsonl"
    if not ev.exists():
        return None
    conv_by_step: dict[int, float] = {}     # step -> convergence_score
    touched_paths: set[str] = set()         # cumulative distinct file paths
    new_path_steps: list[int] = []          # steps at which a new path appeared
    leveron_fire_steps: list[int] = []      # soft_anchor_collapse_promote_fired
    terminal_step = 0
    with ev.open() as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except Exception:
                continue
            kind = e.get("kind")
            if kind == "action_density_sample":
                step = _coerce_int(e.get("step"))
                if "convergence_score" in e:
                    conv_by_step[step] = _coerce_float(e.get("convergence_score"))
                    terminal_step = max(terminal_step, step)
            elif kind == "tool_call":
                step = _coerce_int(e.get("step"))
                terminal_step = max(terminal_step, step)
                path = e.get("path")
                if path in (None, "None", ""):
                    continue
                # successfully touched + genuinely new terrain
                if _coerce_int(e.get("bytes_out")) < _MIN_BYTES_FOR_TOUCH:
                    continue
                if str(e.get("duplicate")) == "True":
                    continue
                if path not in touched_paths:
                    touched_paths.add(path)
                    new_path_steps.append(step)
            elif kind == "soft_anchor_collapse_promote_fired":
                leveron_fire_steps.append(_coerce_int(e.get("step")))
    if not conv_by_step:
        return None
    return {
        "conv_by_step": conv_by_step,
        "new_path_steps": sorted(set(new_path_steps)),
        "terminal_step": terminal_step,
        "leveron_fire_steps": sorted(leveron_fire_steps),
    }


def reconstruct_signals(run: dict):
    """Per-step trend + consecutive-down counters + breadth-saturation series.

    score_log proxy = convergence_score in step order (matches loop.py's
    score_log.append at each step). trend[t] = score_trajectory_trend over the
    prefix up to t, exactly as production computes it. Counters RESET on any
    positive trend (no spanning a prior recovery arc).
    """
    steps = sorted(run["conv_by_step"])
    conv_seq = [run["conv_by_step"][s] for s in steps]

    per_step = {}
    cons_down_le = 0   # consecutive trend <= 0
    cons_down_lt = 0   # consecutive trend  < 0 (strict)
    new_set = set(run["new_path_steps"])
    last_new_path_step = None
    for idx, s in enumerate(steps):
        trend = score_trajectory_trend(conv_seq[: idx + 1], window=_SCORE_TREND_WINDOW)
        if trend > 0:
            cons_down_le = 0
            cons_down_lt = 0
        else:
            cons_down_le += 1
            if trend < 0:
                cons_down_lt += 1
            else:
                cons_down_lt = 0  # strict variant resets on flat
        if s in new_set:
            last_new_path_step = s
        steps_since_new = (s - last_new_path_step) if last_new_path_step is not None else s
        per_step[s] = {
            "conv": conv_seq[idx],
            "trend": trend,
            "cons_down_le": cons_down_le,
            "cons_down_lt": cons_down_lt,
            "steps_since_new_path": steps_since_new,
        }
    return per_step, steps


def fire_single_step(per_step, steps, min_step=MIN_STEP):
    """Reverted v1.11 gate reconstruction: first t>=min_step with conv<LOW and trend<=0."""
    for s in steps:
        if s >= min_step and per_step[s]["conv"] < LOW and per_step[s]["trend"] <= 0:
            return s
    return None


def fire_c1(per_step, steps, k, strict, min_step=MIN_STEP):
    key = "cons_down_lt" if strict else "cons_down_le"
    for s in steps:
        if s >= min_step and per_step[s]["conv"] < LOW and per_step[s][key] >= k:
            return s
    return None


def fire_c2(per_step, steps, j, min_step=MIN_STEP):
    for s in steps:
        if s >= min_step and per_step[s]["conv"] < LOW and per_step[s]["steps_since_new_path"] >= j:
            return s
    return None


def c2_reexpansion_after(run, fire_step):
    if fire_step is None:
        return False
    return any(s > fire_step for s in run["new_path_steps"])


def tier_bucket(tier):
    if tier in ("strong", "plausible"):
        return "RECOVERY"
    if tier in ("empty_patch", "wrong_target", "wrong_location"):
        return "STALL"
    return "OTHER"


def main() -> int:
    tiers = load_taxonomy_tiers(BASELINE_TAXONOMY)

    # lever-ON actual fire set, for cross-validation
    leveron_fired = set()  # (instance, rep_idx)
    for i, mf in enumerate(LEVERON_MANIFESTS):
        if not Path(mf).exists():
            continue
        man = json.loads(Path(mf).read_text())
        for inst, meta in man.items():
            r = parse_run(RUNS_DIR / meta["run_id"])
            if r and r["leveron_fire_steps"]:
                leveron_fired.add((inst, i))

    rows = []          # one per (instance, rep)
    manifest = {}
    for i, mf in enumerate(BASELINE_MANIFESTS):
        man = json.loads(Path(mf).read_text())
        for inst, meta in man.items():
            rid = meta["run_id"]
            run = parse_run(RUNS_DIR / rid)
            if run is None:
                continue
            per_step, steps = reconstruct_signals(run)
            tier = tiers.get(i, {}).get(inst, "unknown")
            entered_band = any(
                s >= MIN_STEP and per_step[s]["conv"] < LOW for s in steps
            )
            row = {
                "instance": inst, "rep": i + 1, "run_id": rid,
                "tier": tier, "bucket": tier_bucket(tier),
                "entered_band": entered_band,
                "terminal_step": run["terminal_step"],
                "single_step_fire": fire_single_step(per_step, steps),
                "_per_step": per_step, "_steps": steps,
            }
            for k in K_SWEEP:
                row[f"c1le_K{k}"] = fire_c1(per_step, steps, k, strict=False)
                row[f"c1lt_K{k}"] = fire_c1(per_step, steps, k, strict=True)
            for j in J_SWEEP:
                fs = fire_c2(per_step, steps, j)
                row[f"c2_J{j}"] = fs
                row[f"c2_J{j}_reexpand"] = c2_reexpansion_after(run, fs)
            rows.append(row)
            manifest[rid] = {"instance": inst, "rep": i + 1, "tier": tier,
                             "entered_band": entered_band}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "run_id_manifest.json").write_text(json.dumps(manifest, indent=1))

    # ---- cross-validation: offline single-step reconstruction vs actual lever-ON fires
    print(f"\n=== CROSS-VALIDATION: offline single-step gate vs lever-ON actual fires ===")
    if leveron_fired:
        offline_fire = {(r["instance"], r["rep"] - 1) for r in rows
                        if r["single_step_fire"] is not None}
        inter = offline_fire & leveron_fired
        print(f"  lever-ON actual fires (soft_anchor_collapse_promote_fired): {len(leveron_fired)}")
        print(f"  offline single-step reconstruction fires:                  {len(offline_fire)}")
        print(f"  agreement (intersection):                                  {len(inter)}")
        only_actual = leveron_fired - offline_fire
        only_offline = offline_fire - leveron_fired
        if only_actual:
            print(f"  WARN actual-but-not-offline (n={len(only_actual)}): "
                  f"{sorted(only_actual)[:6]}")
        if only_offline:
            print(f"  note offline-but-not-actual (n={len(only_offline)}): "
                  f"baseline trajectory differs from lever-ON; expected (≤ few)")
    else:
        print("  no lever-ON fire events found (check post_v111_n75 retention).")

    # ---- band universe by tier
    band = [r for r in rows if r["entered_band"]]
    print(f"\n=== BAND UNIVERSE (entered conv<LOW at step>={MIN_STEP}): "
          f"{len(band)}/{len(rows)} (instance,rep) ===")
    by_bucket = defaultdict(lambda: defaultdict(int))
    for r in band:
        by_bucket[r["bucket"]][r["tier"]] += 1
    for b in ("RECOVERY", "STALL", "OTHER"):
        print(f"  {b:9}: {dict(by_bucket[b])}  (n={sum(by_bucket[b].values())})")

    recov = [r for r in band if r["bucket"] == "RECOVERY"]
    stall = [r for r in band if r["bucket"] == "STALL"]

    def selectivity(field):
        """fires on recovery (BAD), fires on stall (GOOD), sentinel fires, fire-steps."""
        rf = [r for r in recov if r[field] is not None]
        sf = [r for r in stall if r[field] is not None]
        sent_r = sorted({r["instance"] for r in rf if r["instance"] in SENTINELS_RECOVERY})
        fire_steps = [r[field] for r in sf]
        mfs = f"{median(fire_steps):.0f}" if fire_steps else "—"
        return len(rf), len(sf), sent_r, mfs

    print(f"\n=== SELECTIVITY TABLE  (recovery_fires want 0; stall_fires want high) ===")
    print(f"  recovery n={len(recov)}  stall n={len(stall)}")
    print(f"{'predicate':14} {'recov_fire':>11} {'stall_fire':>11} "
          f"{'med_stall_firestep':>19} {'recov_sentinels_fired':>22}")
    rows_out = []
    # single-step (the reverted v1.11 gate) as the baseline-to-beat
    rf, sf, sent, mfs = selectivity("single_step_fire")
    print(f"{'v1.11 single':14} {rf:>11} {sf:>11} {mfs:>19} "
          f"{(','.join(s.split('__')[-1] for s in sent) or '-'):>22}")
    for k in K_SWEEP:
        for variant, lab in (("c1le", "C1<=0"), ("c1lt", "C1<0")):
            field = f"{variant}_K{k}"
            rf, sf, sent, mfs = selectivity(field)
            tag = f"{lab} K={k}"
            clears = "  CLEARS" if (rf == 0 and sf > 0) else ""
            print(f"{tag:14} {rf:>11} {sf:>11} {mfs:>19} "
                  f"{(','.join(s.split('__')[-1] for s in sent) or '-'):>22}{clears}")
    for j in J_SWEEP:
        field = f"c2_J{j}"
        rf, sf, sent, mfs = selectivity(field)
        reexp = sum(1 for r in stall if r[field] is not None and r[f"c2_J{j}_reexpand"])
        clears = "  CLEARS" if (rf == 0 and sf > 0) else ""
        tag = f"C2 J={j}"
        print(f"{tag:14} {rf:>11} {sf:>11} {mfs:>19} "
              f"{(','.join(s.split('__')[-1] for s in sent) or '-'):>22}"
              f"  [stall_reexpand={reexp}]{clears}")

    # ---- recovery false-positive detail (the expensive errors)
    print(f"\n=== RECOVERY FALSE-POSITIVE DETAIL (per predicate, which recovery insts fire) ===")
    for field in (["single_step_fire"]
                  + [f"c1lt_K{k}" for k in K_SWEEP]
                  + [f"c2_J{j}" for j in J_SWEEP]):
        fired = sorted({f"{r['instance'].split('__')[-1]}({r['tier'][:4]})"
                        for r in recov if r[field] is not None})
        if fired:
            print(f"  {field:14}: {fired}")

    # ---- sentinel trajectory dump (named anchors, all reps)
    print(f"\n=== SENTINEL PER-REP (fire step under each gate; '-' = no fire) ===")
    cols = ["single_step_fire", "c1lt_K3", "c1lt_K4", "c2_J2", "c2_J3", "c2_J4"]
    print(f"{'instance':30} {'rep':>3} {'tier':>11} {'band':>5} "
          + " ".join(f"{c.replace('_fire','').replace('single_step','1step'):>9}" for c in cols))
    for inst in sorted(SENTINELS_RECOVERY | SENTINELS_STALL):
        for r in sorted([x for x in rows if x["instance"] == inst], key=lambda x: x["rep"]):
            cells = " ".join(f"{(str(r[c]) if r[c] is not None else '-'):>9}" for c in cols)
            print(f"{inst:30} {r['rep']:>3} {r['tier']:>11} "
                  f"{('Y' if r['entered_band'] else 'n'):>5} {cells}")

    # ---- fire-step threshold sweep: does a LATER gate exit recovering trajectories?
    # Recovery dips that recover should leave the conv<LOW band by a later step;
    # a later min_step might clear them while still catching genuine stalls.
    print(f"\n=== MIN_STEP SWEEP  (best C1<0 K=4 and C2 J=4; recov_fire want 0) ===")
    print(f"{'gate':16} {'min_step':>8} {'recov_fire':>11} {'stall_fire':>11} "
          f"{'recov_sentinels':>18}")
    for gate_lab, fn in (
        ("C1<0 K=4", lambda r, m: fire_c1(r["_per_step"], r["_steps"], 4, True, m)),
        ("C2 J=4", lambda r, m: fire_c2(r["_per_step"], r["_steps"], 4, m)),
        ("C2 J=5", lambda r, m: fire_c2(r["_per_step"], r["_steps"], 5, m)),
    ):
        for m in (6, 8, 10, 11, 12):
            rf = [r for r in recov if fn(r, m) is not None]
            sf = [r for r in stall if fn(r, m) is not None]
            sent = sorted({r["instance"].split("__")[-1] for r in rf
                           if r["instance"] in SENTINELS_RECOVERY})
            clears = "  CLEARS" if (len(rf) == 0 and len(sf) > 0) else ""
            print(f"{gate_lab:16} {m:>8} {len(rf):>11} {len(sf):>11} "
                  f"{(','.join(sent) or '-'):>18}{clears}")

    print(f"\nmanifest written to {OUT_DIR}/run_id_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
