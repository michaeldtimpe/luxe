#!/usr/bin/env python3
"""Mine v17 + v18 SWE-bench traces for action-density distribution and
convergence telemetry. Output drives the v1.9 LUXE_ACTION_DENSITY_GATE
threshold decision.

Reads:
  - acceptance/v17_taxonomy/swebench_n75.json (has run_id field)
  - acceptance/v18_taxonomy/swebench_n75.json (run_id resolved from
    ~/.luxe/swebench-workspace/<instance_id>/log/stdout.log)
  - ~/.luxe/runs/<run_id>/events.jsonl per instance

For each run, derives:
  - per-step action_density samples (already logged by loop.py)
  - tool_call sequence + key_hash repetition stats (convergence proxy)
  - final tool_calls_total, max step, est_completion_tokens
  - whether action_density_gate would have fired at any step under
    several candidate threshold configurations (ROC-style sweep)

Outputs:
  - acceptance/v19_mining/action_density_distribution.json — full data
  - acceptance/v19_mining/action_density_report.md — human-readable

Reuses _run_id_for_swebench from scripts/backfill_v17_taxonomy.py
pattern (replicated here to keep the script self-contained).
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

WORKSPACE = Path.home() / ".luxe" / "swebench-workspace"
RUNS_DIR = Path.home() / ".luxe" / "runs"
OUT_DIR = Path("acceptance/v19_mining")


def _run_id_from_workspace(instance_id: str) -> str | None:
    log = WORKSPACE / instance_id / "log" / "stdout.log"
    if not log.is_file():
        return None
    for line in log.read_text().splitlines():
        if line.startswith("luxe maintain  run_id="):
            return line.split("=", 1)[1].strip()
    return None


def _read_events(run_id: str) -> list[dict]:
    p = RUNS_DIR / run_id / "events.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def summarize_run(events: list[dict]) -> dict:
    """Extract per-step density samples + tool sequence + convergence
    proxies + intervention timing from one run's events.jsonl."""
    density_samples = []  # one per step: {step, completion_tokens, tools, density, writes}
    tool_calls = []       # one per tool_call: {step, name, key_hash, duplicate}
    interventions = {}    # kind -> first-fire step
    for e in events:
        k = e.get("kind", "")
        if k == "action_density_sample":
            d = e.get("action_density") or 0.0
            tt = e.get("tool_calls_total") or 0
            est_completion = int(tt / max(d, 1e-9)) if d > 0 else 0
            density_samples.append({
                "step": e.get("step"),
                "completion_delta": e.get("completion_delta"),
                "completion_tokens": est_completion,
                "tool_calls_total": tt,
                "action_density": d,
                "writes_seen": e.get("writes_seen") or 0,
            })
        elif k == "tool_call":
            tool_calls.append({
                "step": e.get("step"),
                "name": e.get("name"),
                "key_hash": e.get("key_hash"),
                "duplicate": e.get("duplicate", False),
            })
        elif k in ("early_bail_fired", "write_pressure_fired", "prose_burst_fired"):
            interventions.setdefault(k.replace("_fired", ""), e.get("step"))

    # Convergence telemetry. key_hash is deterministic for (name, args), so
    # repeated key_hashes indicate the model is revisiting the same target.
    read_hashes = [t["key_hash"] for t in tool_calls if t["name"] == "read_file"]
    grep_hashes = [t["key_hash"] for t in tool_calls if t["name"] in ("grep", "bm25_search")]
    all_path_hashes = [t["key_hash"] for t in tool_calls
                       if t["name"] in ("read_file", "edit_file", "write_file", "grep", "bm25_search")]

    read_counter = Counter(read_hashes)
    unique_read_keys = len(read_counter)
    same_file_read_twice = any(c >= 2 for c in read_counter.values())
    # First step at which a read was repeated
    seen_read = set()
    same_file_read_twice_step = None
    for t in tool_calls:
        if t["name"] != "read_file":
            continue
        if t["key_hash"] in seen_read:
            same_file_read_twice_step = t["step"]
            break
        seen_read.add(t["key_hash"])

    reread_ratio = (
        (len(read_hashes) - unique_read_keys) / len(read_hashes)
        if read_hashes else 0.0
    )

    # First write step (None if no writes)
    first_write_step = None
    for t in tool_calls:
        if t["name"] in ("write_file", "edit_file"):
            first_write_step = t["step"]
            break

    return {
        "density_samples": density_samples,
        "interventions": interventions,
        "tool_calls_total": len(tool_calls),
        "max_step": max((t["step"] for t in tool_calls), default=-1),
        "first_write_step": first_write_step,
        "unique_read_keys": unique_read_keys,
        "total_read_calls": len(read_hashes),
        "reread_ratio": reread_ratio,
        "same_file_read_twice": same_file_read_twice,
        "same_file_read_twice_step": same_file_read_twice_step,
        "unique_grep_keys": len(set(grep_hashes)),
        "unique_path_keys": len(set(all_path_hashes)),
    }


