"""Offline tests for the GSM8K adapter + grader. No Backend, no network."""
from __future__ import annotations

import pytest

from benchmarks.gsm8k.adapter import build_messages, extract_gold_answer
from benchmarks.gsm8k.grade import aggregate_items


class TestExtractGoldAnswer:
    def test_basic_gsm8k_answer_format(self):
        # Real GSM8K answers end with `#### N`
        answer = "Janet's ducks lay 16 - 3 - 4 = 9 eggs per day.\nShe sells them at $2 each, so she makes 9 * 2 = $18.\n#### 18"
        assert extract_gold_answer(answer) == 18.0

    def test_negative_gold(self):
        assert extract_gold_answer("Some reasoning\n#### -5") == -5.0

    def test_with_commas(self):
        assert extract_gold_answer("Big number\n#### 1,234") == 1234.0

    def test_unparseable_raises(self):
        with pytest.raises(ValueError, match="could not parse"):
            extract_gold_answer("no number here")


class TestBuildMessages:
    def test_message_structure(self):
        msgs = build_messages("If I have 5 apples and eat 2, how many remain?")
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        # Contains the 8 Wei et al. exemplars
        assert msgs[0]["content"].count("Q: ") == 9  # 8 exemplars + the test Q
        assert msgs[0]["content"].endswith("how many remain?\nA:")


class TestAggregateItems:
    def test_empty(self):
        assert aggregate_items([]) == {
            "count": 0,
            "accuracy": 0.0,
            "correct": 0,
            "failure_reasons": {},
        }

    def test_all_correct(self):
        items = [
            {"correct": True, "failure_reason": "none"},
            {"correct": True, "failure_reason": "none"},
        ]
        s = aggregate_items(items)
        assert s["count"] == 2
        assert s["correct"] == 2
        assert s["accuracy"] == 1.0
        assert s["parse_rate"] == 1.0
        assert s["failure_reasons"] == {"none": 2}

    def test_mixed_outcomes(self):
        items = [
            {"correct": True, "failure_reason": "none"},
            {"correct": False, "failure_reason": "none"},
            {"correct": False, "failure_reason": "no_answer_marker"},
            {"correct": False, "failure_reason": "think_only"},
        ]
        s = aggregate_items(items)
        assert s["count"] == 4
        assert s["correct"] == 1
        assert s["accuracy"] == 0.25
        assert s["parsed"] == 2
        assert s["parse_rate"] == 0.5
        assert s["failure_reasons"]["none"] == 2
        assert s["failure_reasons"]["no_answer_marker"] == 1
        assert s["failure_reasons"]["think_only"] == 1
