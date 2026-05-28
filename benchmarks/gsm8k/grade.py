"""Aggregate per-item GSM8K JSONs → summary stats."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from benchmarks._eval_common.dataset import jsonl_load


def aggregate_items(item_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary statistics from a list of per-item result dicts.

    Pure function; offline-testable.
    """
    n = len(item_records)
    if n == 0:
        return {
            "count": 0,
            "accuracy": 0.0,
            "correct": 0,
            "failure_reasons": {},
        }

    correct = sum(1 for r in item_records if r.get("correct"))
    reasons = Counter(r.get("failure_reason", "unknown") for r in item_records)
    parsed = sum(1 for r in item_records if r.get("failure_reason") == "none")
    return {
        "count": n,
        "correct": correct,
        "accuracy": correct / n,
        "parsed": parsed,
        "parse_rate": parsed / n,
        "failure_reasons": dict(reasons),
    }


def load_results_dir(results_dir: Path) -> list[dict[str, Any]]:
    """Load all per-item JSON files in a results dir (one per question)."""
    import json

    out: list[dict[str, Any]] = []
    for p in sorted(results_dir.glob("item_*.json")):
        out.append(json.loads(p.read_text()))
    return out
