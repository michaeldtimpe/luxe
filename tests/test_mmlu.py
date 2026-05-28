"""Offline tests for MMLU adapter + grader. No model, no network."""
from __future__ import annotations

import pytest

from benchmarks.mmlu.adapter import (
    CATEGORIES,
    LETTERS,
    build_prompt,
    category_for,
    fewshot_for_subject,
    index_by_subject,
)
from benchmarks.mmlu.grade import aggregate


class TestIndexBySubject:
    def test_groups_rows(self):
        rows = [
            {"subject": "anatomy", "question": "q1"},
            {"subject": "anatomy", "question": "q2"},
            {"subject": "astronomy", "question": "q3"},
        ]
        out = index_by_subject(rows)
        assert set(out.keys()) == {"anatomy", "astronomy"}
        assert len(out["anatomy"]) == 2


class TestFewshotForSubject:
    def test_filters_and_samples_deterministically(self):
        dev = [
            {"subject": "anatomy", "question": f"q{i}", "choices": ["a", "b", "c", "d"], "answer": i % 4}
            for i in range(10)
        ] + [
            {"subject": "astronomy", "question": "other", "choices": ["x", "y", "z", "w"], "answer": 1}
        ]
        out = fewshot_for_subject(dev, "anatomy", k=5)
        assert len(out) == 5
        # Same seed ⇒ same result
        again = fewshot_for_subject(dev, "anatomy", k=5)
        assert out == again
        # Only anatomy
        questions = [t[0] for t in out]
        assert all(q.startswith("q") for q in questions)
        # gold letter is one of A-D
        for _, _, ans in out:
            assert ans in ("A", "B", "C", "D")


class TestBuildPrompt:
    def test_basic_prompt_structure(self):
        row = {
            "subject": "high_school_biology",
            "question": "What is photosynthesis?",
            "choices": ["A photo", "Plant food-making", "Animal breathing", "None"],
            "answer": 1,
        }
        out = build_prompt(row, fewshot=[])
        assert "high school biology" in out  # underscore → space in instruction
        assert "What is photosynthesis?" in out
        assert "A. A photo" in out
        assert "D. None" in out
        assert out.endswith("Answer:")

    def test_with_fewshot_examples(self):
        row = {"subject": "anatomy", "question": "test_q", "choices": ["a", "b", "c", "d"], "answer": 0}
        fewshot = [
            ("ex_q1", ["w", "x", "y", "z"], "A"),
            ("ex_q2", ["a", "b", "c", "d"], "B"),
        ]
        out = build_prompt(row, fewshot)
        # 2 fewshot examples + 1 test = 3 "Answer:" markers
        assert out.count("Answer:") == 3


class TestCategoryFor:
    def test_known_subjects(self):
        assert category_for("anatomy") == "STEM"
        assert category_for("philosophy") == "humanities"
        assert category_for("sociology") == "social_sciences"
        assert category_for("nutrition") == "other"

    def test_unknown_subject(self):
        assert category_for("dragon_riding_history") == "uncategorized"

    def test_all_57_subjects_categorized(self):
        # If MMLU adds new subjects, this would catch missing categorization
        total = sum(len(v) for v in CATEGORIES.values())
        assert total == 57, f"expected 57 categorized subjects, got {total}"


class TestAggregate:
    def test_empty(self):
        assert aggregate([]) == {"count": 0, "accuracy": 0.0}

    def test_basic_per_subject_and_macro(self):
        items = [
            {"subject": "anatomy", "correct": True},
            {"subject": "anatomy", "correct": True},
            {"subject": "anatomy", "correct": False},
            {"subject": "philosophy", "correct": True},
            {"subject": "philosophy", "correct": False},
        ]
        s = aggregate(items)
        assert s["count"] == 5
        assert s["correct"] == 3
        assert s["accuracy_micro"] == pytest.approx(3 / 5)
        assert s["per_subject"]["anatomy"] == pytest.approx(2 / 3)
        assert s["per_subject"]["philosophy"] == pytest.approx(1 / 2)
        # macro = avg of subject accuracies
        assert s["accuracy_macro_per_subject"] == pytest.approx((2 / 3 + 1 / 2) / 2)
        assert s["per_category"]["STEM"] == pytest.approx(2 / 3)
        assert s["per_category"]["humanities"] == pytest.approx(1 / 2)
        assert s["n_subjects_seen"] == 2
