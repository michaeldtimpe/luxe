#!/usr/bin/env python3
"""Render one luxe run as a readable trajectory. Read-only, stdlib only.

Harvested from harness2's trajectory grammar (acceptance/harvest_2026_10):
one fixed vocabulary, clipping per item with an explicit marker, never a
clipped whole. Built for hand-verification of bench passes.

    [STEP n] phase              a new agent step
    [ACTION] tool(path)         a tool call, with dup/cached flags and bytes out
    [USAGE]                     prompt/cached/completion tokens, reasoning chars
    [COMPACT pN]                a compaction phase firing
    [INTERVENTION kind]         a loop intervention (nudge, retry, bail, gate)
    [REJECT]                    a tool call the loop refused
    [RUN kind]                  a run-level event (preflight, diff, lint, done…)
    [UNKNOWN kind]              an event kind this renderer does not know

Then the final report (synthesizer.md) and the graded diff, each clipped
separately. Telemetry kinds that fire every step are omitted from the trace and
counted in the footer, so their absence is stated rather than silent.

What it CANNOT show: bench runs do not persist message text, so tool
observations and the model's prose between calls are absent. That is a fact
about the records, not something to infer around.

Usage:
    python scripts/render_trajectory.py <run_id | run dir> [--diff PATCH]
    python scripts/render_trajectory.py --cell <bench fixture dir>
        (reads luxe_run_id from state.json and uses its diff.patch)
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

TRUNC = "…[+{n} chars — render clip]"
REPORT_CLIP = 20_000
DIFF_CLIP = 40_000
FIELD_CLIP = 300

# Omitted from the trace (per-step telemetry); counted in the footer.
TELEMETRY = frozenset({
    "action_density_sample", "tool_step_done", "adaptive_state",
    "read_after_compact", "grep_after_compact", "edit_after_compact",
    "compaction_phase_at_first_write", "read_budget_applied",
})

INTERVENTIONS = frozenset({
    "write_pressure_fired", "early_bail_fired", "early_bail_breadth_probe_fired",
    "early_bail_suppressed_diffuse", "early_bail_suppressed_commit_only",
    "early_bail_suppressed_trajectory_shape", "action_density_gate_fired",
    "action_density_gate_suppressed_converged", "habituation_exit",
    "post_write_idle_exit", "truncated_turn_retry", "empty_turn_retry",
    "terminal_turn_truncated", "reread_note", "repeat_read_stub",
    "textfallback_drop",
})

RUN_LEVEL = frozenset({
    "preflight_ok", "single_mode_done", "diff_stat", "citation_lint_passed",
    "citation_lint_blocked", "cleanup_turn", "step_limit_incomplete",
    "step_limit_finish_begin", "step_limit_finish_done", "pr_step_begin",
    "pr_step_end", "pr_blocked", "pr_unexpected_error", "compaction_phase_at_resolve",
    "spec_validation", "reprompt_fired", "diff_stat_failed",
    "secret_redacted", "secret_egress_refused",
})

_DROP_KEYS = {"kind", "ts", "run_id", "phase", "step"}


def clip(value, n: int) -> str:
    s = "" if value is None else str(value)
    return s if len(s) <= n else s[:n] + TRUNC.format(n=len(s) - n)


def kv(event: dict) -> str:
    return " ".join(f"{k}={clip(v, FIELD_CLIP)}" for k, v in event.items()
                    if k not in _DROP_KEYS)


def render_event(e: dict) -> str | None:
    k = e.get("kind", "?")
    if k in TELEMETRY:
        return None
    if k == "tool_call":
        flags = "".join(f" [{f}]" for f in ("duplicate", "cached") if e.get(f))
        target = e.get("path") or ""
        return f"  [ACTION] {e.get('name', '?')}({clip(target, FIELD_CLIP)}){flags} -> {e.get('bytes_out', '?')} B"
    if k == "step_usage":
        return (f"  [USAGE] prompt={e.get('prompt_tokens')} cached={e.get('cached_tokens')} "
                f"completion={e.get('completion_tokens')} reasoning_chars={e.get('reasoning_chars')}")
    if k == "compaction_phase_reached":
        return (f"  [COMPACT p{e.get('phase_reached')}] tokens {e.get('tokens_before')}->"
                f"{e.get('tokens_after')} dropped={e.get('tool_results_dropped')} "
                f"effective={e.get('effective')}")
    if k == "tool_reject":
        return f"  [REJECT] {e.get('name')} reason={e.get('reason')} {clip(e.get('message'), FIELD_CLIP)}"
    if k in INTERVENTIONS:
        return f"  [INTERVENTION {k}] {kv(e)}"
    if k in RUN_LEVEL:
        return f"[RUN {k}] {kv(e)}"
    return f"[UNKNOWN {k}] {kv(e)}"


def render(run_dir: Path, diff_path: Path | None = None) -> str:
    out: list[str] = []
    meta_p = run_dir / "run.json"
    meta = json.loads(meta_p.read_text()) if meta_p.is_file() else {}
    out.append(f"# Trajectory {meta.get('run_id', run_dir.name)}")
    for key in ("task_type", "base_sha", "repo_path"):
        if meta.get(key):
            out.append(f"{key}: {meta[key]}")
    if meta.get("goal"):
        out.append(f"goal: {clip(meta['goal'], 2000)}")
    out.append("")

    counts: collections.Counter = collections.Counter()
    omitted: collections.Counter = collections.Counter()
    unknown: collections.Counter = collections.Counter()
    bad_lines = 0
    cur: tuple | None = None
    ev_p = run_dir / "events.jsonl"
    lines = ev_p.read_text(errors="replace").splitlines() if ev_p.is_file() else []
    for raw in lines:
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        if not isinstance(e, dict):
            bad_lines += 1
            continue
        k = e.get("kind", "?")
        counts[k] += 1
        if "step" in e and e.get("phase") and k not in RUN_LEVEL and k not in TELEMETRY:
            key = (e.get("phase"), e.get("step"))
            if key != cur:
                cur = key
                out.append(f"[STEP {e.get('step')}] {e.get('phase')}")
        line = render_event(e)
        if line is None:
            omitted[k] += 1
            continue
        if line.startswith("[UNKNOWN"):
            unknown[k] += 1
        out.append(line)
    if not lines:
        out.append("(no events.jsonl)")

    out += ["", "## Final report (synthesizer.md)", ""]
    rep = run_dir / "synthesizer.md"
    out.append(clip(rep.read_text(errors="replace"), REPORT_CLIP) if rep.is_file()
               else "(no synthesizer.md)")

    if diff_path is not None:
        out += ["", f"## Diff ({diff_path.name})", ""]
        out.append(clip(diff_path.read_text(errors="replace"), DIFF_CLIP)
                   if diff_path.is_file() else f"(no diff at {diff_path})")

    out += ["", "## Footer", "",
            f"events: {sum(counts.values())} across {len(counts)} kinds; "
            f"tool calls: {counts.get('tool_call', 0)}"]
    if omitted:
        out.append("omitted telemetry: " + ", ".join(f"{k}×{n}" for k, n in sorted(omitted.items())))
    if unknown:
        out.append("UNKNOWN kinds (renderer needs updating): "
                   + ", ".join(f"{k}×{n}" for k, n in sorted(unknown.items())))
    if bad_lines:
        out.append(f"unparseable event lines: {bad_lines}")
    out.append("not recorded by luxe: tool observations and model prose between calls")
    return "\n".join(out) + "\n"


def _runs_root() -> Path:
    """`~/.luxe/runs`, as luxe.paths.luxe_home resolves it (stdlib-only here)."""
    return Path.home() / ".luxe" / "runs"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("run", nargs="?", help="run id, or a run directory")
    g.add_argument("--cell", help="a bench fixture dir holding state.json + diff.patch")
    ap.add_argument("--diff", help="a patch to append")
    args = ap.parse_args(argv)

    diff = Path(args.diff) if args.diff else None
    if args.cell:
        cell = Path(args.cell)
        st = json.loads((cell / "state.json").read_text())
        run_id = st.get("luxe_run_id", "")
        if not run_id:
            print(f"{cell}/state.json has no luxe_run_id", file=sys.stderr)
            return 2
        run_dir = _runs_root() / run_id
        diff = diff or cell / "diff.patch"
    else:
        p = Path(args.run).expanduser()
        run_dir = p if p.is_dir() else _runs_root() / args.run
    if not run_dir.is_dir():
        print(f"no run directory at {run_dir}", file=sys.stderr)
        return 2
    sys.stdout.write(render(run_dir, diff))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
