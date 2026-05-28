"""Fetch ARC-Challenge dataset to ~/.luxe/arc-data/.

Source: allenai/ai2_arc (ARC-Challenge subset) via HuggingFace datasets CDN
(CC BY-SA 4.0).

Usage:
  python scripts/fetch_arc_data.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmarks._eval_common.dataset import cache_dir, sha256_file  # noqa: E402

CACHE = cache_dir("arc")
EXPECTED_CHALLENGE_TEST = 1172


def main() -> int:
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: install with `pip install datasets`.", file=sys.stderr)
        return 2

    out = CACHE / "challenge_test.jsonl"
    print(f"  fetching ai2_arc/ARC-Challenge/test → {out}")
    ds = load_dataset("allenai/ai2_arc", "ARC-Challenge", split="test")
    with open(out, "w") as f:
        for row in ds:
            f.write(
                json.dumps(
                    {
                        "id": row["id"],
                        "question": row["question"],
                        "choices": {
                            "text": list(row["choices"]["text"]),
                            "label": list(row["choices"]["label"]),
                        },
                        "answerKey": row["answerKey"],
                    }
                )
                + "\n"
            )
    n = sum(1 for _ in open(out))
    sha = sha256_file(out)
    print(f"    {n} rows, sha256={sha[:16]}…")
    if n != EXPECTED_CHALLENGE_TEST:
        print(
            f"    WARNING: expected {EXPECTED_CHALLENGE_TEST} rows; "
            f"dataset may have changed.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
