"""GSM8K runner.

Usage:
  python -m benchmarks.gsm8k.run --output acceptance/gsm8k/<run_id> [--limit N]

Per-question JSON lands at <output>/item_<qid>.json (resumable).
summary.json lands at <output>/summary.json with full metadata block.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from luxe.backend import Backend  # noqa: E402

from benchmarks._eval_common.dataset import (  # noqa: E402
    cache_dir,
    jsonl_load,
    sha256_file,
)
from benchmarks._eval_common.meta import build_run_meta  # noqa: E402
from benchmarks.gsm8k.adapter import (  # noqa: E402
    build_messages,
    extract_gold_answer,
)
from benchmarks._eval_common.extract import extract_gsm8k_answer  # noqa: E402
from benchmarks.gsm8k.grade import aggregate_items  # noqa: E402


BENCHMARK_PROTOCOL_VERSION = "gsm8k/v1"
DEFAULT_DATA_PATH = cache_dir("gsm8k") / "test.jsonl"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    data_path = Path(args.data) if args.data else DEFAULT_DATA_PATH
    if not data_path.exists():
        print(
            f"GSM8K data not found at {data_path}. Run "
            f"`python scripts/fetch_gsm8k_data.py` first.",
            file=sys.stderr,
        )
        return 2

    rows = list(jsonl_load(data_path))
    if args.limit is not None:
        rows = rows[: args.limit]

    backend = Backend(base_url=args.base_url, model=args.model)

    n_total = len(rows)
    n_done = 0
    n_cached = 0
    n_correct = 0
    t0 = time.time()

    for i, row in enumerate(rows):
        item_path = out_dir / f"item_{i:05d}.json"
        if args.resume and item_path.exists():
            cached = json.loads(item_path.read_text())
            n_cached += 1
            n_correct += int(cached.get("correct", False))
            continue

        question = row["question"]
        gold = extract_gold_answer(row["answer"])
        messages = build_messages(question)

        t_start = time.time()
        try:
            resp = backend.chat(
                messages=messages,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                num_ctx=args.num_ctx,
            )
            raw_output = resp.text or ""
            prompt_toks = int(getattr(resp, "prompt_tokens", 0) or 0)
            completion_toks = int(getattr(resp, "completion_tokens", 0) or 0)
        except Exception as e:
            raw_output = f"<ERROR: {type(e).__name__}: {e}>"
            prompt_toks = 0
            completion_toks = 0
        wall_s = time.time() - t_start

        extracted, reason = extract_gsm8k_answer(raw_output)
        correct = extracted is not None and math.isclose(extracted, gold, rel_tol=1e-9, abs_tol=1e-9)

        record = {
            "qid": i,
            "question": question,
            "gold_answer": gold,
            "raw_output": raw_output,
            "extracted_answer": extracted,
            "failure_reason": reason,
            "correct": correct,
            "wall_s": wall_s,
            "prompt_tokens": prompt_toks,
            "completion_tokens": completion_toks,
        }
        item_path.write_text(json.dumps(record, indent=2))
        n_done += 1
        n_correct += int(correct)

        if (i + 1) % 10 == 0 or (i + 1) == n_total:
            elapsed = time.time() - t0
            done = n_done + n_cached
            rate = elapsed / max(1, done)
            eta_m = (n_total - done) * rate / 60.0
            print(
                f"  gsm8k {done}/{n_total} acc={n_correct/done:.2%} "
                f"avg={rate:.1f}s eta={eta_m:.1f}m"
            )

    item_records = []
    for p in sorted(out_dir.glob("item_*.json")):
        item_records.append(json.loads(p.read_text()))
    summary_stats = aggregate_items(item_records)

    sampling = {
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "num_ctx": args.num_ctx,
        "think_mode": args.think,
    }
    meta = build_run_meta(
        benchmark_protocol_version=BENCHMARK_PROTOCOL_VERSION,
        model_id=args.model,
        sampling=sampling,
        backend_kind="http",
        context_window=args.num_ctx,
        backend_base_url=args.base_url,
        benchmark_dataset_sha256=sha256_file(data_path),
        scoring={"method": "generation+extract_gsm8k_answer", "fewshot": "8shot_cot_wei_et_al"},
    )
    summary = {"meta": meta.to_dict(), "results": summary_stats}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(
        f"GSM8K — {summary_stats['count']} questions, "
        f"acc={summary_stats['accuracy']:.2%}, "
        f"parse_rate={summary_stats['parse_rate']:.2%}, "
        f"failure_reasons={summary_stats['failure_reasons']}"
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m benchmarks.gsm8k.run")
    p.add_argument("--output", required=True, help="Output directory.")
    p.add_argument("--data", default=None, help=f"GSM8K test JSONL (default: {DEFAULT_DATA_PATH}).")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--model", default="Qwen3.6-35B-A3B-6bit")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument("--resume", action="store_true", default=True, help="Skip items with existing JSON (default true).")
    p.add_argument("--no-resume", dest="resume", action="store_false")
    p.add_argument("--think", action="store_true", default=True, help="Allow <think> blocks (Qwen3 default).")
    p.add_argument("--no-think", dest="think", action="store_false")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
