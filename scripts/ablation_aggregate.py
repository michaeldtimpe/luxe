#!/usr/bin/env python
"""Aggregate the four ablation cells into a cross-cell comparison report.

Reads:
  <root>/<cell>/bfcl/summary.json
  <root>/<cell>/maintain_suite/summary.json
  <root>/<cell>/swebench/summary.json
  <root>/<cell>/swebench/harness_summary.json

Writes:
  <root>/comparison.md   — markdown table, cell × benchmark
  <root>/comparison.json — machine-readable cross-cell data

Usage:
  python scripts/ablation_aggregate.py --root acceptance/agentic_ablation/
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


CELLS = ("baseline", "tiered", "respond", "trajectory")


def _load(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        print(f"  warn: failed to read {p}: {e}", file=sys.stderr)
        return None


def _pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{float(v):.2%}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ablation_aggregate.py")
    p.add_argument("--root", required=True, type=Path)
    args = p.parse_args(argv)

    root = args.root
    if not root.exists():
        print(f"missing root: {root}", file=sys.stderr)
        return 2

    data: dict[str, dict] = {}
    for cell in CELLS:
        cell_dir = root / cell
        cell_data: dict[str, dict | None] = {
            "bfcl": _load(cell_dir / "bfcl" / "summary.json"),
            "maintain_suite": _load(cell_dir / "maintain_suite" / "summary.json"),
            "swebench": _load(cell_dir / "swebench" / "summary.json"),
            "swebench_harness": _load(cell_dir / "swebench" / "harness_summary.json"),
        }
        data[cell] = cell_data

    # --- BFCL: extract overall pass rate per cell --------------------------
    def _bfcl_overall(cell: str) -> tuple[int | None, int | None, float | None]:
        s = data[cell].get("bfcl")
        if not s:
            return None, None, None
        # bfcl summary.json shape: results.{category: {n, passed, pass_rate}}
        results = s.get("results") or s
        n_total = 0
        n_passed = 0
        for cat, v in (results.get("by_category") or results.get("categories") or {}).items():
            n_total += int(v.get("n", 0))
            n_passed += int(v.get("passed", 0))
        if n_total == 0:
            # Fallback: maybe overall stored flat
            n_total = int(results.get("n", 0) or 0)
            n_passed = int(results.get("passed", 0) or 0)
        rate = n_passed / n_total if n_total else None
        return n_total, n_passed, rate

    # --- maintain_suite: extract pass rate + score ------------------------
    def _maintain(cell: str) -> tuple[int | None, int | None, float | None]:
        s = data[cell].get("maintain_suite")
        if not s:
            return None, None, None
        n = int(s.get("fixtures_run", s.get("n", 0)) or 0)
        passed = int(s.get("passed", 0) or 0)
        score = s.get("score")
        return n, passed, score

    # --- SWE-bench: predictions count + resolved rate ---------------------
    def _swebench(cell: str) -> tuple[int | None, int | None, float | None]:
        preds = data[cell].get("swebench")
        harness = data[cell].get("swebench_harness")
        n_preds = preds.get("n_with_patch", preds.get("n")) if preds else None
        if harness:
            n_res = harness.get("n_resolved")
            rate = harness.get("resolution_rate")
        else:
            n_res = None
            rate = None
        return n_preds, n_res, rate

    # --- Render -----------------------------------------------------------
    lines: list[str] = []
    lines.append(f"# Agentic ablation matrix — {root.name}\n")
    lines.append("Per-cell agentic suite numbers across the four forge-hybrid "
                 "feature configurations. See HANDOFF-M5-ABLATION.md in the "
                 "research repo for the cell definitions and rationale.\n")

    lines.append("## BFCL (agentic mode)\n")
    lines.append("| Cell | n | passed | pass_rate |")
    lines.append("|---|---:|---:|---:|")
    for cell in CELLS:
        n, p_, r = _bfcl_overall(cell)
        lines.append(f"| {cell} | {n or '—'} | {p_ or '—'} | **{_pct(r)}** |")
    lines.append("")

    lines.append("## maintain_suite\n")
    lines.append("| Cell | n | passed | score |")
    lines.append("|---|---:|---:|---:|")
    for cell in CELLS:
        n, p_, sc = _maintain(cell)
        lines.append(f"| {cell} | {n or '—'} | {p_ or '—'} | **{sc if sc is not None else '—'}** |")
    lines.append("")

    lines.append("## SWE-bench (preds + Docker harness)\n")
    lines.append("| Cell | preds_with_patch | resolved | resolution_rate |")
    lines.append("|---|---:|---:|---:|")
    for cell in CELLS:
        n_p, n_r, rate = _swebench(cell)
        lines.append(f"| {cell} | {n_p or '—'} | {n_r or '—'} | **{_pct(rate)}** |")
    lines.append("")

    lines.append("## Raw per-cell summary file inventory\n")
    for cell in CELLS:
        lines.append(f"### {cell}\n")
        for bench, s in data[cell].items():
            status = "✓" if s else "—"
            lines.append(f"- {bench}: {status}")
        lines.append("")

    out_md = root / "comparison.md"
    out_md.write_text("\n".join(lines))
    print(f"  wrote {out_md}")

    out_json = root / "comparison.json"
    out_json.write_text(json.dumps(data, indent=2, default=str))
    print(f"  wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
