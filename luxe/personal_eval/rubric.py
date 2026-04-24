"""Scoring utilities for Phase B review (B1) and write (B2) replay."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class ReviewScore:
    precision: float
    recall: float
    f1: float
    matched_issues: list[str]
    missed_issues: list[str]
    false_positives: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "matched": len(self.matched_issues),
            "missed": len(self.missed_issues),
            "false_positives": len(self.false_positives),
        }


def score_review(
    model_comments: list[str],
    human_comments: list[str],
    *,
    similarity_threshold: float = 0.45,
) -> ReviewScore:
    """Fuzzy-match model comments against human review comments.

    For each human comment, consider it "matched" if any model comment has
    >= similarity_threshold sequence ratio with it. This is a stand-in for
    LLM-judged semantic equivalence; good enough for directional signal.
    """
    matched: list[str] = []
    missed: list[str] = []
    used_model_idx: set[int] = set()

    for human in human_comments:
        best = -1.0
        best_idx = -1
        for i, m in enumerate(model_comments):
            if i in used_model_idx:
                continue
            ratio = difflib.SequenceMatcher(a=_norm(human), b=_norm(m)).ratio()
            if ratio > best:
                best, best_idx = ratio, i
        if best >= similarity_threshold and best_idx >= 0:
            matched.append(human)
            used_model_idx.add(best_idx)
        else:
            missed.append(human)

    false_positives = [m for i, m in enumerate(model_comments) if i not in used_model_idx]

    tp = len(matched)
    fp = len(false_positives)
    fn = len(missed)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return ReviewScore(
        precision=precision,
        recall=recall,
        f1=f1,
        matched_issues=matched,
        missed_issues=missed,
        false_positives=false_positives,
    )


def score_diff_similarity(model_diff: str, gold_diff: str) -> float:
    """Ratio of changed lines in model_diff that also appear in gold_diff.

    Crude but directional: we want to know "did the model touch roughly the
    same places as the human?" Not a substitute for tests passing.
    """
    model_changes = _extract_change_lines(model_diff)
    gold_changes = _extract_change_lines(gold_diff)
    if not gold_changes:
        return 0.0
    overlap = len(model_changes & gold_changes)
    return overlap / len(gold_changes)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _extract_change_lines(diff: str) -> set[str]:
    lines: set[str] = set()
    for raw in diff.splitlines():
        if raw.startswith(("+++", "---")):
            continue
        if raw.startswith(("+", "-")) and len(raw) > 1:
            lines.add(raw[1:].strip())
    return lines
