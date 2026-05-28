"""Aggregate per-function CodeNeedle results into a summary."""
from __future__ import annotations

from typing import Any


def aggregate_items(item_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary statistics from per-function scoring records.

    Each record is the to_dict() of a vendored upstream FunctionScore plus
    a few diagnostic fields. Pure function; offline-testable.
    """
    n = len(item_records)
    if n == 0:
        return {"count": 0, "pass_rate": 0.0}

    passed = sum(1 for r in item_records if r.get("passed"))
    primary_matched_total = sum(int(r.get("primary_matched", 0)) for r in item_records)
    primary_total = sum(int(r.get("primary_total", 0)) for r in item_records)
    bonus_matched_total = sum(int(r.get("bonus_matched", 0)) for r in item_records)
    hallucinated_total = sum(int(r.get("hallucinated", 0)) for r in item_records)
    errored = sum(1 for r in item_records if r.get("error"))

    # Per-position curve over the primary 20 lines
    primary_curve: list[int] = [0] * 20
    primary_curve_n = 0
    for r in item_records:
        tagged = r.get("expected_tagged", [])
        if len(tagged) != 20:
            continue
        primary_curve_n += 1
        for i, t in enumerate(tagged):
            if isinstance(t, dict) and t.get("tag") == "matched":
                primary_curve[i] += 1

    return {
        "count": n,
        "passed": passed,
        "pass_rate": passed / n,
        "errored": errored,
        "primary_matched": primary_matched_total,
        "primary_total": primary_total,
        "primary_match_rate": (
            primary_matched_total / primary_total if primary_total else 0.0
        ),
        "bonus_matched": bonus_matched_total,
        "hallucinated": hallucinated_total,
        "primary_accuracy_curve": [
            (primary_curve[i] / primary_curve_n) if primary_curve_n else 0.0
            for i in range(20)
        ],
    }
