"""Dataset cache, integrity verification, and JSONL streaming."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterator


def cache_dir(benchmark_name: str) -> Path:
    """Per-benchmark vendored-data directory under ~/.luxe/<name>-data/."""
    d = Path.home() / ".luxe" / f"{benchmark_name}-data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def sha256_verify(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(
            f"SHA-256 mismatch for {path}: expected {expected}, got {actual}"
        )


def jsonl_load(path: Path) -> Iterator[dict]:
    with open(path) as f:
        for line in f:
            s = line.strip()
            if s:
                yield json.loads(s)
