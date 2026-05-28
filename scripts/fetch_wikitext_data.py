"""Fetch WikiText-103 test split to ~/.luxe/wikitext-data/.

Source: Salesforce/wikitext, config wikitext-103-raw-v1 (CC BY-SA 3.0).
Stored as a single concatenated UTF-8 text file (the standard form for
sliding-window perplexity).

Usage:
  python scripts/fetch_wikitext_data.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmarks._eval_common.dataset import cache_dir, sha256_file  # noqa: E402

CACHE = cache_dir("wikitext")
OUT = CACHE / "wikitext-103-raw-test.txt"


def main() -> int:
    try:
        from datasets import load_dataset
    except ImportError:
        print("ERROR: install with `pip install datasets`.", file=sys.stderr)
        return 2

    print(f"  fetching wikitext-103-raw test → {OUT}")
    ds = load_dataset("Salesforce/wikitext", "wikitext-103-raw-v1", split="test")
    # Standard format: concatenate non-empty rows separated by single newlines.
    # WikiText already has section headers / blank-line structure embedded
    # in the row text, so a plain join is correct.
    text = "\n".join(row["text"] for row in ds)
    OUT.write_text(text)
    sha = sha256_file(OUT)
    print(f"    {len(text):,} chars, {len(text.encode('utf-8')):,} bytes, sha256={sha[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
