"""Split a `cohort_shift_3x3.py --snapshot-out` JSONL into per-instance
JSON files for Stage 3's prior loader (`~/.luxe/cohort-history/`).

This completes the Stage 2 → Stage 3 closed loop:
  Stage 2: cohort_shift_3x3.py --snapshot-out snapshot.jsonl
  this:    split_cohort_snapshot.py snapshot.jsonl
  Stage 3: LUXE_LOAD_PRIORS=1 + LUXE_INSTANCE_ID → load_prior_from_env()
            returns the per-instance verdict at run start

Usage:
    python -m scripts.split_cohort_snapshot \\
        --snapshot /tmp/cohort_test/snapshot.jsonl \\
        [--out-dir ~/.luxe/cohort-history/]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True,
                    help="Path to JSONL snapshot from cohort_shift_3x3.py")
    ap.add_argument("--out-dir", type=Path,
                    default=Path.home() / ".luxe" / "cohort-history",
                    help="Per-instance JSON output directory")
    args = ap.parse_args(argv)

    if not args.snapshot.is_file():
        print(f"FATAL: snapshot file not found: {args.snapshot}", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    with args.snapshot.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            iid = row.get("instance_id")
            if not iid:
                continue
            (args.out_dir / f"{iid}.json").write_text(json.dumps(row, indent=2))
            n += 1
    print(f"wrote {n} per-instance prior files to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
