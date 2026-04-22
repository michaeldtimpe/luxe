"""Phase A — canned screening.

Runs a fixed benchmark suite across every active candidate in the baseline
(Q4, FP16 KV, no spec) configuration. Downloads models on demand.

    uv run python scripts/run_phase_a.py                 # all active candidates
    uv run python scripts/run_phase_a.py --candidate qwen2.5-coder-14b
    uv run python scripts/run_phase_a.py --limit 20      # quick-pass: 20 tasks per bench
    uv run python scripts/run_phase_a.py --bench humaneval_plus,mbpp_plus
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmarks._common import run_benchmark  # noqa: E402
from benchmarks.bfcl_v3 import BFCLv3  # noqa: E402
from benchmarks.humaneval_plus import HumanEvalPlus  # noqa: E402
from benchmarks.livecodebench import LiveCodeBench  # noqa: E402
from benchmarks.mbpp_plus import MbppPlus  # noqa: E402
from benchmarks.multipl_e import MultiPLE  # noqa: E402
from benchmarks.swebench_lite import SWEBenchLite  # noqa: E402
from benchmarks.tau_bench import TauBench  # noqa: E402
from harness.registry import load_optimization_registry, load_registry  # noqa: E402
from harness.server import launch_server  # noqa: E402

ALL_BENCHES = {
    "humaneval_plus": lambda: HumanEvalPlus(),
    "mbpp_plus": lambda: MbppPlus(),
    "multipl_e_rust": lambda: MultiPLE(language="rust"),
    "multipl_e_go": lambda: MultiPLE(language="go"),
    "livecodebench": lambda: LiveCodeBench(),
    "bfcl_v3": lambda: BFCLv3(category="simple"),
    "tau_bench": lambda: TauBench(domain="retail"),
    "swebench_lite": lambda: SWEBenchLite(),
}


def main(
    candidate: str = typer.Option("all", "--candidate"),
    bench: str = typer.Option(
        "humaneval_plus,mbpp_plus,multipl_e_rust,multipl_e_go,livecodebench",
        "--bench",
    ),
    limit: int | None = typer.Option(None, "--limit"),
    backend_kind: str = typer.Option("mlx", "--backend"),
) -> None:
    reg = load_registry()
    opt = load_optimization_registry()
    baseline = opt.get("baseline")

    candidates = reg.active_candidates() if candidate == "all" else [reg.get(candidate)]
    bench_keys = [b.strip() for b in bench.split(",") if b.strip()]

    n_cands = len(candidates)
    n_benches = len(bench_keys)
    for ci, cand in enumerate(candidates, 1):
        print(f"\n=== [{ci}/{n_cands}] {cand.display} ({cand.id}) ===")
        with launch_server(
            kind=backend_kind,
            candidate=cand,
            config=baseline,
            draft=None,  # Phase A runs baseline only — no spec decoding here.
        ) as backend:
            for bi, key in enumerate(bench_keys, 1):
                if key not in ALL_BENCHES:
                    print(f"  [skip] unknown bench: {key}")
                    continue
                print(f"\n--- [{ci}/{n_cands} · {bi}/{n_benches}] {key} ---")
                bench_obj = ALL_BENCHES[key]()
                run_benchmark(
                    bench_obj,
                    backend,
                    phase="phase_a",
                    candidate_id=cand.id,
                    config_id=baseline.id,
                    limit=limit,
                )


if __name__ == "__main__":
    typer.run(main)
