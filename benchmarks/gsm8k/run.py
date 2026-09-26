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
from benchmarks._eval_common.store import ResultStore  # noqa: E402
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

    store = ResultStore(out_dir, model=args.model, resume=args.resume)
    n_total = len(rows)
    n_done = 0
    n_cached = 0
    n_correct = 0
    n_infra = 0
    t0 = time.time()
    selected: list[tuple[str, str]] = []

    for i, row in enumerate(rows):
        item_id = f"item_{i:05d}"
        fp = row["question"]
        selected.append((item_id, fp))
        cached = store.get(item_id, fp)
        if cached is not None:
            n_cached += 1
            n_correct += int(cached.get("correct", False))
            continue

        question = row["question"]
        gold = extract_gold_answer(row["answer"])
        messages = build_messages(question, think=args.think)

        t_start = time.time()
        try:
            resp = backend.chat(
                messages=messages,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                num_ctx=args.num_ctx,
            )
        except Exception as e:  # noqa: BLE001 — infra, not an answer
            # No extraction from the error text (it can carry digits — a
            # status code, a port — that "answered" the question), no cache.
            store.put_infra_error(item_id, f"{type(e).__name__}: {e}", fp,
                                  qid=i, wall_s=time.time() - t_start)
            n_infra += 1
            print(f"  gsm8k item {i}: INFRA ERROR {type(e).__name__}: {e}"[:300])
            continue
        raw_output = resp.text or ""
        # Token counts live on resp.timing (ChatResponse has no top-level
        # prompt_tokens — the getattr fallback always recorded 0).
        prompt_toks = int(resp.timing.prompt_tokens or 0)
        completion_toks = int(resp.timing.completion_tokens or 0)
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
        store.put(item_id, record, fp)
        n_done += 1
        n_correct += int(correct)

        if (i + 1) % 10 == 0 or (i + 1) == n_total:
            elapsed = time.time() - t0
            done = n_done + n_cached
            rate = elapsed / max(1, done)
            eta_m = (n_total - done) * rate / 60.0
            print(
                f"  gsm8k {done}/{n_total} acc={n_correct/max(1, done):.2%} "
                f"avg={rate:.1f}s eta={eta_m:.1f}m"
            )

    got = store.collect(selected)
    summary_stats = aggregate_items(got.records)
    summary_stats["infra_errors"] = got.infra_errors

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
        + (f", infra_errors={got.infra_errors} (excluded, re-run to retry)"
           if got.infra_errors else "")
    )
    return 1 if got.infra_errors else 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m benchmarks.gsm8k.run")
    p.add_argument("--output", required=True, help="Output directory.")
    p.add_argument("--data", default=None, help=f"GSM8K test JSONL (default: {DEFAULT_DATA_PATH}).")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--model", default="Qwen3.6-35B-A3B-6bit")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--temperature", type=float, default=0.0)
    # 4096 fits a full Qwen3 think+answer pass; 512 truncates mid-think
    # at temperature=0 on word problems (verified via calibration).
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--num-ctx", type=int, default=8192)
    p.add_argument("--resume", action="store_true", default=True, help="Reuse items already graded under this --model (default true).")
    p.add_argument("--no-resume", dest="resume", action="store_false")
    p.add_argument("--think", action="store_true", default=True, help="Allow <think> blocks (Qwen3 default).")
    p.add_argument("--no-think", dest="think", action="store_false")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
