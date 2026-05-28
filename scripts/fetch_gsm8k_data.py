"""Fetch GSM8K dataset to ~/.luxe/gsm8k-data/.

Source: openai/gsm8k via HuggingFace datasets CDN (MIT license).

Usage:
  python scripts/fetch_gsm8k_data.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmarks._eval_common.dataset import cache_dir, sha256_file  # noqa: E402

CACHE = cache_dir("gsm8k")
EXPECTED_TEST_ROWS = 1319
EXPECTED_TRAIN_ROWS = 7473


def main() -> int:
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: install with `pip install datasets`.", file=sys.stderr)
        return 2

    for split, expected in (("test", EXPECTED_TEST_ROWS), ("train", EXPECTED_TRAIN_ROWS)):
        out = CACHE / f"{split}.jsonl"
        print(f"  fetching gsm8k/{split} → {out}")
        ds = load_dataset("openai/gsm8k", "main", split=split)
        with open(out, "w") as f:
            for row in ds:
                f.write(json.dumps({"question": row["question"], "answer": row["answer"]}) + "\n")
        n = sum(1 for _ in open(out))
        sha = sha256_file(out)
        print(f"    {n} rows, sha256={sha[:16]}…")
        if n != expected:
            print(
                f"    WARNING: expected {expected} rows; dataset may have changed.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
