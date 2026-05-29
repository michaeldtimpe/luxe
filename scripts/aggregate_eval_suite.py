"""Aggregate an eval-suite run's per-benchmark summary.json files into a markdown report.

Usage:
  python scripts/aggregate_eval_suite.py --run-dir acceptance/eval_suite/<ts>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        print(f"run dir not found: {run_dir}", file=sys.stderr)
        return 2

    sections: list[str] = []
    sections.append(f"# Eval suite — {run_dir.name}")
    sections.append("")

    meta_block = None
    # gsm8k can land at canonical `gsm8k/` or split into `gsm8k_think/`
    # `gsm8k_nothink/` (deployed-mode + canonical-methodology pair).
    bench_dirs: list[tuple[str, str]] = []
    for variant in ("gsm8k", "gsm8k_think", "gsm8k_nothink"):
        if (run_dir / variant / "summary.json").exists():
            bench_dirs.append(("gsm8k", variant))
    for bench in ("codeneedle", "mmlu", "arc_challenge", "perplexity"):
        bench_dirs.append((bench, bench))
    for bench, subdir in bench_dirs:
        bench_dir = run_dir / subdir
        summary_path = bench_dir / "summary.json"
        if not summary_path.exists():
            sections.append(f"## {subdir}")
            sections.append("(not run)")
            sections.append("")
            continue
        s = json.loads(summary_path.read_text())
        meta = s.get("meta") or {}
        if meta_block is None and meta:
            meta_block = meta
        sections.append(f"## {subdir}")
        sections.append(f"- protocol: `{meta.get('benchmark_protocol_version', '?')}`")
        if bench == "gsm8k":
            samp = meta.get("sampling") or {}
            sections.append(f"- think_mode: `{samp.get('think_mode')}`  max_tokens: `{samp.get('max_tokens')}`")

        results = s.get("results") or s.get("results_by_corpus") or {}
        sections.extend(_render_results(bench, results))
        sections.append("")

    # Top-of-report meta block
    if meta_block:
        top: list[str] = ["## Run metadata", ""]
        top.append(f"- eval_suite_version: `{meta_block.get('eval_suite_version')}`")
        top.append(f"- model: `{meta_block.get('model_id')}`")
        top.append(f"- luxe_commit: `{meta_block.get('luxe_commit')}`")
        top.append(f"- timestamp_utc: `{meta_block.get('timestamp_utc')}`")
        top.append(f"- device: `{meta_block.get('device')}`")
        top.append("")
        sections = sections[:2] + top + sections[2:]

    out = run_dir / "summary.md"
    out.write_text("\n".join(sections))
    print(f"  wrote {out}")
    return 0


def _render_results(bench: str, results: dict) -> list[str]:
    lines: list[str] = []
    if bench == "gsm8k":
        lines.append(f"- count: {results.get('count')}")
        lines.append(f"- accuracy: **{_pct(results.get('accuracy'))}**")
        lines.append(f"- parse_rate: {_pct(results.get('parse_rate'))}")
        if results.get("failure_reasons"):
            lines.append(f"- failure_reasons: {results['failure_reasons']}")
    elif bench == "mmlu":
        lines.append(f"- count: {results.get('count')}")
        lines.append(f"- accuracy_micro: **{_pct(results.get('accuracy_micro'))}**")
        lines.append(f"- accuracy_macro_per_subject: {_pct(results.get('accuracy_macro_per_subject'))}")
        per_cat = results.get("per_category") or {}
        for cat in ("STEM", "humanities", "social_sciences", "other"):
            if cat in per_cat:
                lines.append(f"  - {cat}: {_pct(per_cat[cat])}")
    elif bench == "arc_challenge":
        lines.append(f"- count: {results.get('count')}")
        lines.append(f"- accuracy: **{_pct(results.get('accuracy'))}**")
        per_n = results.get("per_choice_count") or {}
        for n in sorted(per_n.keys()):
            d = per_n[n]
            lines.append(f"  - {n}-choice questions: {_pct(d['accuracy'])} (n={d['n']})")
    elif bench == "codeneedle":
        # results_by_corpus shape
        for corpus, s in results.items():
            lines.append(f"- {corpus}:")
            lines.append(f"  - pass_rate: **{_pct(s.get('pass_rate'))}**")
            lines.append(f"  - primary_match_rate: {_pct(s.get('primary_match_rate'))}")
            lines.append(f"  - hallucinations: {s.get('hallucinated')}")
            lines.append(f"  - bonus_matched: {s.get('bonus_matched')}")
    elif bench == "perplexity":
        ppl = results.get("perplexity")
        lines.append(f"- perplexity (internal metric, NOT leaderboard-comparable): **{ppl:.4f}**" if ppl else "- perplexity: ?")
        lines.append(f"- tokens_evaluated: {results.get('tokens_evaluated'):,}" if results.get("tokens_evaluated") else "")
        lines.append(f"- num_windows: {results.get('num_windows')}")
    return lines


def _pct(v) -> str:
    if v is None:
        return "?"
    return f"{float(v):.2%}"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python scripts/aggregate_eval_suite.py")
    p.add_argument("--run-dir", required=True)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
