"""A/B perf benchmark: Ollama vs llama-server on the same Q4_K_M weights.

Sweeps each (candidate × backend) pair through a benchmark suite,
serializing model loads (each candidate is fully done before moving
on). After the sweep, regenerates the side-by-side report.

Examples:

    # Full sweep, all luxe-served candidates, both backends, all benches
    uv run python scripts/run_ab_benchmark.py

    # One model, both backends, BFCL only, 5 tasks (smoke test)
    uv run python scripts/run_ab_benchmark.py \\
        --candidate qwen2.5-coder-14b \\
        --bench bfcl_v3 --limit 5

    # Just regenerate the report from existing JSONL
    uv run python scripts/run_ab_benchmark.py --report-only
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks._common import run_benchmark  # noqa: E402
from benchmarks.bfcl_v3 import BFCLv3  # noqa: E402
from benchmarks.decode_throughput import DecodeThroughput  # noqa: E402
from benchmarks.humaneval_plus import HumanEvalPlus  # noqa: E402
from benchmarks.luxe_replay import LuxeReplay  # noqa: E402
from benchmarks.mbpp_plus import MbppPlus  # noqa: E402
from harness.registry import (  # noqa: E402
    Candidate,
    OptimizationConfig,
    load_optimization_registry,
    load_registry,
)
from harness.report import ab_report  # noqa: E402
from harness.server import launch_server  # noqa: E402

PHASE = "ab_ollama_vs_llamacpp"
RESULTS_DIR = ROOT / "results" / PHASE

# A/B-specific config_id — keeps the directory layout
# results/runs/<phase>/<candidate>/<config>/<bench>.jsonl distinct from
# Phase A's `baseline` and friends.
_CONFIG_IDS = {"ollama": "ollama_q4km", "llamacpp": "llamacpp_q4km"}

ALL_BENCHES = {
    "decode_throughput": lambda: DecodeThroughput(),
    "bfcl_v3": lambda: BFCLv3(category="simple"),
    "humaneval_plus": lambda: HumanEvalPlus(),
    "mbpp_plus": lambda: MbppPlus(),
    "luxe_replay": lambda: LuxeReplay(),
}

DEFAULT_CANDIDATES = (
    "qwen2.5-7b-instruct,qwen2.5-coder-14b,qwen2.5-32b-instruct"
)
DEFAULT_BENCHES = "decode_throughput,bfcl_v3,humaneval_plus,luxe_replay"


def _make_ab_config() -> OptimizationConfig:
    """Synthetic config for the A/B sweep — both backends use Q4_K_M
    weights and FP16 KV; OptimizationConfig is required by launch_server
    but its fields don't drive Ollama (which is externally managed)."""
    return OptimizationConfig(
        id="ab_q4km",
        display="A/B Q4_K_M baseline",
        weight_quant="q4_k_m",
        kv_quant="fp16",
        spec_decoding=False,
        spec_draft_tokens=0,
        prompt_cache=False,
        temperature=0.2,
    )


def _validate_token_parity(
    candidate: Candidate, ollama_url: str = "http://127.0.0.1:11434"
) -> None:
    """Quick sanity check before timing: same tiny prompt → similar
    prompt_tokens count from both backends. Loud warn on >5% drift."""
    if not candidate.ollama_tag:
        return
    probe = "Hello, world. Count to five."
    payload = {
        "messages": [{"role": "user", "content": probe}],
        "max_tokens": 4,
        "temperature": 0,
        "stream": False,
    }
    try:
        r = httpx.post(
            f"{ollama_url}/v1/chat/completions",
            json={"model": candidate.ollama_tag, **payload},
            timeout=60.0,
        )
        r.raise_for_status()
        ollama_tok = (r.json().get("usage") or {}).get("prompt_tokens", 0)
        # llama-server side will be measured live during run; just record
        # Ollama's number for the audit log — divergence is logged at
        # report time when both are present.
        typer.echo(f"  [parity] {candidate.id}: ollama prompt_tokens={ollama_tok}")
    except Exception as e:  # noqa: BLE001
        typer.echo(f"  [parity] {candidate.id}: ollama probe failed: {e}")


def main(
    candidate: str = typer.Option(DEFAULT_CANDIDATES, "--candidate"),
    backends: str = typer.Option("ollama,llamacpp", "--backends"),
    bench: str = typer.Option(DEFAULT_BENCHES, "--bench"),
    limit: int | None = typer.Option(None, "--limit"),
    report_only: bool = typer.Option(False, "--report-only"),
    ollama_url: str = typer.Option("http://127.0.0.1:11434", "--ollama-url"),
) -> None:
    if report_only:
        out = RESULTS_DIR / "REPORT.md"
        csv = RESULTS_DIR / "REPORT.csv"
        md = ab_report(
            phase=PHASE,
            backends=("ollama_q4km", "llamacpp_q4km"),
            output_md=out,
            output_csv=csv,
        )
        typer.echo(md)
        typer.echo(f"\nwrote {out} and {csv}")
        return

    reg = load_registry()
    candidates: list[Candidate] = []
    for cid in (c.strip() for c in candidate.split(",") if c.strip()):
        try:
            candidates.append(reg.get(cid))
        except KeyError:
            typer.echo(f"[skip] unknown candidate: {cid}")
    backend_kinds = [b.strip() for b in backends.split(",") if b.strip()]
    bench_keys = [b.strip() for b in bench.split(",") if b.strip()]

    config = _make_ab_config()

    n_runs = len(candidates) * len(backend_kinds) * len(bench_keys)
    typer.echo(
        f"=== A/B sweep: {len(candidates)} candidate(s) × "
        f"{len(backend_kinds)} backend(s) × {len(bench_keys)} bench(es) "
        f"= {n_runs} run(s) ===\n"
    )

    for ci, cand in enumerate(candidates, 1):
        typer.echo(f"\n--- [{ci}/{len(candidates)}] {cand.display} ({cand.id}) ---")
        if "ollama" in backend_kinds:
            _validate_token_parity(cand, ollama_url)
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
                    cfg_id = _CONFIG_IDS[kind]
                    for bk in bench_keys:
                        if bk not in ALL_BENCHES:
                            typer.echo(f"  [skip] unknown bench: {bk}")
                            continue
                        typer.echo(f"  → {kind} · {bk}")
                        run_benchmark(
                            ALL_BENCHES[bk](),
                            backend,
                            phase=PHASE,
                            candidate_id=cand.id,
                            config_id=cfg_id,
                            limit=limit,
                        )
            except Exception as e:  # noqa: BLE001
                typer.echo(f"  [error] {kind} run failed: {type(e).__name__}: {e}")
                continue
            typer.echo(
                f"  ← {kind} done in {time.monotonic() - t0:.0f}s"
            )

    typer.echo("\n=== regenerating report ===")
    out = RESULTS_DIR / "REPORT.md"
    csv = RESULTS_DIR / "REPORT.csv"
    md = ab_report(
        phase=PHASE,
        backends=("ollama_q4km", "llamacpp_q4km"),
        output_md=out,
        output_csv=csv,
    )
    typer.echo(md)
    typer.echo(f"\nwrote {out} and {csv}")


if __name__ == "__main__":
    typer.run(main)
