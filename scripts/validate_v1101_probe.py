"""Validate the v1.10.1 regression probe against the W2 + W3 ship-gate
expectations.

W2 (habituation clean-exit) — sympy-13031 expectations:
  - All three commitment interventions fire (WRITE_PRESSURE, EARLY_BAIL,
    ACTION_DENSITY_GATE) over the trajectory
  - Zero post-intervention writes (consistent with the v1.10 trace shape)
  - habituation_exit event emitted at step >= 20
  - Loop terminated cleanly (not max_steps-aborted)

W3 (exploratory-support variant) — matplotlib-14623 expectations:
  - early_bail_fired event with msg_variant='exploratory'
  - convergence_score < _CONVERGENCE_LOW_THRESHOLD on the event
  - Non-empty patch produced (any tier != empty_patch)

Usage:
    python scripts/validate_v1101_probe.py [<probe_output_dir>]

Default probe_output_dir: acceptance/swebench/v1101_probe_n2/rep_1/
Reads ~/.luxe/runs/<run_id>/events.jsonl for each instance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_PROBE_DIR = Path("acceptance/swebench/v1101_probe_n2/rep_1")
RUNS = Path.home() / ".luxe" / "runs"
WORKSPACE = Path.home() / ".luxe" / "swebench-workspace"


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


def validate_w2_sympy_13031(events: list[dict], pred: dict) -> dict:
    """sympy-13031 should fire all three commitment interventions and then
    exit cleanly via habituation_exit at step >= 20."""
    intervention_kinds = {
        "early_bail_fired": "EARLY_BAIL",
        "write_pressure_fired": "WRITE_PRESSURE",
        "action_density_gate_fired": "ACTION_DENSITY_GATE",
    }
    fired: set[str] = set()
    habituation_step: int | None = None
    last_step: int = 0
    has_post_intervention_write = False
    first_intervention_step: int | None = None
    for evt in events:
        kind = evt.get("kind")
        step = evt.get("step", 0)
        if not isinstance(step, int):
            continue
        last_step = max(last_step, step)
        if kind in intervention_kinds:
            fired.add(intervention_kinds[kind])
            if first_intervention_step is None:
                first_intervention_step = step
        elif kind == "habituation_exit":
            habituation_step = step
        elif kind == "tool_call" and evt.get("phase") == "main":
            name = (evt.get("name") or "").strip()
            if name in ("write_file", "edit_file"):
                if first_intervention_step is not None and step > first_intervention_step:
                    has_post_intervention_write = True
    return {
        "instance": "sympy__sympy-13031",
        "checks": [
            ("all 3 commitment interventions fired",
             len(fired & {"EARLY_BAIL", "WRITE_PRESSURE", "ACTION_DENSITY_GATE"}) == 3,
             f"fired={sorted(fired)}"),
            ("habituation_exit event emitted",
             habituation_step is not None,
             f"habituation_step={habituation_step}"),
            ("habituation fired at step >= 20",
             habituation_step is not None and habituation_step >= 20,
             f"habituation_step={habituation_step}"),
            ("zero post-intervention writes (v1.10 trace shape preserved)",
             not has_post_intervention_write,
             f"post_intervention_write={has_post_intervention_write}"),
            ("empty_patch outcome (writes never happened)",
             not bool((pred.get("model_patch") or "").strip()),
             f"has_patch={bool((pred.get('model_patch') or '').strip())}"),
        ],
    }


def validate_w3_matplotlib_14623(events: list[dict], pred: dict) -> dict:
    """matplotlib-14623 should fire early_bail with msg_variant='exploratory'
    (because convergence_score = 0.0 for the diffuse-recon trajectory)."""
    exploratory_fires: list[dict] = []
    other_fires: list[dict] = []
    for evt in events:
        if evt.get("kind") == "early_bail_fired":
            variant = evt.get("msg_variant")
            if variant == "exploratory":
                exploratory_fires.append(evt)
            else:
                other_fires.append(evt)
    has_patch = bool((pred.get("model_patch") or "").strip())
    return {
        "instance": "matplotlib__matplotlib-14623",
        "checks": [
            ("early_bail fired with msg_variant='exploratory'",
             len(exploratory_fires) >= 1,
             f"exploratory={len(exploratory_fires)} other={len(other_fires)}"),
            ("convergence_score on exploratory fire < LOW (0.10)",
             all(f.get("convergence_score", 1.0) < 0.10 for f in exploratory_fires)
             if exploratory_fires else False,
             f"scores={[round(f.get('convergence_score', -1), 3) for f in exploratory_fires]}"),
            ("non-empty patch produced (any tier != empty_patch)",
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
    preds = json.loads(preds_path.read_text())
    preds_by_id = {p["instance_id"]: p for p in preds}

    print(f"=== v1.10.1 probe validation: {probe_dir} ===\n")

    overall_pass = True
    for inst, validator in [
        ("sympy__sympy-13031", validate_w2_sympy_13031),
        ("matplotlib__matplotlib-14623", validate_w3_matplotlib_14623),
    ]:
        run_id = run_id_for(inst)
        if not run_id:
            print(f"  ! no run_id for {inst} (workspace stdout.log missing/empty)")
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
