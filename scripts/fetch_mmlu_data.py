"""Fetch MMLU dataset to ~/.luxe/mmlu-data/.

Source: cais/mmlu via HuggingFace datasets CDN (MIT license).

Usage:
  python scripts/fetch_mmlu_data.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmarks._eval_common.dataset import cache_dir, sha256_file  # noqa: E402

CACHE = cache_dir("mmlu")
# Expected counts as of the standard release.
EXPECTED_TEST_ROWS = 14042
EXPECTED_DEV_ROWS = 285  # 57 subjects × 5 dev examples


def main() -> int:
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: install with `pip install datasets`.", file=sys.stderr)
        return 2

    for split, expected in (("test", EXPECTED_TEST_ROWS), ("dev", EXPECTED_DEV_ROWS)):
        out = CACHE / f"{split}.jsonl"
        print(f"  fetching mmlu/{split} → {out}")
        ds = load_dataset("cais/mmlu", "all", split=split)
        with open(out, "w") as f:
            for row in ds:
                f.write(
                    json.dumps(
                        {
                            "question": row["question"],
                            "subject": row["subject"],
                            "choices": list(row["choices"]),
                            "answer": int(row["answer"]),
                        }
                    )
                    + "\n"
                )
        n = sum(1 for _ in open(out))
        sha = sha256_file(out)
        print(f"    {n} rows, sha256={sha[:16]}…")
        if n != expected:
            print(
                f"    WARNING: expected {expected} rows; dataset may have changed.",
                file=sys.stderr,
            )

    # Index test by subject for the runner's convenience.
    by_subject: dict[str, list[dict]] = defaultdict(list)
    for line in open(CACHE / "test.jsonl"):
        row = json.loads(line)
        by_subject[row["subject"]].append(row)
    print(f"  {len(by_subject)} subjects in test split")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