def gate_would_fire(samples: list[dict], min_step: int, min_tokens: int,
                    max_tools: int, min_turns_after_bail: int,
                    early_bail_step: int | None,
                    same_read_twice_step: int | None) -> dict:
    """Simulate the v1.9 LUXE_ACTION_DENSITY_GATE predicate against this
    run's density samples. Returns the step at which the gate would fire
    (or None) and the fire_mode (standalone / post_bail_rescue). The
    convergence proxy is "same_file_read_twice at or before this step".
    """
    for s in samples:
        step = s["step"]
        if step is None or step < min_step:
            continue
        if s["writes_seen"] > 0:
            continue
        if s["completion_tokens"] < min_tokens:
            continue
        if s["tool_calls_total"] > max_tools:
            continue
        # Convergence proxy — skip gate if same file read twice ON or BEFORE step
        if same_read_twice_step is not None and same_read_twice_step <= step:
            continue
        if early_bail_step is not None:
            if step - early_bail_step < min_turns_after_bail:
                continue
            return {"step": step, "mode": "post_bail_rescue",
                    "turns_since_bail": step - early_bail_step}
        return {"step": step, "mode": "standalone", "turns_since_bail": None}
    return {"step": None, "mode": None, "turns_since_bail": None}


def load_v17_rows() -> list[dict]:
    raw = json.loads(Path("acceptance/v17_taxonomy/swebench_n75.json").read_text())
    return raw.get("rows", [])


