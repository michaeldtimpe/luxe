#!/usr/bin/env python3
"""
scripts/analyze_v111_calibration.py — v1.11 Phase A calibration analyzer.

Reads the retained adaptive-policy event corpus under ~/.luxe/runs/*/events.jsonl
(written by the inert-substrate Phase 3a/3b/4 runs) and answers the Phase A
question:

    Does any empty_patch trajectory contain a recoverable regime transition
    before terminal collapse — i.e. is there writable horizon left after a
    no_write threshold would fire, and is convergence still improving — or
    are empties monotonic stalls that write_pressure can only perturb?

DATA NOTES (verified against the corpus before writing this):
  * adaptive_state events are FLAT: keys live at the top level of the event
    dict (consecutive_no_write, score_trend, convergence_score, modulation_*),
    NOT under a nested "adaptive_state" key.
  * instance_id is recovered from run.json's repo_path
    (.../swebench-workspace/<instance_id>/repo).
  * per-run outcome is self-contained: a `diff_stat` event with
    additions+deletions == 0 (or absent) => empty_patch; > 0 => patched.
    Full 5-tier grading needs Docker (Phase D) and is NOT available here;
    the calibration axis is empty_patch vs patched, which is the no_write
    target class anyway.
  * the corpus mixes ablation conditions. When a signal was disabled its
    adaptive_state field is null. Each run is tagged with which signals were
    live, and a run is excluded from a signal's calibration when that signal
    was ablated.

OUTPUTS (to acceptance/v1110_calibration/):
  * run_id_manifest.json — run_id -> {instance, outcome, condition, started_at}
    saved FIRST, before any retention rotation.
  * report printed to stdout.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median, mean

RUNS_DIR = Path("~/.luxe/runs").expanduser()
OUT_DIR = Path("acceptance/v1110_calibration")
# Authoritative outcomes live in the acceptance result JSONs (patch_present);
# diff_stat after_main_pass over-counts patches (mid-run writes get reverted).
ACCEPTANCE_GLOB = "acceptance/swebench/v1110_*/rep_*/*.json"
# Only consider runs from the v1.11 calibration window (Phase 3a onward).
CUTOFF_TS = 1779331200.0  # 2026-05-20 20:00 local-ish; runs older are pre-v1.11
NOMINAL_MAX_STEPS = 20    # informational: configured single-mode cap
_INSTANCE_RE = re.compile(r"swebench-workspace/(.+?)/repo")


def build_outcome_index() -> dict[str, list[tuple[float, bool, int]]]:
    """instance_id -> [(wall_s, patch_present, patch_lines), ...] from the
    authoritative acceptance result JSONs. Join key for runs is (instance,
    nearest wall_s) — wall_s is a continuous near-unique per-run measurement."""
    idx: dict[str, list[tuple[float, bool, int]]] = defaultdict(list)
    import glob
    for f in glob.glob(ACCEPTANCE_GLOB):
        if f.endswith(("summary.json", "predictions.json")):
            continue
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        if "patch_present" not in d or "instance_id" not in d:
            continue
        idx[d["instance_id"]].append(
            (float(d.get("wall_s", 0.0)), bool(d["patch_present"]),
             int(d.get("patch_lines", 0))))
    return idx


def resolve_outcome(idx, instance: str, wall_s: float | None) -> str:
    """Authoritative outcome by nearest-wall_s match within the instance's
    result rows. Returns 'empty_patch' / 'patched' / 'unknown'."""
    rows = idx.get(instance)
    if not rows or wall_s is None:
        return "unknown"
    _, present, _ = min(rows, key=lambda r: abs(r[0] - wall_s))
    return "patched" if present else "empty_patch"


def _slope(series: list[float]) -> float:
    """Endpoint slope (last - first) over the series. Robust to noise vs OLS."""
    return (series[-1] - series[0]) if len(series) >= 2 else 0.0


def _sign_flips(series: list[float]) -> int:
    diffs = [b - a for a, b in zip(series, series[1:]) if (b - a) != 0.0]
    signs = [1 if d > 0 else -1 for d in diffs]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def _first_crossing(no_write_series: list[tuple[int, int]], t: int) -> int | None:
    """First step at which consecutive_no_write >= t. Series is [(step, val)]."""
    for step, val in no_write_series:
        if val is not None and val >= t:
            return step
    return None


def load_run(run_dir: Path, idx) -> dict | None:
    ev = run_dir / "events.jsonl"
    rj = run_dir / "run.json"
    if not ev.exists() or not rj.exists():
        return None
    try:
        run_meta = json.loads(rj.read_text())
    except Exception:
        return None
    started = run_meta.get("started_at", 0.0)
    if started < CUTOFF_TS:
        return None

    m = _INSTANCE_RE.search(run_meta.get("repo_path", ""))
    instance = m.group(1) if m else "unknown"

    # adaptive_state series + outcome-bearing events
    nw_series: list[tuple[int, int | None]] = []   # (step, consecutive_no_write)
    trend_series: list[float] = []
    conv_series: list[tuple[int, float]] = []       # (step, convergence_score)
    mod_departures = 0          # write_pressure-specific (the only wired lever)
    mod_departures_other = 0    # early_bail + soft_anchor (computed-but-inert)
    no_write_live = False
    score_trend_live = False
    early_bail = None
    terminal_step = 0
    wall_s = None
    saw_adaptive = False

    with ev.open() as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except Exception:
                continue
            kind = e.get("kind")
            if kind == "adaptive_state":
                saw_adaptive = True
                step = e.get("step", 0)
                terminal_step = max(terminal_step, step)
                nw = e.get("consecutive_no_write")
                if nw is not None:
                    no_write_live = True
                nw_series.append((step, nw))
                st = e.get("score_trend")
                if st is not None:
                    score_trend_live = True
                    trend_series.append(st)
                cs = e.get("convergence_score")
                if cs is not None:
                    conv_series.append((step, cs))
                if e.get("modulation_write_pressure", 1.0) != 1.0:
                    mod_departures += 1
                for mk in ("modulation_early_bail", "modulation_soft_anchor"):
                    if e.get(mk, 1.0) != 1.0:
                        mod_departures_other += 1
            elif kind == "single_mode_done":
                wall_s = e.get("wall_s")
            elif kind == "early_bail_fired":
                early_bail = {"step": e.get("step"),
                              "score": e.get("convergence_score"),
                              "variant": e.get("msg_variant")}

    if not saw_adaptive:
        return None

    outcome = resolve_outcome(idx, instance, wall_s)
    condition = ("both_on" if (no_write_live and score_trend_live)
                 else "no_write_off" if score_trend_live
                 else "score_trend_off" if no_write_live
                 else "both_off")

    return {
        "run_id": run_dir.name,
        "instance": instance,
        "task_type": run_meta.get("task_type"),
        "started_at": started,
        "outcome": outcome,
        "wall_s": wall_s,
        "condition": condition,
        "no_write_live": no_write_live,
        "score_trend_live": score_trend_live,
        "mod_departures": mod_departures,
        "mod_departures_other": mod_departures_other,
        "terminal_step": terminal_step,
        "max_no_write": max((v for _, v in nw_series if v is not None), default=0),
        "terminal_no_write": next((v for _, v in reversed(nw_series)
                                   if v is not None), 0),
        "nw_series": nw_series,
        "conv_series": conv_series,
        "trend_terminal": trend_series[-1] if trend_series else None,
        "trend_min": min(trend_series) if trend_series else None,
        "trend_sign_flips": _sign_flips(trend_series) if trend_series else None,
        "trend_slope": _slope(trend_series) if trend_series else None,
        "early_bail": early_bail,
    }


def conv_rising_after_stall(run: dict, stall: int = 5) -> bool | None:
    """For a run whose no_write counter reached `stall`, did convergence_score
    rise at any point AFTER that crossing? True => recoverable regime;
    False => monotonic stall. None => never reached the stall depth."""
    cross = _first_crossing(run["nw_series"], stall)
    if cross is None:
        return None
    after = [cs for step, cs in run["conv_series"] if step >= cross]
    if len(after) < 2:
        return False
    return any(b > a for a, b in zip(after, after[1:]))


def main() -> int:
    idx = build_outcome_index()
    runs = [r for d in sorted(RUNS_DIR.iterdir()) if d.is_dir()
            for r in [load_run(d, idx)] if r]
    if not runs:
        print("No v1.11 calibration runs found.", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {r["run_id"]: {k: r[k] for k in
                ("instance", "outcome", "condition", "started_at",
                 "wall_s", "terminal_step", "max_no_write")} for r in runs}
    (OUT_DIR / "run_id_manifest.json").write_text(json.dumps(manifest, indent=1))

    print(f"\n=== v1.11 CALIBRATION CORPUS: {len(runs)} runs ===")
    by_cond = defaultdict(int)
    for r in runs:
        by_cond[r["condition"]] += 1
    print("conditions:", dict(by_cond))
    wp_dep = sum(r["mod_departures"] for r in runs)
    other_dep = sum(r["mod_departures_other"] for r in runs)
    print(f"MODULATION REALITY: write_pressure (WIRED) departed 1.0 in {wp_dep} "
          f"events; early_bail+soft_anchor (computed-but-inert) in {other_dep}.")
    print(f"  => substrate is NOT inert on this corpus where no_write>=8 fires; "
          f"behavior preservation comes from effect-size, not from mod==1.0.")
    n_unknown = sum(1 for r in runs if r["outcome"] == "unknown")
    if n_unknown:
        print(f"  WARN: {n_unknown} runs had no authoritative outcome match.")
    by_out = defaultdict(int)
    for r in runs:
        by_out[r["outcome"]] += 1
    print("outcomes:", dict(by_out))

    # no_write calibration uses runs where no_write was live.
    nw_runs = [r for r in runs if r["no_write_live"]]
    print(f"\n=== max_consecutive_no_write BY OUTCOME (n={len(nw_runs)} no_write-live) ===")
    for out in ("empty_patch", "patched"):
        vals = [r["max_no_write"] for r in nw_runs if r["outcome"] == out]
        if vals:
            print(f"  {out:12} n={len(vals):3} min={min(vals)} max={max(vals)} "
                  f"mean={mean(vals):.1f} median={median(vals)}")

    print("\n=== COUNTERFACTUAL CONFUSION TABLE (no_write threshold T) ===")
    print(f"{'T':>3} {'fire_empty':>11} {'fire_patch':>11} {'precision':>10} "
          f"{'med_fire_step':>14} {'med_remain_steps':>17} {'too_late(<=1)':>14}")
    empties = [r for r in nw_runs if r["outcome"] == "empty_patch"]
    patched = [r for r in nw_runs if r["outcome"] == "patched"]
    for t in range(3, 13):
        fe = [r for r in empties if r["max_no_write"] >= t]
        fp = [r for r in patched if r["max_no_write"] >= t]
        total = len(fe) + len(fp)
        prec = f"{len(fe)/total:.0%}" if total else "—"
        fire_steps, remain = [], []
        for r in fe:
            c = _first_crossing(r["nw_series"], t)
            if c is not None:
                fire_steps.append(c)
                remain.append(r["terminal_step"] - c)
        mfs = f"{median(fire_steps):.0f}" if fire_steps else "—"
        mrs = f"{median(remain):.1f}" if remain else "—"
        too_late = f"{sum(1 for x in remain if x <= 1)}/{len(remain)}" if remain else "—"
        print(f"{t:>3} {len(fe):>11} {len(fp):>11} {prec:>10} "
              f"{mfs:>14} {mrs:>17} {too_late:>14}")

    print("\n=== RECOVERABLE REGIME (empties reaching no_write>=5) ===")
    recov = [conv_rising_after_stall(r) for r in empties]
    reached = [x for x in recov if x is not None]
    if reached:
        n_recov = sum(1 for x in reached if x)
        print(f"  empties reaching stall depth 5: {len(reached)}/{len(empties)}")
        print(f"  of those, convergence still RISING after stall (recoverable): "
              f"{n_recov}/{len(reached)}")
        print(f"  monotonic stall (write_pressure can only perturb): "
              f"{len(reached)-n_recov}/{len(reached)}")
    else:
        print(f"  0 empties reached stall depth 5 — no_write may fire too rarely "
              f"on the target class; see score_trend separability below.")

    # score_trend separability uses runs where score_trend was live.
    st_runs = [r for r in runs if r["score_trend_live"]]
    print(f"\n=== score_trend SEPARABILITY BY OUTCOME (n={len(st_runs)} trend-live) ===")
    print(f"{'outcome':12} {'n':>4} {'med_terminal':>13} {'med_min':>9} "
          f"{'med_slope':>10} {'med_signflips':>14}")
    for out in ("empty_patch", "patched"):
        g = [r for r in st_runs if r["outcome"] == out]
        if g:
            print(f"  {out:12} {len(g):>4} "
                  f"{median(r['trend_terminal'] for r in g):>13.3f} "
                  f"{median(r['trend_min'] for r in g):>9.3f} "
                  f"{median(r['trend_slope'] for r in g):>10.3f} "
                  f"{median(r['trend_sign_flips'] for r in g):>14.1f}")

    print("\n=== PER-INSTANCE REP CONSISTENCY (rep variance, not independent runs) ===")
    by_inst = defaultdict(list)
    for r in runs:
        by_inst[r["instance"]].append(r)
    for inst in sorted(by_inst, key=lambda k: (-len(by_inst[k]), k)):
        rs = by_inst[inst]
        outs = [r["outcome"] for r in rs]
        ne = outs.count("empty_patch")
        nws = [r["max_no_write"] for r in rs if r["no_write_live"]]
        nw_str = f"max_nw={min(nws)}..{max(nws)}" if nws else "max_nw=n/a"
        flag = " <-- variance" if 0 < ne < len(outs) else ""
        print(f"  {inst:38} reps={len(rs)} empty={ne}/{len(outs)} {nw_str}{flag}")

    print(f"\nmanifest + report basis written to {OUT_DIR}/run_id_manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
