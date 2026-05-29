"""Perplexity runner — sliding-window over WikiText-103-raw via mlx_lm in-process.

Internal regression metric. See benchmarks/perplexity/README.md.

Usage:
  python -m benchmarks.perplexity.run --output acceptance/perplexity/<run_id>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from benchmarks._eval_common.dataset import cache_dir, sha256_file  # noqa: E402
from benchmarks._eval_common.logprob import aggregate, plan_sliding_windows  # noqa: E402
from benchmarks._eval_common.meta import build_run_meta  # noqa: E402

BENCHMARK_PROTOCOL_VERSION = "perplexity/v1"
DEFAULT_CORPUS = cache_dir("wikitext") / "wikitext-103-raw-test.txt"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = Path(args.corpus) if args.corpus else DEFAULT_CORPUS
    if not corpus_path.exists():
        print(
            f"WikiText not found at {corpus_path}. Run "
            f"`python scripts/fetch_wikitext_data.py`.",
            file=sys.stderr,
        )
        return 2

    text = corpus_path.read_text()
    print(f"  corpus: {len(text):,} chars, sha256={sha256_file(corpus_path)[:16]}…")

    from benchmarks._eval_common.mlx_direct import MLXDirectBackend
    print(f"  loading {args.model} via mlx_lm...")
    backend = MLXDirectBackend(args.model)
    ids = backend.tokenizer.encode(text)
    total = len(ids)
    print(f"  tokenized: {total:,} tokens")

    if args.max_tokens_eval and total > args.max_tokens_eval:
        ids = ids[: args.max_tokens_eval]
        total = len(ids)
        print(f"  truncated to {total:,} tokens (--max-tokens-eval)")

    windows = plan_sliding_windows(total, window=args.window, stride=args.stride)
    print(f"  planned {len(windows)} windows (window={args.window}, stride={args.stride})")

    nll_sum = 0.0
    token_count = 0
    t0 = time.time()

    for wi, w in enumerate(windows):
        window_ids = ids[w.window_start : w.window_end]
        all_lps = backend.token_logprobs_from_ids(window_ids)
        # all_lps[i] is logprob of window_ids[i+1] conditioned on window_ids[0..i]
        # i.e. logprob of absolute token at (w.window_start + i + 1)
        for local_i, lp in enumerate(all_lps):
            abs_pos = w.window_start + local_i + 1
            if w.eval_start <= abs_pos < w.eval_end:
                nll_sum -= lp
                token_count += 1
        if (wi + 1) % 5 == 0 or wi == len(windows) - 1:
            elapsed = time.time() - t0
            rate = elapsed / (wi + 1)
            eta_m = (len(windows) - wi - 1) * rate / 60.0
            running_ppl = float("inf")
            if token_count > 0:
                import math
                running_ppl = math.exp(nll_sum / token_count)
            print(
                f"  window {wi+1}/{len(windows)} tokens_evaluated={token_count:,} "
                f"running_ppl={running_ppl:.3f} avg={rate:.1f}s eta={eta_m:.1f}m"
            )

    result = aggregate(nll_sum, token_count, len(windows))
    print(
        f"  Perplexity = {result.perplexity:.4f} "
        f"(nll_sum={result.nll_sum:.2f}, tokens={result.token_count:,}, "
        f"windows={result.num_windows})"
    )

    meta = build_run_meta(
        benchmark_protocol_version=BENCHMARK_PROTOCOL_VERSION,
        model_id=args.model,
        sampling={"temperature": "N/A (no sampling)"},
        backend_kind="mlx_direct",
        context_window=args.window,
        benchmark_dataset_sha256=sha256_file(corpus_path),
        scoring={
            "method": "sliding_window_perplexity",
            "window": args.window,
            "stride": args.stride,
            "tokenizer": type(backend.tokenizer).__name__,
            "note": "internal regression metric; not leaderboard-comparable",
        },
    )
    summary = {
        "meta": meta.to_dict(),
        "results": {
            "perplexity": result.perplexity,
            "nll_sum": result.nll_sum,
            "tokens_evaluated": result.token_count,
            "num_windows": result.num_windows,
            "total_corpus_tokens": total,
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m benchmarks.perplexity.run")
    p.add_argument("--output", required=True)
    p.add_argument("--corpus", default=None)
    p.add_argument("--model", default="mlx-community/Qwen3.6-35B-A3B-6bit")
    # mlx_lm Qwen3.6 RoPE breaks at ≥16K context (PPL collapses to ~20k+).
    # Defaults sit comfortably below that cliff. Upstream comparison would
    # want 32K/16K once mlx_lm's long-context implementation is fixed.
    p.add_argument("--window", type=int, default=8192)
    p.add_argument("--stride", type=int, default=4096)
    p.add_argument(
        "--max-tokens-eval",
        type=int,
        default=None,
        help="Cap total corpus tokens (for fast partial runs).",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
