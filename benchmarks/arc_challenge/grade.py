"""Aggregate ARC-Challenge per-question results."""
from __future__ import annotations

from collections import Counter
from typing import Any


def aggregate(item_records: list[dict[str, Any]]) -> dict[str, Any]:
    if not item_records:
        return {"count": 0, "accuracy": 0.0}

    n = len(item_records)
    correct = sum(1 for r in item_records if r.get("correct"))

    # Breakdown by choice-count (3 vs 4 vs 5)
    by_choice_count: dict[int, list[bool]] = {}
    for r in item_records:
        k = int(r.get("n_choices", 4))
        by_choice_count.setdefault(k, []).append(bool(r.get("correct")))

    return {
        "count": n,
        "correct": correct,
        "accuracy": correct / n,
        "per_choice_count": {
            str(k): {
                "n": len(v),
                "accuracy": sum(v) / len(v) if v else 0.0,
            }
            for k, v in sorted(by_choice_count.items())
        },
    }
