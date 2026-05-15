"""Validate the v1.10.2 4-instance regression probe against ship-gate
expectations.

Per-instance expectations:

  sympy-13031 (W2 founding): habituation_exit fires at step >= 20
    (same as v1.10.1). Empty patch expected.

  matplotlib-14623 (W3 founding recovery): exploratory variant fires
    at score < LOW; either model commits without escalation OR
    escalation fires + commits. Non-empty patch required.

  pylint-6528 (W3 collateral): exploratory fires (diversity >= 2);
    if model stops responding, post_exploratory_escalation fires
    soft_anchor at step ~8. Non-empty patch required (locus not).

  sphinx-10323 (W3 collateral): diversity gate falls back to
    soft_anchor at step=4 (diversity=1 < threshold=2); escalation
    must NOT fire (gates on exploratory_variant_fired). Non-empty
    patch required.

Usage:
    python scripts/validate_v1102_probe.py [<probe_output_dir>]

Default: acceptance/swebench/v1102_probe_n4/rep_1/
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_PROBE_DIR = Path("acceptance/swebench/v1102_probe_n4/rep_1")
WORKSPACE = Path.home() / ".luxe" / "swebench-workspace"
RUNS = Path.home() / ".luxe" / "runs"


def run_id_for(instance_id: str) -> str | None:
    log = WORKSPACE / instance_id / "log" / "stdout.log"
    if not log.is_file():
        return None
    for line in log.read_text().splitlines():
        if line.startswith("luxe maintain  run_id="):
            return line.split("=", 1)[1].strip()
    return None


def load_events(run_id: str) -> list[dict]:
    p = RUNS / run_id / "events.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def validate_sympy_13031(events, pred):
    """Habituation exit must fire (same as v1.10.1)."""
    habit_step = None
    for e in events:
        if e.get("kind") == "habituation_exit":
            habit_step = e.get("step")
    return {
        "instance": "sympy__sympy-13031",
        "checks": [
            ("habituation_exit event emitted",
             habit_step is not None,
             f"habituation_step={habit_step}"),
            ("habituation fired at step >= 20",
             habit_step is not None and habit_step >= 20,
             f"step={habit_step}"),
            ("empty_patch outcome (predicate is budget-saver, not rescue)",
             not bool((pred.get("model_patch") or "").strip()),
             f"has_patch={bool((pred.get('model_patch') or '').strip())}"),
        ],
    }


def validate_matplotlib_14623(events, pred):
    """W3 founding recovery must be preserved."""
    exploratory_fires = []
    escalation_fires = []
    fallback_fires = []
    for e in events:
        if e.get("kind") == "early_bail_fired":
            mv = e.get("msg_variant")
            if mv == "exploratory":
                exploratory_fires.append(e)
            elif mv == "soft_anchor_low_diversity_fallback":
                fallback_fires.append(e)
        elif e.get("kind") == "post_exploratory_escalation_fired":
            escalation_fires.append(e)
    has_patch = bool((pred.get("model_patch") or "").strip())
    return {
        "instance": "matplotlib__matplotlib-14623",
        "checks": [
            ("exploratory variant fired (not fallback)",
             len(exploratory_fires) >= 1 and len(fallback_fires) == 0,
             f"exploratory={len(exploratory_fires)} fallback={len(fallback_fires)}"),
            ("non-empty patch produced (v1.10.1 recovery preserved)",
             has_patch,
             f"has_patch={has_patch}"),
        ],
    }


def validate_pylint_6528(events, pred):
    """W3 collateral: escalation should fire if model stops post-exploratory."""
    exploratory_fires = []
    escalation_fires = []
    for e in events:
        if e.get("kind") == "early_bail_fired" and e.get("msg_variant") == "exploratory":
            exploratory_fires.append(e)
        elif e.get("kind") == "post_exploratory_escalation_fired":
            escalation_fires.append(e)
    has_patch = bool((pred.get("model_patch") or "").strip())
    return {
        "instance": "pylint-dev__pylint-6528",
        "checks": [
            ("exploratory variant fired",
             len(exploratory_fires) >= 1,
             f"exploratory={len(exploratory_fires)}"),
            ("non-empty patch produced (v1.10.1 collateral closed)",
             has_patch,
             f"has_patch={has_patch}"),
            # Escalation fire is optional — if model commits without
            # waiting for escalation, that's the best outcome.
        ],
    }


def validate_sphinx_10323(events, pred):
    """W3 collateral: diversity gate fallback to soft_anchor."""
    exploratory_fires = []
    fallback_fires = []
    escalation_fires = []
    for e in events:
        if e.get("kind") == "early_bail_fired":
            mv = e.get("msg_variant")
            if mv == "exploratory":
                exploratory_fires.append(e)
            elif mv == "soft_anchor_low_diversity_fallback":
                fallback_fires.append(e)
        elif e.get("kind") == "post_exploratory_escalation_fired":
            escalation_fires.append(e)
    has_patch = bool((pred.get("model_patch") or "").strip())
    return {
        "instance": "sphinx-doc__sphinx-10323",
        "checks": [
            ("exploratory variant did NOT fire (diversity below threshold)",
             len(exploratory_fires) == 0,
             f"exploratory={len(exploratory_fires)} fallback={len(fallback_fires)}"),
            ("escalation did NOT fire (no exploratory to escalate from)",
             len(escalation_fires) == 0,
             f"escalation={len(escalation_fires)}"),
            ("non-empty patch produced (v1.10.1 collateral closed)",
             has_patch,
             f"has_patch={has_patch}"),
        ],
    }


def main() -> int:
    probe_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PROBE_DIR
    preds_path = probe_dir / "predictions.json"
    if not preds_path.is_file():
        print(f"  ! predictions missing: {preds_path}", file=sys.stderr)
        return 1
    preds_by_id = {p["instance_id"]: p for p in json.loads(preds_path.read_text())}

    print(f"=== v1.10.2 probe validation: {probe_dir} ===\n")
    validators = [
        ("sympy__sympy-13031", validate_sympy_13031),
        ("matplotlib__matplotlib-14623", validate_matplotlib_14623),
        ("pylint-dev__pylint-6528", validate_pylint_6528),
        ("sphinx-doc__sphinx-10323", validate_sphinx_10323),
    ]
    overall_pass = True
    for inst, validator in validators:
        run_id = run_id_for(inst)
        if not run_id:
            print(f"  ! no run_id for {inst}")
            overall_pass = False
            continue
        events = load_events(run_id)
        if not events:
            print(f"  ! no events for {inst} (run_id={run_id})")
            overall_pass = False
            continue
        result = validator(events, preds_by_id.get(inst, {}))
        all_passed = all(ok for _, ok, _ in result["checks"])
        marker = "✓" if all_passed else "✗"
        print(f"{marker} {result['instance']} (run_id={run_id}, events={len(events)})")
        for label, ok, detail in result["checks"]:
            print(f"    {'✓' if ok else '✗'} {label}  [{detail}]")
        if not all_passed:
            overall_pass = False
        print()
    print("=" * 60)
    print(f"  Overall: {'✓ PASS' if overall_pass else '✗ FAIL'}")
    return 0 if overall_pass else 2


if __name__ == "__main__":
    sys.exit(main())
