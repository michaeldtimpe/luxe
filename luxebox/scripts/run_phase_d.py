"""Phase D — baseline vs optimized report + acceptance-gate verdict.

Reads the JSONL logs written by Phase A (baseline) and Phase C (optimized
variants) for the winner, and emits the gate verdict.

    uv run python scripts/run_phase_d.py --winner qwen2.5-coder-32b
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import report  # noqa: E402


def main(
    winner: str = typer.Option(..., "--winner"),
    out: Path = typer.Option(Path("results/phase_d.md"), "--out"),
) -> None:
    md = report.phase_d_report(winner=winner, output_md=out)
    print(md)


if __name__ == "__main__":
    typer.run(main)
