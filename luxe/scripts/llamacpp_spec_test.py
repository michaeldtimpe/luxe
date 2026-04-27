"""llama-server speculative-decoding A/B against the same workload
the oMLX DFlash test ran. Brings up llama-server twice — once with
baseline config, once with spec-only — and writes JSONL into
results/runs/<phase>/<candidate>/{llamacpp_q4km,llamacpp_q4km_spec}/<bench>.jsonl.

Reuses harness.server.launch_server (kind="llamacpp") which already
wires --model-draft + --draft-max from OptimizationConfig.spec_decoding.

Usage:

    uv run python scripts/llamacpp_spec_test.py \\
        --candidate qwen2.5-coder-14b \\
        --bench decode_throughput,humaneval_plus,prefix_cache_decay \\
        --limit 30
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks._common import run_benchmark  # noqa: E402
from benchmarks.decode_throughput import DecodeThroughput  # noqa: E402
from benchmarks.humaneval_plus import HumanEvalPlus  # noqa: E402
from benchmarks.prefix_cache_decay import PrefixCacheDecay  # noqa: E402
from harness.registry import OptimizationConfig, load_registry  # noqa: E402
from harness.server import launch_server  # noqa: E402

PHASE_DEFAULT = "ab_ollama_vs_omlx"  # share dir with the oMLX runs

ALL_BENCHES = {
    "decode_throughput": lambda: DecodeThroughput(),
    "humaneval_plus": lambda: HumanEvalPlus(),
    "prefix_cache_decay": lambda: PrefixCacheDecay(),
}


def _make_config(spec_on: bool, draft_tokens: int) -> OptimizationConfig:
    return OptimizationConfig(
        id=("llamacpp_spec" if spec_on else "llamacpp_baseline"),
        display=(
            f"llama-server Q4_K_M{' + spec(N=' + str(draft_tokens) + ')' if spec_on else ''}"
        ),
        weight_quant="q4_k_m",
        kv_quant="fp16",
        spec_decoding=spec_on,
        spec_draft_tokens=draft_tokens if spec_on else 0,
        prompt_cache=False,
        temperature=0.2,
    )


def main(
    candidate: str = typer.Option("qwen2.5-coder-14b", "--candidate"),
    bench: str = typer.Option(
        "decode_throughput,humaneval_plus,prefix_cache_decay", "--bench"
    ),
    limit: int | None = typer.Option(None, "--limit"),
    phase: str = typer.Option(PHASE_DEFAULT, "--phase"),
    draft_tokens: int = typer.Option(3, "--draft-tokens",
        help="Number of speculative tokens per step (matches optimization_configs.yaml's spec-only)."),
) -> None:
    reg = load_registry()
    cand = reg.get(candidate)
    draft = reg.draft_for(cand)
    if not draft:
        typer.echo(f"ERROR: candidate {candidate} has no draft_id configured", err=True)
        sys.exit(1)
    bench_keys = [b.strip() for b in bench.split(",") if b.strip()]

    for spec_on in (False, True):
        cfg = _make_config(spec_on, draft_tokens)
        cfg_id = "llamacpp_q4km_spec" if spec_on else "llamacpp_q4km"
        typer.echo(f"\n=== {cand.id} · {cfg_id} (spec={spec_on}) ===")
        t0 = time.monotonic()
        with launch_server(
            kind="llamacpp",
            candidate=cand,
            config=cfg,
            draft=draft if spec_on else None,
        ) as backend:
            for key in bench_keys:
                if key not in ALL_BENCHES:
                    typer.echo(f"  [skip] unknown bench: {key}")
                    continue
                typer.echo(f"  → {key}")
                try:
                    run_benchmark(
                        ALL_BENCHES[key](),
                        backend,
                        phase=phase,
                        candidate_id=cand.id,
                        config_id=cfg_id,
                        limit=limit,
                    )
                except Exception as e:  # noqa: BLE001
                    typer.echo(f"  [error] {key} failed: {type(e).__name__}: {e}")
        typer.echo(f"  ← {cfg_id} done in {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    typer.run(main)
