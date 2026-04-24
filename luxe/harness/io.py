"""JSONL results logging. Append-only, resumable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

RESULTS_ROOT = Path(__file__).resolve().parent.parent / "results"


def runs_path(phase: str, candidate_id: str, config_id: str, benchmark: str) -> Path:
    return RESULTS_ROOT / "runs" / phase / candidate_id / config_id / f"{benchmark}.jsonl"


def append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=_default) + "\n")


def read(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def completed_task_ids(path: Path) -> set[str]:
    """Task IDs already recorded in this run file — used to resume without re-running."""
    return {rec.get("task_id") for rec in read(path) if rec.get("task_id")}


def _default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)
