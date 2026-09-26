#!/usr/bin/env python
"""C13 — action-density gate recalibration: pre/post-intervention split.

Offline analysis of post-v1.10.2 traces (the v1.10 backlog item: "post-
intervention trajectories are NOT IID relative to pre-intervention — the
intervention itself alters action cadence; split the gate into
pre_intervention_density_gate and post_intervention_density_gate with
separately calibrated decay windows / minimum action counts").

Mines every ~/.luxe/runs/<id>/events.jsonl in the window with
`action_density_sample` events (loop.py telemetry incl. the v1.10 habituation
fields: since_intervention_step/kind, time_to_first_write_after_intervention,
write_burst_persistence, convergence_score). The taxonomy JSONs from the May
cycles are no longer on this host, so the outcome label is the
mechanism-relevant proxy the gate itself targets: WROTE (>=1 write by run end)
vs NO-WRITE (the stall class the rescue gate exists for).

Outputs acceptance/c13_density_split/{c13_data.json,C13_REPORT.md}.

HARD CAVEAT (precommitted): this is analysis ONLY — no threshold promotion
without a 5-fixture smoke + 3-rep confirm (CLAUDE.md Phase C ground rules).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

RUNS = Path.home() / ".luxe" / "runs"
OUT = Path("acceptance/c13_density_split")
WINDOW_START = time.mktime(time.strptime("2026-05-16", "%Y-%m-%d"))
WINDOW_END = time.mktime(time.strptime("2026-06-02", "%Y-%m-%d"))
# v1.9 locked thresholds (THRESHOLD_DECISION.md): step>=6 tok>=1500 tools<=10 bail+2
V19 = {"min_step": 6, "min_tokens": 1500, "max_tools": 10,
       "min_turns_after_bail": 2}


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {}
    vs = sorted(vals)
    n = len(vs)
    return {"n": n, "min": vs[0], "max": vs[-1], "p25": vs[n // 4],
            "p50": vs[n // 2], "p75": vs[3 * n // 4],
            "p90": vs[min(n - 1, int(0.9 * n))],
            "mean": round(sum(vs) / n, 3)}


def load_run(run_dir: Path) -> dict | None:
    f = run_dir / "events.jsonl"
    if not f.is_file():
        return None
    samples, first_intervention, first_write = [], None, None
    interv_kinds: Counter = Counter()
    for ln in f.read_text().splitlines():
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        k = ev.get("kind", "")
        if k == "action_density_sample" and ev.get("phase") == "main":
            samples.append(ev)
        elif k.endswith("_fired") and k != "vacuous_test_fired":
            interv_kinds[k] += 1
            if first_intervention is None:
                first_intervention = ev.get("step")
        elif k == "tool_call" and ev.get("name") in ("write_file", "edit_file"):
            if first_write is None:
                first_write = ev.get("step")
    if not samples:
        return None
    return {"run_id": run_dir.name, "samples": samples,
            "first_intervention_step": first_intervention,
            "interventions": dict(interv_kinds),
            "first_write_step": first_write,
            "wrote": any((s.get("writes_seen") or 0) > 0 for s in samples)
            or first_write is not None}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    runs = []
    for r in sorted(RUNS.iterdir()):
        if r.name.startswith("gitkit"):
            continue                       # gitkit chat-phase runs: out of scope
        m = r.stat().st_mtime
        if not (WINDOW_START <= m < WINDOW_END):
            continue
        run = load_run(r)
        if run:
            runs.append(run)
    print(f"runs in window with main-phase density samples: {len(runs)}")
    wrote = [r for r in runs if r["wrote"]]
    nowrite = [r for r in runs if not r["wrote"]]
    fired = [r for r in runs if r["first_intervention_step"] is not None]
    print(f"  wrote={len(wrote)}  no-write={len(nowrite)}  "
          f"intervention-fired={len(fired)}")

    # --- pre vs post intervention cadence ------------------------------------
    def split_samples(run):
        fi = run["first_intervention_step"]
        pre, post = [], []
        for s in run["samples"]:
            (pre if fi is None or (s.get("step") or 0) < fi else post).append(s)
        return pre, post

    def cadence(samples):
        return {
            "completion_delta": _stats([s.get("completion_delta") or 0
                                        for s in samples]),
            "action_density": _stats([s.get("action_density") or 0.0
                                      for s in samples]),
            "tool_calls_total_at_sample": _stats([s.get("tool_calls_total") or 0
                                                  for s in samples]),
            "convergence_score": _stats([s.get("convergence_score") or 0.0
                                         for s in samples
                                         if s.get("convergence_score") is not None]),
        }

    pre_all, post_all = [], []
    for r in fired:
        pre, post = split_samples(r)
        pre_all.extend(pre)
        post_all.extend(post)

    # --- conversion latency: how long after an intervention until the first
    # write, among runs that converted (THE decay-window calibration signal:
    # the post-intervention rescue gate must NOT fire inside the typical
    # conversion window or it interrupts conversions in flight). -------------
    conv_latency = []
    for r in fired:
        fi, fw = r["first_intervention_step"], r["first_write_step"]
        if fw is not None and fi is not None and fw >= fi:
            conv_latency.append(fw - fi)
    ttfw = [s.get("time_to_first_write_after_intervention") for r in fired
            for s in r["samples"]
            if s.get("time_to_first_write_after_intervention") is not None]
    burst = [s.get("write_burst_persistence") for r in runs for s in r["samples"]
             if s.get("write_burst_persistence") is not None]

    # --- pre-intervention gate sweep: would the v1.9 thresholds fire early on
    # runs that were going to write anyway (false-positive pressure)? --------
    def pre_gate_fires_before_write(run, min_step, min_tokens, max_tools):
        fi = run["first_intervention_step"]
        fw = run["first_write_step"]
        for s in run["samples"]:
            step = s.get("step")
            if step is None or step < min_step:
                continue
            if fi is not None and step >= fi:
                break                      # pre-intervention window only
            if (s.get("writes_seen") or 0) > 0:
                break
            d = s.get("action_density") or 0
            tt = s.get("tool_calls_total") or 0
            est_tok = int(tt / d) if d > 0 else 0
            if est_tok >= min_tokens and tt <= max_tools:
                return fw is None or step < fw
        return False

    sweep = []
    for cand in ({"min_step": 6, "min_tokens": 1500, "max_tools": 10},
                 {"min_step": 6, "min_tokens": 2000, "max_tools": 10},
                 {"min_step": 8, "min_tokens": 1500, "max_tools": 10},
                 {"min_step": 8, "min_tokens": 2000, "max_tools": 12}):
        fp = sum(pre_gate_fires_before_write(r, **cand) for r in wrote)
        tp = sum(pre_gate_fires_before_write(r, **cand) for r in nowrite)
        sweep.append({**cand,
                      "fires_on_eventual_writers (false-pressure)": fp,
                      "writers_n": len(wrote),
                      "fires_on_never-writers (rescue-targets)": tp,
                      "never_writers_n": len(nowrite)})

    data = {
        "window": "2026-05-16 .. 2026-06-01 (post-v1.10.2)",
        "n_runs": len(runs), "n_wrote": len(wrote), "n_nowrite": len(nowrite),
        "n_intervention_fired": len(fired),
        "intervention_kind_totals": dict(sum(
            (Counter(r["interventions"]) for r in runs), Counter())),
        "pre_intervention_cadence": cadence(pre_all),
        "post_intervention_cadence": cadence(post_all),
        "conversion_latency_steps": _stats(conv_latency),
        "time_to_first_write_after_intervention": _stats(
            [t for t in ttfw if isinstance(t, (int, float))]),
        "write_burst_persistence": _stats(
            [b for b in burst if isinstance(b, (int, float))]),
        "v19_locked_thresholds": V19,
        "pre_gate_sweep": sweep,
    }
    (OUT / "c13_data.json").write_text(json.dumps(data, indent=2))
    print(f"wrote {OUT/'c13_data.json'}")

    # report rendered by the caller / by hand from c13_data.json
    return 0


if __name__ == "__main__":
    sys.exit(main())
