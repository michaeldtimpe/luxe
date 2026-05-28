"""One-shot CodeNeedle manifest builder.

Enumerates candidate functions in each fixture, samples k=16 with seed=42,
and freezes the (name, language, primary_lines, bonus_lines) into a
committed manifest. Runs deterministically.

Bumping the manifest = bumping codeneedle/v1 → v2 (old scores not comparable).
Re-run only when you intentionally want to refresh the needle set.

Usage:
  python scripts/build_codeneedle_manifest.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from benchmarks._eval_common.dataset import sha256_file  # noqa: E402
from benchmarks.codeneedle.upstream.extract import (  # noqa: E402
    MIN_BODY_LINES,
    BONUS_CAP,
    extract,
)

SEED = 42
K = 16
FIXTURES = (
    REPO_ROOT / "benchmarks/codeneedle/fixtures/http_server.py",
    REPO_ROOT / "benchmarks/codeneedle/fixtures/jquery.js",
)
OUT = REPO_ROOT / "benchmarks/codeneedle/manifest.json"


def main() -> int:
    corpora = []
    for path in FIXTURES:
        if not path.exists():
            print(f"  fixture missing: {path}", file=sys.stderr)
            return 2
        targets = extract(path)
        if len(targets) < K:
            print(
                f"  WARNING: {path.name} only has {len(targets)} eligible functions "
                f"(MIN_BODY_LINES={MIN_BODY_LINES}); sampling all of them",
                file=sys.stderr,
            )
        rng = random.Random(SEED)
        sampled = rng.sample(targets, min(K, len(targets)))
        corpus_entry = {
            "corpus_name": path.name,
            "corpus_path": str(path.relative_to(REPO_ROOT)),
            "corpus_sha256": sha256_file(path),
            "language": targets[0].language if targets else None,
            "seed": SEED,
            "k_sampled": len(sampled),
            "k_target": K,
            "min_body_lines": MIN_BODY_LINES,
            "bonus_cap": BONUS_CAP,
            "functions": [
                {
                    "name": t.name,
                    "start_line": t.start_line,
                    "primary_lines": list(t.primary_lines),
                    "bonus_lines": list(t.bonus_lines),
                }
                for t in sampled
            ],
        }
        corpora.append(corpus_entry)
        print(
            f"  {path.name}: enumerated {len(targets)} fns, "
            f"sampled {len(sampled)}, sha256={corpus_entry['corpus_sha256'][:16]}…"
        )

    manifest = {
        "schema_version": 1,
        "protocol_version": "codeneedle/v1",
        "corpora": corpora,
    }
    OUT.write_text(json.dumps(manifest, indent=2))
    print(f"  wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
