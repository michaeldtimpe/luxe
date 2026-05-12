#!/usr/bin/env python3
"""Secondary-gate inspector for Phase B.4 v17 early-bail smoke.

Reads predictions.json from the smoke output dir, classifies each of the
18 instances via smoke_inspect's gold-comparison logic, and reports the
secondary gates that the conversion-count alone can't surface:

  - First-write step distribution for converts (from events.jsonl)
  - Median modified files per converted run (from diff path count)
  - Strong / plausible / wrong_target tier ratio of converts
  - early_bail_fired event distribution (did the intervention land?)

Usage:
    .venv/bin/python scripts/inspect_v17_smoke.py \\
        --smoke acceptance/swebench/v17_early_bail_smoke_n18/rep_1/
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from benchmarks.swebench.smoke_inspect import (
    _diff_paths,
    compare_predictions_to_gold,
)


WORKSPACE = Path.home() / ".luxe" / "swebench-workspace"
RUNS_DIR = Path.home() / ".luxe" / "runs"

V3_EMPTY_INSTANCE_IDS = [
    "astropy__astropy-13453", "astropy__astropy-13977", "astropy__astropy-14096",
    "astropy__astropy-8707", "django__django-11734",
    "matplotlib__matplotlib-13989", "matplotlib__matplotlib-20488",
    "matplotlib__matplotlib-20826", "matplotlib__matplotlib-24870",
    "mwaskom__seaborn-3069", "mwaskom__seaborn-3187",
    "psf__requests-6028", "pydata__xarray-2905", "pydata__xarray-6938",
    "pylint-dev__pylint-4604", "sphinx-doc__sphinx-10323",
    "sphinx-doc__sphinx-10435", "sphinx-doc__sphinx-10614",
]


def _run_id_for(instance_id: str) -> str | None:
    log = WORKSPACE / instance_id / "log" / "stdout.log"
    if not log.is_file():
        return None
    for line in log.read_text().splitlines():
        if line.startswith("luxe maintain  run_id="):
            return line.split("=", 1)[1].strip()
    return None


def _first_write_step(run_id: str) -> int | None:
    events_path = RUNS_DIR / run_id / "events.jsonl"
    if not events_path.is_file():
        return None
    write_tools = {"write_file", "edit_file"}
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        evt = json.loads(line)
        if evt.get("kind") != "tool_call":
            continue
        if evt.get("phase") != "main":
            continue
        if evt.get("name", "").strip() in write_tools:
            return evt.get("step")
    return None


def _early_bail_fired(run_id: str) -> tuple[bool, int | None]:
    events_path = RUNS_DIR / run_id / "events.jsonl"
    if not events_path.is_file():
        return False, None
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        evt = json.loads(line)
        if evt.get("kind") == "early_bail_fired":
            return True, evt.get("step")
    return False, None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", type=Path, required=True,
                        help="Smoke output dir (with per-instance JSON files).")
    parser.add_argument("--gold-source", type=Path,
                        default=Path("benchmarks/swebench/subsets/raw/verified.jsonl"),
                        help="SWE-bench Verified raw JSONL with gold patches.")
    args = parser.parse_args()

    # 1. Build a predictions.json out of the per-instance shape used by run.py
    rows = []
    for instance_id in V3_EMPTY_INSTANCE_IDS:
        p = args.smoke / f"{instance_id}.json"
        if not p.is_file():
            print(f"  WARN: missing {p}")
            continue
        d = json.loads(p.read_text())
        rows.append({
            "instance_id": instance_id,
            "model_name_or_path": "luxe-v17",
            "model_patch": d.get("model_patch", ""),
        })

    if not rows:
        print("No instance JSON files found.")
        return 1

    preds_path = args.smoke / "predictions.json"
    preds_path.write_text(json.dumps(rows, indent=2))

    # 2. Gold-comparison verdicts (strong/plausible/wrong_*/empty_patch tiers)
    verdicts = compare_predictions_to_gold(preds_path, args.gold_source)
    verdict_map = {v.instance_id: v for v in verdicts}

    # 3. Secondary-gate signals — first_write_step, files touched, early_bail event
    print(f"\n{'instance':<35} {'tier':<14} {'eb_fired':<9} {'eb_step':<8} {'fwr_step':<9} {'files':<6}")
    converts = []
    for instance_id in V3_EMPTY_INSTANCE_IDS:
        v = verdict_map.get(instance_id)
        tier = v.tier if v else "(missing)"
        run_id = _run_id_for(instance_id)
        eb_fired, eb_step = (False, None)
        fwr = None
        files = 0
        if run_id:
            eb_fired, eb_step = _early_bail_fired(run_id)
            fwr = _first_write_step(run_id)
        row = next((r for r in rows if r["instance_id"] == instance_id), {})
        files = len(_diff_paths(row.get("model_patch", "")))
        print(f"{instance_id:<35} {tier:<14} "
              f"{('Y' if eb_fired else 'N'):<9} "
              f"{str(eb_step or '-'):<8} "
              f"{str(fwr if fwr is not None else '-'):<9} "
              f"{files:<6}")
        if tier in {"strong", "plausible", "wrong_target", "wrong_location"}:
            converts.append({
                "instance_id": instance_id,
                "tier": tier,
                "first_write_step": fwr,
                "files_touched": files,
                "early_bail_fired": eb_fired,
                "early_bail_step": eb_step,
            })

    # 4. Aggregate gates
    print()
    print(f"=== Phase B.4 smoke gates (n={len(rows)}/18) ===")
    print(f"  CONVERSION (PRIMARY, floor >=10):")
    tier_counts = {}
    for v in verdicts:
        tier_counts[v.tier] = tier_counts.get(v.tier, 0) + 1
    for t in sorted(tier_counts, key=lambda x: -tier_counts[x]):
        print(f"    {t:<20} {tier_counts[t]}")
    non_empty = sum(1 for v in verdicts
                    if v.tier in {"strong", "plausible", "wrong_target", "wrong_location"})
    print(f"  -> converted to non-empty: {non_empty}/{len(verdicts)}  "
          f"({'PASS' if non_empty >= 10 else 'FAIL'})")

    eb_fire_count = sum(1 for c in converts if c["early_bail_fired"])
    print(f"\n  EARLY_BAIL EVENT (intervention reach):")
    print(f"    fired in {sum(1 for i in V3_EMPTY_INSTANCE_IDS if (lambda r: _early_bail_fired(r)[0] if r else False)(_run_id_for(i)))}/18 total runs")
    print(f"    fired in {eb_fire_count}/{len(converts)} converts (intervention attributable)")

    if converts:
        fwrs = [c["first_write_step"] for c in converts if c["first_write_step"] is not None]
        if fwrs:
            print(f"\n  FIRST-WRITE STEP (converts, sweet spot 4-6):")
            print(f"    median={statistics.median(fwrs)}  range={min(fwrs)}-{max(fwrs)}")
            late = sum(1 for s in fwrs if s >= 7)
            print(f"    fired late (step >=7): {late}/{len(fwrs)}")
        files_lst = [c["files_touched"] for c in converts]
        print(f"\n  FILES TOUCHED PER CONVERT (sane: 1-2):")
        print(f"    median={statistics.median(files_lst)}  range={min(files_lst)}-{max(files_lst)}")
        shotgun = sum(1 for f in files_lst if f >= 4)
        print(f"    shotgun (>=4 files): {shotgun}/{len(files_lst)}")
        wrong = sum(1 for c in converts if c["tier"] == "wrong_target")
        wrong_frac = wrong / len(converts) if converts else 0
        print(f"\n  QUALITY (converts -> wrong_target ratio, floor <80%):")
        print(f"    wrong_target: {wrong}/{len(converts)} = {wrong_frac:.0%}  "
              f"({'PASS' if wrong_frac < 0.80 else 'FAIL'})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
