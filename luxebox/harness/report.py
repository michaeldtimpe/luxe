"""Aggregate results JSONL files into readable reports.

Three modes:

  `phase_a`  → candidate × benchmark screening table.
  `phase_b`  → finalist review + write replay table.
  `phase_d`  → baseline vs optimized diff + acceptance-gate verdict.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from harness import io
from harness.registry import AcceptanceGate, load_optimization_registry, load_registry

RESULTS_ROOT = Path(__file__).resolve().parent.parent / "results"


def _bench_pass_rate(records: list[dict[str, Any]]) -> float:
    if not records:
        return 0.0
    # Prefer fractional `score` when available (e.g. LiveCodeBench partial),
    # else fall back to boolean `passed`.
    scores = [float(r.get("score") or (1.0 if r.get("passed") else 0.0)) for r in records]
    return 100.0 * mean(scores)


def _tool_call_success(records: list[dict[str, Any]]) -> float:
    rates = [
        (r.get("metrics") or {}).get("tool_call", {}).get("success_rate_pct", 0.0)
        for r in records
    ]
    rates = [r for r in rates if r > 0]
    return mean(rates) if rates else 0.0


def _throughput(records: list[dict[str, Any]]) -> tuple[float, float]:
    thr = [(r.get("metrics") or {}).get("throughput", {}) for r in records]
    ttft = [t.get("ttft_s") or 0.0 for t in thr]
    dec = [t.get("decode_tok_s") or 0.0 for t in thr]
    ttft = [v for v in ttft if v > 0]
    dec = [v for v in dec if v > 0]
    return (mean(ttft) if ttft else 0.0, mean(dec) if dec else 0.0)


def _peak_rss_gb(records: list[dict[str, Any]]) -> float:
    rss = [(r.get("metrics") or {}).get("peak_rss_bytes", 0) for r in records]
    return max(rss) / (1024**3) if rss else 0.0


def phase_a_report(output_md: Path | None = None, output_csv: Path | None = None) -> str:
    runs_root = RESULTS_ROOT / "runs" / "phase_a"
    if not runs_root.exists():
        return "No Phase A results yet.\n"

    rows: list[dict[str, Any]] = []
    for candidate_dir in sorted(runs_root.iterdir()):
        if not candidate_dir.is_dir():
            continue
        for config_dir in candidate_dir.iterdir():
            if not config_dir.is_dir():
                continue
            row = {
                "candidate": candidate_dir.name,
                "config": config_dir.name,
            }
            for bench_file in config_dir.glob("*.jsonl"):
                recs = list(io.read(bench_file))
                row[f"{bench_file.stem}_pass_pct"] = round(_bench_pass_rate(recs), 2)
                row[f"{bench_file.stem}_n"] = len(recs)
            # Roll-ups across all benchmarks.
            all_recs: list[dict[str, Any]] = []
            for bench_file in config_dir.glob("*.jsonl"):
                all_recs.extend(io.read(bench_file))
            ttft, dec = _throughput(all_recs)
            row["tool_call_success_pct"] = round(_tool_call_success(all_recs), 2)
            row["ttft_s"] = round(ttft, 3)
            row["decode_tok_s"] = round(dec, 1)
            row["peak_rss_gb"] = round(_peak_rss_gb(all_recs), 2)
            rows.append(row)

    if output_csv:
        _write_csv(output_csv, rows)

    md = _rows_to_markdown("Phase A screening", rows)
    if output_md:
        output_md.write_text(md)
    return md


def phase_b_report(output_md: Path | None = None) -> str:
    runs_root = RESULTS_ROOT / "runs" / "phase_b"
    if not runs_root.exists():
        return "No Phase B results yet.\n"

    rows: list[dict[str, Any]] = []
    for candidate_dir in sorted(runs_root.iterdir()):
        if not candidate_dir.is_dir():
            continue
        for config_dir in candidate_dir.iterdir():
            review_f1_vals: list[float] = []
            write_pass_ct = 0
            write_total = 0
            write_sim_vals: list[float] = []
            for jsonl in config_dir.glob("*.jsonl"):
                recs = list(io.read(jsonl))
                if jsonl.stem.startswith("review_"):
                    review_f1_vals.extend([r.get("f1", 0.0) for r in recs])
                elif jsonl.stem.startswith("write_"):
                    for r in recs:
                        write_total += 1
                        if r.get("tests_passed"):
                            write_pass_ct += 1
                        write_sim_vals.append(r.get("diff_similarity", 0.0))
            rows.append(
                {
                    "candidate": candidate_dir.name,
                    "config": config_dir.name,
                    "review_f1_mean": round(mean(review_f1_vals), 3) if review_f1_vals else 0.0,
                    "review_n": len(review_f1_vals),
                    "write_pass_rate_pct": (
                        round(100 * write_pass_ct / write_total, 1) if write_total else 0.0
                    ),
                    "write_diff_sim_mean": (
                        round(mean(write_sim_vals), 3) if write_sim_vals else 0.0
                    ),
                    "write_n": write_total,
                }
            )

    md = _rows_to_markdown("Phase B finalists", rows)
    if output_md:
        output_md.write_text(md)
    return md


def phase_d_report(
    *,
    winner: str,
    output_md: Path | None = None,
) -> str:
    opt_reg = load_optimization_registry()
    gate = opt_reg.acceptance_gate

    baseline_rows, config_rows, verdict_lines = _compute_phase_d(winner, gate)

    md_parts = ["# Phase D — Baseline vs Optimized\n", f"Winner: **{winner}**\n"]
    md_parts.append(_rows_to_markdown("Baseline (per benchmark)", baseline_rows))
    md_parts.append(_rows_to_markdown("Deltas vs baseline", config_rows))
    md_parts.append("\n## Acceptance gate\n")
    md_parts.extend(f"- {v}\n" for v in verdict_lines)

    md = "\n".join(md_parts)
    if output_md:
        output_md.write_text(md)
    return md


def _compute_phase_d(winner: str, gate: AcceptanceGate):
    runs_root = RESULTS_ROOT / "runs"
    phase_a_root = runs_root / "phase_a" / winner
    phase_b_root = runs_root / "phase_b" / winner

    if not phase_a_root.exists():
        return [], [], ["No Phase A/D data yet for winner."]

    # Gather per-config per-benchmark pass rates.
    per_config: dict[str, dict[str, float]] = defaultdict(dict)
    for config_dir in phase_a_root.iterdir():
        if not config_dir.is_dir():
            continue
        for jsonl in config_dir.glob("*.jsonl"):
            recs = list(io.read(jsonl))
            per_config[config_dir.name][jsonl.stem] = _bench_pass_rate(recs)

    baseline = per_config.get("baseline", {})
    baseline_rows = [{"benchmark": b, "pass_pct": round(v, 2)} for b, v in sorted(baseline.items())]

    config_rows: list[dict[str, Any]] = []
    verdict_lines: list[str] = []
    for cid, bench_scores in sorted(per_config.items()):
        if cid == "baseline":
            continue
        row: dict[str, Any] = {"config": cid}
        max_drop = 0.0
        over_soft = 0
        for b, v in sorted(bench_scores.items()):
            base = baseline.get(b, 0.0)
            delta = v - base
            row[f"{b}_delta"] = round(delta, 2)
            drop = -delta
            if drop > max_drop:
                max_drop = drop
            if drop > gate.max_abs_regression_per_bench:
                over_soft += 1

        # Tool-call success delta (use roll-up across all benchmarks).
        base_tc = _config_tool_call_success(phase_a_root / "baseline")
        this_tc = _config_tool_call_success(phase_a_root / cid)
        tc_delta = this_tc - base_tc
        row["tool_call_success_delta"] = round(tc_delta, 2)

        # Phase B write tests passed delta.
        base_pass = _config_write_pass(phase_b_root / "baseline")
        this_pass = _config_write_pass(phase_b_root / cid)
        row["b2_pass_delta_tasks"] = this_pass - base_pass

        # Gate checks.
        soft_ok = over_soft == 0
        hard_ok = max_drop <= gate.hard_floor_per_bench
        tc_ok = (-tc_delta) <= gate.max_abs_regression_tool_call_success
        b2_ok = (base_pass - this_pass) <= gate.max_b2_task_regression

        row["gate_pass"] = soft_ok and hard_ok and tc_ok and b2_ok
        config_rows.append(row)

        verdict_lines.append(
            f"**{cid}**: "
            f"soft={'ok' if soft_ok else f'FAIL ({over_soft} bench > {gate.max_abs_regression_per_bench}pt)'}, "
            f"hard={'ok' if hard_ok else f'FAIL ({max_drop:.1f}pt)'}, "
            f"tool-call={'ok' if tc_ok else f'FAIL ({tc_delta:+.1f}pt)'}, "
            f"B2={'ok' if b2_ok else f'FAIL ({base_pass - this_pass} task regression)'} "
            f"→ {'PASS' if row['gate_pass'] else 'FAIL'}"
        )

    return baseline_rows, config_rows, verdict_lines


def ab_report(
    *,
    phase: str = "ab_ollama_vs_llamacpp",
    backends: tuple[str, str] = ("ollama_q4km", "llamacpp_q4km"),
    output_md: Path | None = None,
    output_csv: Path | None = None,
) -> str:
    """Side-by-side comparison of two backend configs across all benchmarks.

    For each (candidate, benchmark) pair, pivots the per-task records
    into a row showing each backend's mean TTFT, decode tok/s, peak
    RSS, and pass rate, plus the Δ% between them and a one-line
    verdict per candidate."""
    runs_root = RESULTS_ROOT / "runs" / phase
    if not runs_root.exists():
        return "No A/B results yet.\n"

    a_id, b_id = backends
    rows: list[dict[str, Any]] = []
    per_candidate_summary: list[str] = []

    for candidate_dir in sorted(runs_root.iterdir()):
        if not candidate_dir.is_dir():
            continue
        a_dir = candidate_dir / a_id
        b_dir = candidate_dir / b_id
        if not (a_dir.exists() and b_dir.exists()):
            continue

        bench_files = sorted(
            {p.name for p in a_dir.glob("*.jsonl")}
            | {p.name for p in b_dir.glob("*.jsonl")}
        )

        decode_a, decode_b = [], []
        ttft_a, ttft_b = [], []

        for bench_name in bench_files:
            a_recs = list(io.read(a_dir / bench_name)) if (a_dir / bench_name).exists() else []
            b_recs = list(io.read(b_dir / bench_name)) if (b_dir / bench_name).exists() else []
            if not (a_recs or b_recs):
                continue

            ttft_a_v, dec_a_v = _throughput(a_recs)
            ttft_b_v, dec_b_v = _throughput(b_recs)

            row = {
                "candidate": candidate_dir.name,
                "benchmark": bench_name.replace(".jsonl", ""),
                "n": min(len(a_recs), len(b_recs)),
                f"{a_id}_pass_pct": round(_bench_pass_rate(a_recs), 2),
                f"{b_id}_pass_pct": round(_bench_pass_rate(b_recs), 2),
                f"{a_id}_ttft_s": round(ttft_a_v, 3),
                f"{b_id}_ttft_s": round(ttft_b_v, 3),
                f"{a_id}_decode_tok_s": round(dec_a_v, 1),
                f"{b_id}_decode_tok_s": round(dec_b_v, 1),
                "ttft_delta_pct": _delta_pct(ttft_a_v, ttft_b_v, lower_is_better=True),
                "decode_delta_pct": _delta_pct(dec_a_v, dec_b_v, lower_is_better=False),
                f"{a_id}_peak_rss_gb": round(_peak_rss_gb(a_recs), 2),
                f"{b_id}_peak_rss_gb": round(_peak_rss_gb(b_recs), 2),
            }
            rows.append(row)
            if dec_a_v > 0:
                decode_a.append(dec_a_v)
            if dec_b_v > 0:
                decode_b.append(dec_b_v)
            if ttft_a_v > 0:
                ttft_a.append(ttft_a_v)
            if ttft_b_v > 0:
                ttft_b.append(ttft_b_v)

        if decode_a and decode_b:
            dec_a_mean = mean(decode_a)
            dec_b_mean = mean(decode_b)
            ttft_a_mean = mean(ttft_a) if ttft_a else 0.0
            ttft_b_mean = mean(ttft_b) if ttft_b else 0.0
            verdict = _verdict(
                a_id=a_id, b_id=b_id,
                ttft_a=ttft_a_mean, ttft_b=ttft_b_mean,
                dec_a=dec_a_mean, dec_b=dec_b_mean,
            )
            per_candidate_summary.append(
                f"- **{candidate_dir.name}** — {verdict}"
            )

    if output_csv:
        _write_csv(output_csv, rows)

    md_parts = [
        f"# A/B benchmark — `{a_id}` vs `{b_id}`\n",
        "## Per-candidate verdicts\n",
        *(line + "\n" for line in per_candidate_summary),
        "\n",
        _rows_to_markdown(
            f"Detail (Δ% rows: TTFT lower-is-better; decode higher-is-better)",
            rows,
        ),
    ]
    md = "".join(md_parts)
    if output_md:
        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(md)
    return md


def _delta_pct(a: float, b: float, *, lower_is_better: bool) -> str:
    """Format the percent change going from a to b. Sign convention: a
    minus sign means b improved over a in the metric's preferred
    direction."""
    if a <= 0 or b <= 0:
        return "—"
    raw = (b - a) / a * 100.0
    # When lower is better (e.g. TTFT), an improvement appears as
    # negative raw; we keep that sign so the reader sees `-22%` = good.
    sign = "+" if raw >= 0 else ""
    if lower_is_better:
        return f"{sign}{raw:.0f}%"
    # Higher is better (decode tok/s): same sign convention so + = good.
    return f"{sign}{raw:.0f}%"


def _verdict(
    *,
    a_id: str, b_id: str,
    ttft_a: float, ttft_b: float,
    dec_a: float, dec_b: float,
) -> str:
    ttft_better = b_id if ttft_b > 0 and ttft_a > 0 and ttft_b < ttft_a * 0.95 else (
        a_id if ttft_a > 0 and ttft_b > 0 and ttft_a < ttft_b * 0.95 else "tie"
    )
    dec_better = b_id if dec_b > dec_a * 1.05 else (a_id if dec_a > dec_b * 1.05 else "tie")
    if ttft_better == dec_better and ttft_better != "tie":
        return f"`{ttft_better}` wins on both TTFT and decode tok/s"
    if ttft_better == "tie" and dec_better == "tie":
        return "no meaningful difference"
    return (
        f"TTFT favors `{ttft_better}`, decode tok/s favors `{dec_better}`"
    )


def _config_tool_call_success(config_dir: Path) -> float:
    if not config_dir.exists():
        return 0.0
    all_recs: list[dict[str, Any]] = []
    for jsonl in config_dir.glob("*.jsonl"):
        all_recs.extend(io.read(jsonl))
    return _tool_call_success(all_recs)


def _config_write_pass(config_dir: Path) -> int:
    if not config_dir.exists():
        return 0
    count = 0
    for jsonl in config_dir.glob("write_*.jsonl"):
        for r in io.read(jsonl):
            if r.get("tests_passed"):
                count += 1
    return count


def _rows_to_markdown(title: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"## {title}\n\n(no data)\n"
    cols = sorted({k for row in rows for k in row.keys()})
    out = [f"## {title}\n", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(c, "")) for c in cols) + " |")
    return "\n".join(out) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = sorted({k for row in rows for k in row.keys()})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