def load_v18_rows() -> list[dict]:
    raw = json.loads(Path("acceptance/v18_taxonomy/swebench_n75.json").read_text())
    rows = raw.get("rows", [])
    # v18 backfill did not embed run_id; resolve from workspace.
    for r in rows:
        if "run_id" not in r or not r.get("run_id"):
            r["run_id"] = _run_id_from_workspace(r["instance_id"])
    return rows


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading v17 + v18 taxonomy rows...")
    v17_rows = load_v17_rows()
    v18_rows = load_v18_rows()
    print(f"  v17 rows: {len(v17_rows)}  v18 rows: {len(v18_rows)}")

    # Bucket per-run summaries by (cycle, tier)
    by_cycle = {"v17": v17_rows, "v18": v18_rows}
    all_runs = []  # flat list with cycle+instance+tier+summary

    for cycle, rows in by_cycle.items():
        for r in rows:
            inst = r["instance_id"]
            tier = r["tier"]
            run_id = r.get("run_id")
            if not run_id:
                continue
            events = _read_events(run_id)
            if not events:
                continue
            s = summarize_run(events)
            all_runs.append({
                "cycle": cycle, "instance_id": inst, "tier": tier,
                "run_id": run_id, **s,
            })

    print(f"Total runs analyzed: {len(all_runs)}")

    # Candidate gate threshold configurations to sweep
    candidates = [
        {"min_step": 5, "min_tokens": 1000, "max_tools": 8, "min_turns_after_bail": 2},
        {"min_step": 6, "min_tokens": 1500, "max_tools": 10, "min_turns_after_bail": 2},
        {"min_step": 6, "min_tokens": 2000, "max_tools": 12, "min_turns_after_bail": 2},
        {"min_step": 7, "min_tokens": 1500, "max_tools": 10, "min_turns_after_bail": 2},
        {"min_step": 6, "min_tokens": 1500, "max_tools": 8,  "min_turns_after_bail": 2},
    ]

    # For each candidate, compute (would-fire | tier | cycle) cross-tab
    sweep_results = []
    for cand in candidates:
        rows = []
        for run in all_runs:
            eb_step = run["interventions"].get("early_bail")
            fire = gate_would_fire(
                run["density_samples"], cand["min_step"], cand["min_tokens"],
                cand["max_tools"], cand["min_turns_after_bail"],
                eb_step, run["same_file_read_twice_step"],
            )
            rows.append({
                "cycle": run["cycle"], "instance_id": run["instance_id"],
                "tier": run["tier"], "fire_step": fire["step"],
                "fire_mode": fire["mode"], "first_write_step": run["first_write_step"],
            })
        # Bucket: rescue_targets (would-fire AND tier=empty AND no_pre_bail),
        # careful_strong_at_risk (would-fire AND tier=strong AND fire_step < first_write_step),
        # benign_no_fire on strongs (would NOT fire AND tier=strong)
        rescue_emp = [r for r in rows if r["fire_step"] is not None and r["tier"] == "empty_patch"]
        risk_strong = [r for r in rows if r["fire_step"] is not None and r["tier"] == "strong"
                       and r["first_write_step"] is not None
                       and r["fire_step"] < r["first_write_step"]]
        safe_strong = [r for r in rows if r["fire_step"] is None and r["tier"] == "strong"]
        sweep_results.append({
            "candidate": cand,
            "rescue_target_count": len(rescue_emp),
            "rescue_target_instances": [r["instance_id"] for r in rescue_emp],
            "careful_strong_at_risk_count": len(risk_strong),
            "careful_strong_at_risk_instances": [r["instance_id"] for r in risk_strong],
            "safe_strong_count": len(safe_strong),
            "rows": rows,
        })

    # Distribution: per-step action_density stats by terminal tier.
    # For each tier, collect each run's max-step density sample.
    distribution_by_tier = defaultdict(lambda: defaultdict(list))
    for run in all_runs:
        last = run["density_samples"][-1] if run["density_samples"] else None
        if last is None:
            continue
        key = (run["cycle"], run["tier"])
        distribution_by_tier[key]["completion_tokens"].append(last["completion_tokens"])
        distribution_by_tier[key]["tool_calls_total"].append(last["tool_calls_total"])
        distribution_by_tier[key]["action_density"].append(last["action_density"])
        distribution_by_tier[key]["first_write_step"].append(
            run["first_write_step"] if run["first_write_step"] is not None else -1)
        distribution_by_tier[key]["unique_read_keys"].append(run["unique_read_keys"])
        distribution_by_tier[key]["reread_ratio"].append(run["reread_ratio"])
        distribution_by_tier[key]["same_file_read_twice"].append(
            1 if run["same_file_read_twice"] else 0)

    def _stats(vals: list[float]) -> dict:
        if not vals:
            return {}
        vs = sorted(vals)
        n = len(vs)
        return {
            "n": n, "min": vs[0], "max": vs[-1],
            "p50": vs[n // 2],
            "p25": vs[n // 4],
            "p75": vs[3 * n // 4],
            "mean": sum(vs) / n,
        }

    distribution = {}
    for (cycle, tier), cols in distribution_by_tier.items():
        distribution[f"{cycle}/{tier}"] = {
            field: _stats(vals) for field, vals in cols.items()
        }

    out_data = {
        "distribution_by_cycle_tier": distribution,
        "candidate_sweep": sweep_results,
        "run_count": len(all_runs),
    }
    (OUT_DIR / "action_density_distribution.json").write_text(
        json.dumps(out_data, indent=2))
    print(f"Wrote {OUT_DIR / 'action_density_distribution.json'}")

    # Markdown report
    lines = ["# v1.9 action-density mining — distribution + threshold sweep", ""]
    lines.append(f"Total runs analyzed: {len(all_runs)} (v17 + v18 SWE-bench n=75)")
    lines.append("")
    lines.append("## Final-step distribution by (cycle, tier)")
    lines.append("")
    lines.append(
        "| cycle/tier | n | completion_tokens p25/p50/p75 | tool_calls p25/p50/p75 | "
        "action_density p25/p50/p75 | first_write_step p25/p50/p75 |")
    lines.append("|---|---|---|---|---|---|")
    for key in sorted(distribution.keys()):
        d = distribution[key]
        ct = d.get("completion_tokens", {})
        tc = d.get("tool_calls_total", {})
        ad = d.get("action_density", {})
        fws = d.get("first_write_step", {})
        if not ct:
            continue
        lines.append(
            f"| {key} | {ct.get('n',0)} | "
            f"{ct.get('p25',0)}/{ct.get('p50',0)}/{ct.get('p75',0)} | "
            f"{tc.get('p25',0)}/{tc.get('p50',0)}/{tc.get('p75',0)} | "
            f"{ad.get('p25',0):.4f}/{ad.get('p50',0):.4f}/{ad.get('p75',0):.4f} | "
            f"{fws.get('p25',0)}/{fws.get('p50',0)}/{fws.get('p75',0)} |"
        )
    lines.append("")
    lines.append("## Threshold candidate sweep")
    lines.append("")
    lines.append("| candidate | rescue_targets (empty) | careful_strongs_at_risk | safe_strongs |")
    lines.append("|---|---|---|---|")
    for sr in sweep_results:
        c = sr["candidate"]
        label = (f"step≥{c['min_step']} tok≥{c['min_tokens']} "
                 f"tools≤{c['max_tools']} bail+{c['min_turns_after_bail']}")
        lines.append(
            f"| {label} | {sr['rescue_target_count']} "
            f"({', '.join(sr['rescue_target_instances'])}) | "
            f"{sr['careful_strong_at_risk_count']} "
            f"({', '.join(sr['careful_strong_at_risk_instances'])}) | "
            f"{sr['safe_strong_count']} |"
        )
    lines.append("")
    lines.append("## Convergence telemetry — reread_ratio + same_file_read_twice")
    lines.append("")
    lines.append("| cycle/tier | n | reread_ratio mean | same_file_read_twice rate |")
    lines.append("|---|---|---|---|")
    for key in sorted(distribution.keys()):
        d = distribution[key]
        rr = d.get("reread_ratio", {})
        srt = d.get("same_file_read_twice", {})
        if not rr:
            continue
        lines.append(
            f"| {key} | {rr.get('n',0)} | {rr.get('mean',0):.3f} | "
            f"{srt.get('mean',0):.3f} |"
        )
    (OUT_DIR / "action_density_report.md").write_text("\n".join(lines) + "\n")
    print(f"Wrote {OUT_DIR / 'action_density_report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
