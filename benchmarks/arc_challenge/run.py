"""ARC-Challenge runner — 0-shot, first-token logprob via MLXDirectBackend.

Same sequencing constraint as MMLU: oMLX must be stopped (or have RAM
headroom) before invoking.

Usage:
  python -m benchmarks.arc_challenge.run --output acceptance/arc/<run_id>
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from benchmarks._eval_common.dataset import cache_dir, jsonl_load, sha256_file  # noqa: E402
from benchmarks._eval_common.meta import build_run_meta  # noqa: E402
from benchmarks.arc_challenge.adapter import (  # noqa: E402
    LETTERS,
    build_prompt,
    gold_letter,
    valid_letters_for,
)
from benchmarks.arc_challenge.grade import aggregate  # noqa: E402

BENCHMARK_PROTOCOL_VERSION = "arc_challenge/v1"
DEFAULT_DATA = cache_dir("arc") / "challenge_test.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    data_path = Path(args.data) if args.data else DEFAULT_DATA
    if not data_path.exists():
        print(
            f"ARC data missing at {data_path}. Run `python scripts/fetch_arc_data.py`.",
            file=sys.stderr,
        )
        return 2

    rows = list(jsonl_load(data_path))
    if args.limit is not None:
        rows = rows[: args.limit]

    from benchmarks._eval_common.mlx_direct import MLXDirectBackend
    print(f"  loading {args.model} via mlx_lm...")
    backend = MLXDirectBackend(args.model)

    choice_token_ids = backend.encode_choice_letters(LETTERS)
    print(f"  choice token ids: {choice_token_ids}")

    n_total = len(rows)
    n_done = 0
    n_correct = 0
    t0 = time.time()

    for i, row in enumerate(rows):
        item_path = out_dir / f"q_{i:05d}.json"
        if args.resume and item_path.exists():
            cached = json.loads(item_path.read_text())
            n_correct += int(cached.get("correct", False))
            n_done += 1
            continue

        valid = valid_letters_for(row)
        user_prompt = build_prompt(row)
        # enable_thinking=False: see mmlu/run.py comment for rationale.
        messages = [{"role": "user", "content": user_prompt}]
        try:
            prompt_text = backend.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
        except Exception:
            prompt_text = user_prompt

        t_start = time.time()
        scores = backend.score_choices(prompt_text, valid, top_k=args.top_k)
        wall_s = time.time() - t_start

        predicted = max(valid, key=lambda L: scores[L])
        gold = gold_letter(row)
        correct = predicted == gold

        record = {
            "id": row["id"],
            "qid": i,
            "question": row["question"],
            "choices": row["choices"],
            "n_choices": len(row["choices"]["text"]),
            "gold": gold,
            "predicted": predicted,
            "correct": correct,
            "scores": {L: (None if not math.isfinite(scores[L]) else scores[L]) for L in valid},
            "wall_s": wall_s,
        }
        item_path.write_text(json.dumps(record, indent=2))
        n_correct += int(correct)
        n_done += 1

        if n_done % 25 == 0 or n_done == n_total:
            elapsed = time.time() - t0
            rate = elapsed / max(1, n_done)
            eta_m = (n_total - n_done) * rate / 60.0
            print(
                f"  arc {n_done}/{n_total} acc={n_correct/n_done:.2%} "
                f"avg={rate:.2f}s eta={eta_m:.1f}m"
            )

    items = [json.loads(p.read_text()) for p in sorted(out_dir.glob("q_*.json"))]
    summary_stats = aggregate(items)

    meta = build_run_meta(
        benchmark_protocol_version=BENCHMARK_PROTOCOL_VERSION,
        model_id=args.model,
        sampling={"temperature": 0.0, "max_tokens": 1, "top_k_inspected": args.top_k},
        backend_kind="mlx_direct",
        context_window=getattr(backend.tokenizer, "model_max_length", 32768) or 32768,
        benchmark_dataset_sha256=sha256_file(data_path),
        scoring={
            "method": "first_token_top_logprob_via_mlx_direct",
            "choice_token_ids": choice_token_ids,
            "chat_template_applied": True,
            "shots": 0,
        },
    )
    summary = {"meta": meta.to_dict(), "results": summary_stats}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print(
        f"ARC-Challenge — {summary_stats['count']} questions, "
        f"acc={summary_stats['accuracy']:.2%}, "
        f"by_choices={summary_stats['per_choice_count']}"
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m benchmarks.arc_challenge.run")
    p.add_argument("--output", required=True)
    p.add_argument("--data", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--model", default="mlx-community/Qwen3.6-35B-A3B-6bit")
    p.add_argument("--top-k", type=int, default=50)
    p.add_argument("--resume", action="store_true", default=True)
    p.add_argument("--no-resume", dest="resume", action="store_false")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
