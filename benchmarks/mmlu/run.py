"""MMLU runner — uses MLXDirectBackend for chat-template + first-token logprob scoring.

Sequencing: this benchmark loads the model in-process via mlx_lm. The oMLX
HTTP server must be stopped (or have memory headroom for both copies of
the 35B model) before invoking this on a 64 GB machine.

Usage:
  python -m benchmarks.mmlu.run --output acceptance/mmlu/<run_id> [--limit N]
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
from benchmarks.mmlu.adapter import (  # noqa: E402
    LETTERS,
    build_prompt,
    fewshot_for_subject,
    index_by_subject,
)
from benchmarks.mmlu.grade import aggregate  # noqa: E402


BENCHMARK_PROTOCOL_VERSION = "mmlu/v1"
DEFAULT_TEST = cache_dir("mmlu") / "test.jsonl"
DEFAULT_DEV = cache_dir("mmlu") / "dev.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    test_path = Path(args.test_data) if args.test_data else DEFAULT_TEST
    dev_path = Path(args.dev_data) if args.dev_data else DEFAULT_DEV

    for p in (test_path, dev_path):
        if not p.exists():
            print(
                f"MMLU data missing at {p}. Run `python scripts/fetch_mmlu_data.py`.",
                file=sys.stderr,
            )
            return 2

    test_rows = list(jsonl_load(test_path))
    dev_rows = list(jsonl_load(dev_path))

    if args.subject:
        test_rows = [r for r in test_rows if r["subject"] == args.subject]
        if not test_rows:
            print(f"no rows for subject {args.subject!r}", file=sys.stderr)
            return 2
    if args.limit is not None:
        # Stratify across subjects so --limit doesn't collapse to one subject.
        full_by_subject = index_by_subject(test_rows)
        n_subj = len(full_by_subject)
        per_subj_base = args.limit // n_subj
        remainder = args.limit % n_subj
        selected: list[dict] = []
        for i, (subj, rows) in enumerate(sorted(full_by_subject.items())):
            take = per_subj_base + (1 if i < remainder else 0)
            selected.extend(rows[:take])
        test_rows = selected

    by_subject = index_by_subject(test_rows)

    # Lazy import — only loads model when run.py actually executes
    from benchmarks._eval_common.mlx_direct import MLXDirectBackend
    print(f"  loading {args.model} via mlx_lm (this may take a minute)...")
    backend = MLXDirectBackend(args.model)
    print(f"  model loaded; tokenizer={type(backend.tokenizer).__name__}")

    # Resolve choice token IDs once
    choice_token_ids = backend.encode_choice_letters(LETTERS)
    print(f"  choice token ids: {choice_token_ids}")
    for L, ids in choice_token_ids.items():
        if not ids:
            print(
                f"  WARNING: no single-token encoding for choice {L!r}; "
                f"scoring may be unreliable",
                file=sys.stderr,
            )

    n_total = sum(len(v) for v in by_subject.values())
    n_done = 0
    n_correct = 0
    t0 = time.time()

    for subject, sub_rows in sorted(by_subject.items()):
        subj_dir = out_dir / subject
        subj_dir.mkdir(parents=True, exist_ok=True)
        fewshot = fewshot_for_subject(dev_rows, subject)

        for i, row in enumerate(sub_rows):
            item_path = subj_dir / f"q_{i:04d}.json"
            if args.resume and item_path.exists():
                cached = json.loads(item_path.read_text())
                n_correct += int(cached.get("correct", False))
                n_done += 1
                continue

            user_prompt = build_prompt(row, fewshot)
            # Apply chat template with enable_thinking=False. Default for Qwen3
            # opens an unclosed <think> block, so the model's next token is
            # thinking content (Here/Okay/...), pushing A/B/C/D off the
            # distribution. enable_thinking=False emits `<think>\n\n</think>\n\n`
            # so the next token slot is the answer.
            messages = [{"role": "user", "content": user_prompt}]
            try:
                prompt_text = backend.tokenizer.apply_chat_template(
                    messages,
                    tokenize=False,
                    add_generation_prompt=True,
                    enable_thinking=False,
                )
            except Exception:
                prompt_text = user_prompt  # fallback if tokenizer lacks chat template

            t_start = time.time()
            scores = backend.score_choices(prompt_text, LETTERS, top_k=args.top_k)
            wall_s = time.time() - t_start

            predicted = max(LETTERS, key=lambda L: scores[L])
            gold = LETTERS[row["answer"]]
            correct = predicted == gold

            record = {
                "subject": subject,
                "qid": i,
                "question": row["question"],
                "choices": row["choices"],
                "gold": gold,
                "predicted": predicted,
                "correct": correct,
                "scores": {L: (None if not math.isfinite(scores[L]) else scores[L]) for L in LETTERS},
                "wall_s": wall_s,
            }
            item_path.write_text(json.dumps(record, indent=2))
            n_correct += int(correct)
            n_done += 1

            if n_done % 50 == 0 or n_done == n_total:
                elapsed = time.time() - t0
                rate = elapsed / max(1, n_done)
                eta_m = (n_total - n_done) * rate / 60.0
                print(
                    f"  mmlu {n_done}/{n_total} acc={n_correct/n_done:.2%} "
                    f"avg={rate:.2f}s eta={eta_m:.1f}m"
                )

    # Aggregate
    items: list[dict] = []
    for subject in sorted(by_subject.keys()):
        subj_dir = out_dir / subject
        for p in sorted(subj_dir.glob("q_*.json")):
            items.append(json.loads(p.read_text()))
    summary_stats = aggregate(items)

    sampling = {"temperature": 0.0, "max_tokens": 1, "top_k_inspected": args.top_k}
    meta = build_run_meta(
        benchmark_protocol_version=BENCHMARK_PROTOCOL_VERSION,
        model_id=args.model,
        sampling=sampling,
        backend_kind="mlx_direct",
        context_window=getattr(backend.tokenizer, "model_max_length", 32768) or 32768,
        benchmark_dataset_sha256=sha256_file(test_path),
        scoring={
            "method": "first_token_top_logprob_via_mlx_direct",
            "choice_token_ids": choice_token_ids,
            "chat_template_applied": True,
        },
    )
    summary = {"meta": meta.to_dict(), "results": summary_stats}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print(
        f"MMLU — {summary_stats['count']} questions, "
        f"micro_acc={summary_stats['accuracy_micro']:.2%}, "
        f"macro_per_subject={summary_stats['accuracy_macro_per_subject']:.2%}, "
        f"per_category={summary_stats['per_category']}"
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m benchmarks.mmlu.run")
    p.add_argument("--output", required=True)
    p.add_argument("--test-data", default=None)
    p.add_argument("--dev-data", default=None)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--subject", default=None, help="Filter to one subject.")
    p.add_argument(
        "--model",
        default="mlx-community/Qwen3.6-35B-A3B-6bit",
        help="HF repo or local path passed to mlx_lm.load().",
    )
    p.add_argument("--top-k", type=int, default=50, help="top_logprobs depth inspected.")
    p.add_argument("--resume", action="store_true", default=True)
    p.add_argument("--no-resume", dest="resume", action="store_false")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
