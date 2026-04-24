"""Aggregate compression-benchmark JSONL runs into a summary report.

Reads every row under `luxe/results/runs/compression_strategies/` and
emits per-strategy and per-strategy-per-task tables covering pass rate,
mean prompt tokens, file precision/recall, retrieval timing, and pass
vs. oracle.

Usage:
    uv run python scripts/analyze_compression_sweep.py [--format md|csv]
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results" / "runs" / "compression_strategies"


def _rows():
    for cand_dir in sorted(RESULTS.iterdir()):
        if not cand_dir.is_dir():
            continue
        for cfg_dir in sorted(cand_dir.iterdir()):
            if not cfg_dir.is_dir():
                continue
            # config_id looks like ollama_q4km_ctx4096__STRAT
            cfg = cfg_dir.name
            if "__" not in cfg:
                continue
            backend_cfg, strat = cfg.split("__", 1)
            for path in cfg_dir.glob("*.jsonl"):
                for line in path.open():
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    d = r.get("details", {}) or {}
                    m = r.get("metrics", {}) or {}
                    t = m.get("throughput", {}) or {}
                    yield {
                        "model": cand_dir.name,
                        "backend_cfg": backend_cfg,
                        "strategy": strat,
                        "task_id": r.get("task_id"),
                        "passed": bool(r.get("passed")),
                        "apply_ok": bool(d.get("apply_ok")),
                        "val_exit": d.get("validation_exit"),
                        "prompt_tokens": int(t.get("prompt_tokens_total", 0)),
                        "completion_tokens": int(t.get("completion_tokens_total", 0)),
                        "wall_s": float(m.get("wall_s", 0.0)),
                        "file_precision": float(d.get("file_precision") or 0.0),
                        "file_recall": float(d.get("file_recall") or 0.0),
                        "t_retrieval_s": float(d.get("t_retrieval_s") or 0.0),
                        "t_compression_s": float(d.get("t_compression_s") or 0.0),
                        "assembled_chars": int(d.get("assembled_prompt_chars") or 0),
                        "output_format": d.get("output_format", "unified_diff"),
                        "error": r.get("error"),
                    }


def _fmt_pct(num: float, denom: float) -> str:
    if not denom:
        return "-"
    return f"{int(round(num / denom * 100))}%"


def _strategy_summary(rows):
    by_strategy = defaultdict(list)
    for r in rows:
        by_strategy[(r["model"], r["strategy"])].append(r)

    summary = []
    for (model, strat), group in sorted(by_strategy.items()):
        n = len(group)
        n_pass = sum(1 for r in group if r["passed"])
        n_apply = sum(1 for r in group if r["apply_ok"])
        p_tokens = [r["prompt_tokens"] for r in group if r["prompt_tokens"] > 0]
        c_tokens = [r["completion_tokens"] for r in group if r["completion_tokens"] > 0]
        wall = [r["wall_s"] for r in group]
        prec = [r["file_precision"] for r in group]
        rec = [r["file_recall"] for r in group]
        summary.append({
            "model": model,
            "strategy": strat,
            "n": n,
            "pass": n_pass,
            "apply": n_apply,
            "pass_rate": n_pass / n if n else 0.0,
            "apply_rate": n_apply / n if n else 0.0,
            "mean_prompt_tok": statistics.mean(p_tokens) if p_tokens else 0,
            "mean_compl_tok": statistics.mean(c_tokens) if c_tokens else 0,
            "mean_wall_s": statistics.mean(wall) if wall else 0.0,
            "mean_precision": statistics.mean(prec) if prec else 0.0,
            "mean_recall": statistics.mean(rec) if rec else 0.0,
        })
    return summary


def _per_task_table(rows):
    """Rows where we can spot a strategy beating/losing on specific tasks."""
    by = defaultdict(dict)  # (model, task_id) -> {strategy: passed}
    for r in rows:
        by[(r["model"], r["task_id"])][r["strategy"]] = r["passed"]
    return by


def _print_summary_table(summary):
    print(f"{'model':22} {'strategy':28} {'n':>3} {'pass':>5} {'apply':>5} "
          f"{'mean_p_tok':>10} {'mean_c_tok':>10} {'wall_s':>7} {'prec':>5} {'rec':>5}")
    print("-" * 120)
    for s in summary:
        print(
            f"{s['model']:22} {s['strategy']:28} {s['n']:>3} "
            f"{_fmt_pct(s['pass'], s['n']):>5} "
            f"{_fmt_pct(s['apply'], s['n']):>5} "
            f"{int(s['mean_prompt_tok']):>10} {int(s['mean_compl_tok']):>10} "
            f"{s['mean_wall_s']:>7.1f} "
            f"{s['mean_precision']:.2f} {s['mean_recall']:.2f}"
        )


def _print_per_task_matrix(rows):
    # Restrict to 14b to keep terminal width manageable.
    models = sorted({r["model"] for r in rows})
    strategies = sorted({r["strategy"] for r in rows})
    tasks = sorted({r["task_id"] for r in rows})

    for model in models:
        print(f"\n=== {model}: per-task pass matrix ===")
        print(f"{'task':34} | " + " | ".join(s[:16] for s in strategies))
        print("-" * (34 + 3 + 19 * len(strategies)))
        for task in tasks:
            cells = []
            for strat in strategies:
                # Find the row for this (model, strat, task)
                found = None
                for r in rows:
                    if r["model"] == model and r["strategy"] == strat and r["task_id"] == task:
                        found = r
                        break
                if found is None:
                    cells.append("-".center(16))
                else:
                    mark = "✓" if found["passed"] else "✗"
                    cells.append(f"{mark} ({found['prompt_tokens']}t)".center(16))
            print(f"{task:34} | " + " | ".join(cells))


def _compression_verdict(summary, rows):
    """Compute the key comparisons that answer 'does compression make a
    measurable difference, and by how much.'"""
    # Per-model compression-vs-baseline deltas.
    out = []
    by_key = {(s["model"], s["strategy"]): s for s in summary}
    for model in sorted({s["model"] for s in summary}):
        def row(strat): return by_key.get((model, strat))
        baseline = row("baseline_whole_file")
        full = row("retrieve_full_wf")
        oracle = row("retrieve_oracle_whole_file")
        none_ = row("retrieve_none_wf")
        stack = row("stack_trace_guided_wf")
        outlines = row("file_outline_only_wf")
        summarize = row("retrieve_then_summarize_wf")

        out.append(f"\n=== {model}: strategy comparison ===")
        hdr = f"{'strategy':28} {'pass_rate':>9} {'prompt_tok':>11} {'vs_full':>10} {'pass/tok':>12}"
        out.append(hdr)
        out.append("-" * len(hdr))

        def line(s, label):
            if not s:
                return
            pr = s["pass_rate"] * 100
            pt = s["mean_prompt_tok"]
            if full:
                gap = int(pt - full["mean_prompt_tok"])
                gap_tok = f"{gap:+d}"
            else:
                gap_tok = "-"
            eff = (s["pass"] / pt) if pt else 0
            out.append(
                f"{label:28} {pr:>8.0f}% {int(pt):>11} "
                f"{gap_tok:>10} {eff:>12.4f}"
            )

        line(none_, "retrieve_none_wf")
        line(oracle, "retrieve_oracle_whole_file")
        line(summarize, "retrieve_then_summarize_wf")
        line(outlines, "file_outline_only_wf")
        line(stack, "stack_trace_guided_wf")
        line(baseline, "baseline_whole_file")
        line(full, "retrieve_full_wf")

    return "\n".join(out)


def main(show_errors: bool = typer.Option(False, "--show-errors")) -> None:
    rows = list(_rows())
    if not rows:
        print("no results found under", RESULTS)
        sys.exit(1)

    summary = _strategy_summary(rows)
    _print_summary_table(summary)
    print()
    print(_compression_verdict(summary, rows))
    _print_per_task_matrix(rows)

    if show_errors:
        print("\n=== failures ===")
        for r in rows:
            if not r["passed"]:
                err = (r["error"] or "").splitlines()[0] if r["error"] else "-"
                print(f"  {r['model']} / {r['strategy']} / {r['task_id']}: {err[:120]}")


if __name__ == "__main__":
    typer.run(main)
