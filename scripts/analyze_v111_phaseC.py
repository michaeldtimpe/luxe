#!/usr/bin/env python3
"""scripts/analyze_v111_phaseC.py — v1.11 Phase C activation-probe gate.

Evaluates the three Phase C gates for the score_trend->soft_anchor lever:

  (a) LEVER FIRES: soft_anchor_collapse_promote_fired appears on >=1 empty
      instance (seaborn-3069 / sympy-13031 / sphinx-10435).
  (b) NO ARCHETYPE REGRESSION: none of the 6 archetypes (all non-empty at
      v1.10.5 baseline) goes empty 3/3 under the activated lever.
  (c) CONVERSIONS (informational): empties that became non-empty.

Phase C runs WITHOUT the Docker harness (cheap probe), so the outcome axis
is empty_patch vs patched (patch_present) — full 5-tier waits for Phase D.

Joins ~/.luxe/runs/<run_id>/events.jsonl (promote events + started_at +
instance from run.json) to the acceptance result JSONs (patch_present).
"""
from __future__ import annotations

import glob
import json
import re
from collections import defaultdict
from pathlib import Path

RUNS_DIR = Path("~/.luxe/runs").expanduser()
PHASEC_GLOB = "acceptance/swebench/v111_phaseC_n8/rep_*/*.json"
# Phase C runs start after the Phase B commit; filter the run corpus to today.
CUTOFF_TS = 1779339600.0  # 2026-05-21 ~midday; excludes the inert Phase 3a/4 runs
_INSTANCE_RE = re.compile(r"swebench-workspace/(.+?)/repo")

ARCHETYPES = {
    "sphinx-doc__sphinx-10435", "matplotlib__matplotlib-14623",
    "psf__requests-5414", "psf__requests-1921",
    "sphinx-doc__sphinx-10323", "sympy__sympy-12419",
}
EMPTIES = {"mwaskom__seaborn-3069", "sympy__sympy-13031",
           "sphinx-doc__sphinx-10435"}  # 10435 is both archetype + conversion target


def outcome_index() -> dict[str, list[bool]]:
    """instance_id -> [patch_present per Phase C rep]."""
    idx: dict[str, list[bool]] = defaultdict(list)
    for f in glob.glob(PHASEC_GLOB):
        if f.endswith(("summary.json", "predictions.json")):
            continue
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        if "patch_present" in d and "instance_id" in d:
            idx[d["instance_id"]].append(bool(d["patch_present"]))
    return idx


def promote_events() -> dict[str, int]:
    """instance_id -> count of runs (in the Phase C window) that emitted
    soft_anchor_collapse_promote_fired."""
    fired: dict[str, int] = defaultdict(int)
    for ev in glob.glob(str(RUNS_DIR / "*" / "events.jsonl")):
        rd = Path(ev).parent
        try:
            meta = json.loads((rd / "run.json").read_text())
        except Exception:
            continue
        if meta.get("started_at", 0) < CUTOFF_TS:
            continue
        m = _INSTANCE_RE.search(meta.get("repo_path", ""))
        inst = m.group(1) if m else "unknown"
        with open(ev) as fh:
            if any("soft_anchor_collapse_promote_fired" in line for line in fh):
                fired[inst] += 1
    return fired


def main() -> int:
    idx = outcome_index()
    fired = promote_events()
    if not idx:
        print("No Phase C results found yet.")
        return 1

    print("\n=== v1.11 PHASE C — ACTIVATION PROBE GATES ===\n")

    print("Per-instance (patch_present per rep | promote-fired runs):")
    for inst in sorted(idx):
        outs = idx[inst]
        empt = sum(1 for p in outs if not p)
        tag = "ARCH" if inst in ARCHETYPES else "EMPTY"
        print(f"  [{tag:5}] {inst:36} present={['T' if p else 'F' for p in outs]} "
              f"empty={empt}/{len(outs)} promote_runs={fired.get(inst, 0)}")

    # Gate (a): lever fires on >=1 empty.
    fired_on_empty = {i: c for i, c in fired.items() if i in EMPTIES and c > 0}
    gate_a = len(fired_on_empty) >= 1
    print(f"\nGATE (a) LEVER FIRES on >=1 empty: "
          f"{'PASS' if gate_a else 'FAIL'} — fired on {sorted(fired_on_empty)}")

    # Gate (b): no archetype goes empty 3/3.
    regressions = []
    for inst in ARCHETYPES:
        outs = idx.get(inst, [])
        if outs and all(not p for p in outs):
            regressions.append(inst)
    gate_b = not regressions
    print(f"GATE (b) NO ARCHETYPE REGRESSION (none empty all reps): "
          f"{'PASS' if gate_b else 'FAIL'}"
          + (f" — REGRESSED: {regressions}" if regressions else ""))

    # Informational: conversions (empties that produced a patch in any rep).
    conversions = [i for i in EMPTIES
                   if idx.get(i) and any(idx[i])]
    print(f"\nCONVERSIONS (empty->patched in >=1 rep): {sorted(conversions)}")

    print(f"\nOVERALL Phase C swebench gates: "
          f"{'PASS' if (gate_a and gate_b) else 'FAIL'} "
          f"(BFCL irrelevance checked separately by bfcl_anchor_check)")
    return 0 if (gate_a and gate_b) else 2


if __name__ == "__main__":
    raise SystemExit(main())
