"""Top-level CLI: `lux <command>`. Thin wrapper around the phase scripts."""

from __future__ import annotations

from pathlib import Path

import typer

from harness import report
from harness.registry import load_registry

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command()
def candidates() -> None:
    """List configured Phase A candidates."""
    reg = load_registry()
    for c in reg.candidates:
        marker = "x" if c.active else " "
        print(f"[{marker}] {c.id:30s}  ~{c.mem_gb_q4:5.1f} GB @ Q4  ({c.family})")


@app.command()
def report_phase_a(
    out: Path = typer.Option(Path("results/phase_a.md"), "--out"),
    csv_out: Path = typer.Option(Path("results/phase_a.csv"), "--csv"),
) -> None:
    """Aggregate Phase A JSONL runs into markdown + CSV."""
    md = report.phase_a_report(output_md=out, output_csv=csv_out)
    print(md)


@app.command()
def report_phase_b(out: Path = typer.Option(Path("results/phase_b.md"), "--out")) -> None:
    """Aggregate Phase B personal-repo runs."""
    md = report.phase_b_report(output_md=out)
    print(md)


@app.command()
def report_phase_d(
    winner: str = typer.Option(..., "--winner"),
    out: Path = typer.Option(Path("results/phase_d.md"), "--out"),
) -> None:
    """Baseline vs optimized verdict for the Phase A/D winner."""
    md = report.phase_d_report(winner=winner, output_md=out)
    print(md)


if __name__ == "__main__":
    app()
