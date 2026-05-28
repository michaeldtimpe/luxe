"""Offline tests for ARC-Challenge adapter + grader."""
from __future__ import annotations

import pytest

from benchmarks.arc_challenge.adapter import (
    LETTERS,
    build_prompt,
    gold_letter,
    valid_letters_for,
)
from benchmarks.arc_challenge.grade import aggregate


class TestGoldLetter:
    def test_letter_labels_aligned(self):
        # Standard A/B/C/D-labeled ARC row
        row = {
            "choices": {"label": ["A", "B", "C", "D"], "text": ["w", "x", "y", "z"]},
            "answerKey": "C",
        }
        assert gold_letter(row) == "C"

    def test_numeric_labels_remapped(self):
        # ARC sometimes uses 1/2/3/4 labels. Gold remapped to our LETTERS by position.
        row = {
            "choices": {"label": ["1", "2", "3", "4"], "text": ["w", "x", "y", "z"]},
            "answerKey": "2",
        }
        assert gold_letter(row) == "B"  # position 1 in LETTERS = "B"

    def test_three_options_5th_letter_not_used(self):
        row = {
            "choices": {"label": ["A", "B", "C"], "text": ["x", "y", "z"]},
            "answerKey": "A",
        }
        assert gold_letter(row) == "A"


class TestValidLettersFor:
    def test_three_choices(self):
        row = {"choices": {"label": ["A", "B", "C"], "text": ["x", "y", "z"]}}
        assert valid_letters_for(row) == ("A", "B", "C")

    def test_five_choices(self):
        row = {"choices": {"label": ["A", "B", "C", "D", "E"], "text": ["v", "w", "x", "y", "z"]}}
        assert valid_letters_for(row) == ("A", "B", "C", "D", "E")


class TestBuildPrompt:
    def test_prompt_includes_question_and_options(self):
        row = {
            "id": "Mercury_7081", "question": "Which is a plant?",
            "choices": {"label": ["A", "B", "C", "D"], "text": ["Rock", "Tree", "Fish", "Cloud"]},
            "answerKey": "B",
        }
        p = build_prompt(row)
        assert "Which is a plant?" in p
        assert "A. Rock" in p
        assert "B. Tree" in p
        assert "D. Cloud" in p
        assert p.endswith("Answer:")

    def test_three_choice_prompt(self):
        row = {
            "id": "x", "question": "Q?",
            "choices": {"label": ["1", "2", "3"], "text": ["a", "b", "c"]},
            "answerKey": "1",
        }
        p = build_prompt(row)
        assert "A. a" in p
        assert "C. c" in p
        assert "D." not in p


class TestAggregate:
    def test_empty(self):
        assert aggregate([]) == {"count": 0, "accuracy": 0.0}

    def test_mixed_choice_counts(self):
        items = [
            {"correct": True, "n_choices": 4},
            {"correct": True, "n_choices": 4},
            {"correct": False, "n_choices": 4},
            {"correct": True, "n_choices": 3},
            {"correct": False, "n_choices": 3},
            {"correct": True, "n_choices": 5},
        ]
        s = aggregate(items)
        assert s["count"] == 6
        assert s["correct"] == 4
        assert s["accuracy"] == pytest.approx(4 / 6)
        assert s["per_choice_count"]["4"] == {"n": 3, "accuracy": pytest.approx(2 / 3)}
        assert s["per_choice_count"]["3"] == {"n": 2, "accuracy": 0.5}
        assert s["per_choice_count"]["5"] == {"n": 1, "accuracy": 1.0}
