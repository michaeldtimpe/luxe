"""Phase C — apply optimization configs to the winner.

Phase C is a thin wrapper: it re-runs Phase A on the winner under each
optimization config (baseline + 4 variants). Phase D then compares them.

    uv run python scripts/run_phase_c.py --winner qwen2.5-coder-32b
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks._common import run_benchmark  # noqa: E402
from harness.registry import load_optimization_registry, load_registry  # noqa: E402
from harness.server import launch_server  # noqa: E402
from scripts.run_phase_a import ALL_BENCHES  # noqa: E402


def main(
    winner: str = typer.Option(..., "--winner"),
    bench: str = typer.Option(
        "humaneval_plus,mbpp_plus,multipl_e_rust,multipl_e_go,livecodebench",
        "--bench",
    ),
    limit: int | None = typer.Option(None, "--limit"),
    backend_kind: str = typer.Option("mlx", "--backend"),
    skip_baseline: bool = typer.Option(
        True, "--skip-baseline/--run-baseline",
        help="Skip baseline if Phase A already ran it (default).",
    ),
) -> None:
    reg = load_registry()
    opt = load_optimization_registry()
    cand = reg.get(winner)
    draft = reg.draft_for(cand)
    bench_keys = [b.strip() for b in bench.split(",") if b.strip()]

    for cfg in opt.configs:
        if skip_baseline and cfg.id == "baseline":
            print(f"[skip] baseline (use Phase A results)")
            continue
        print(f"\n=== {cand.id} · {cfg.id} ===")
        use_draft = draft if cfg.spec_decoding else None
        with launch_server(kind=backend_kind, candidate=cand, config=cfg, draft=use_draft) as backend:
            for key in bench_keys:
                if key not in ALL_BENCHES:
                    continue
                run_benchmark(
                    ALL_BENCHES[key](),
                    backend,
                    phase="phase_a",  # re-populate phase_a/<winner>/<config>/* tree
                    candidate_id=cand.id,
                    config_id=cfg.id,
                    limit=limit,
                )


if __name__ == "__main__":
    typer.run(main)
