"""Compression-benchmark runner: (candidate × backend × strategy) sweep.

Example:

    uv run python scripts/run_compression_bench.py \\
        --candidate qwen2.5-coder-14b \\
        --backends ollama \\
        --strategy baseline_retrieval_only \\
        --limit 1
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks._common import run_benchmark  # noqa: E402
from benchmarks.compression_repo import CompressionRepo  # noqa: E402
from harness.registry import Candidate, OptimizationConfig, load_registry  # noqa: E402
from harness.server import launch_server  # noqa: E402
from strategies import load_strategy  # noqa: E402

PHASE = "compression_strategies"

_CONFIG_IDS = {"ollama": "ollama_q4km", "llamacpp": "llamacpp_q4km"}


def _make_config() -> OptimizationConfig:
    """Synthetic config — parity with the A/B sweep's Q4_K_M baseline so
    compression results can be compared against that run."""
    return OptimizationConfig(
        id="ab_q4km",
        display="Compression strategies Q4_K_M baseline",
        weight_quant="q4_k_m",
        kv_quant="fp16",
        spec_decoding=False,
        spec_draft_tokens=0,
        prompt_cache=False,
        temperature=0.2,
    )


def main(
    candidate: str = typer.Option("qwen2.5-coder-14b", "--candidate"),
    backends: str = typer.Option("ollama", "--backends"),
    strategy: str = typer.Option("baseline_retrieval_only", "--strategy"),
    limit: int | None = typer.Option(None, "--limit"),
    num_ctx: int | None = typer.Option(
        None,
        "--num-ctx",
        help="Constrain Ollama's context window (in tokens). Creates "
        "pressure so strategies that retrieve less get a material "
        "advantage. 4096 is a good stress setting for small fixtures.",
    ),
    max_tokens: int = typer.Option(
        2048,
        "--max-tokens",
        help="Max completion tokens; bump for whole_file output on "
        "larger files.",
    ),
    ollama_url: str = typer.Option("http://127.0.0.1:11434", "--ollama-url"),
) -> None:
    reg = load_registry()
    candidates: list[Candidate] = []
    for cid in (c.strip() for c in candidate.split(",") if c.strip()):
        try:
            candidates.append(reg.get(cid))
        except KeyError:
            typer.echo(f"[skip] unknown candidate: {cid}")

    backend_kinds = [b.strip() for b in backends.split(",") if b.strip()]
    strategies = [s.strip() for s in strategy.split(",") if s.strip()]
    config = _make_config()

    n_runs = len(candidates) * len(backend_kinds) * len(strategies)
    typer.echo(
        f"=== compression sweep: {len(candidates)} candidate(s) × "
        f"{len(backend_kinds)} backend(s) × {len(strategies)} strategy(ies) "
        f"= {n_runs} run(s) ===\n"
    )

    for ci, cand in enumerate(candidates, 1):
        typer.echo(f"\n--- [{ci}/{len(candidates)}] {cand.display} ({cand.id}) ---")
        for kind in backend_kinds:
            t0 = time.monotonic()
            try:
                with launch_server(
                    kind=kind,
                    candidate=cand,
                    config=config,
                    draft=None,
                    ollama_base_url=ollama_url,
                ) as backend:
                    base_cfg_id = _CONFIG_IDS.get(kind, kind)
                    extra_body = (
                        {"options": {"num_ctx": num_ctx}} if (num_ctx and kind == "ollama") else None
                    )
                    for strat_name in strategies:
                        strat = load_strategy(strat_name)
                        # Split results per strategy so one JSONL file per
                        # (candidate, strategy) pair — reports can pivot.
                        ctx_tag = f"_ctx{num_ctx}" if num_ctx else ""
                        cfg_id = f"{base_cfg_id}{ctx_tag}__{strat_name}"
                        typer.echo(f"  → {kind} · {strat_name}")
                        bench = CompressionRepo(strategy=strat)
                        run_benchmark(
                            bench,
                            backend,
                            phase=PHASE,
                            candidate_id=cand.id,
                            config_id=cfg_id,
                            limit=limit,
                            max_tokens=max_tokens,
                            extra_body=extra_body,
                        )
            except Exception as e:  # noqa: BLE001
                typer.echo(f"  [error] {kind} run failed: {type(e).__name__}: {e}")
                continue
            typer.echo(f"  ← {kind} done in {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    typer.run(main)
