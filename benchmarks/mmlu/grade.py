"""Aggregate per-question MMLU results into per-subject + per-category + macro."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from benchmarks.mmlu.adapter import CATEGORIES, category_for


def aggregate(item_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute MMLU summary: overall accuracy, per-subject, per-category, macro avg."""
    if not item_records:
        return {"count": 0, "accuracy": 0.0}

    n = len(item_records)
    correct = sum(1 for r in item_records if r.get("correct"))

    by_subject: dict[str, list[bool]] = defaultdict(list)
    for r in item_records:
        by_subject[r["subject"]].append(bool(r.get("correct")))

    subject_acc = {s: sum(v) / len(v) for s, v in by_subject.items()}

    by_category: dict[str, list[bool]] = defaultdict(list)
    for s, v in by_subject.items():
        by_category[category_for(s)].extend(v)
    category_acc = {c: sum(v) / len(v) for c, v in by_category.items()}

    macro_avg = sum(subject_acc.values()) / len(subject_acc) if subject_acc else 0.0

    return {
        "count": n,
        "correct": correct,
        "accuracy_micro": correct / n,
        "accuracy_macro_per_subject": macro_avg,
        "per_subject": dict(sorted(subject_acc.items())),
        "per_subject_n": {s: len(v) for s, v in by_subject.items()},
        "per_category": category_acc,
        "n_subjects_seen": len(subject_acc),
    }
