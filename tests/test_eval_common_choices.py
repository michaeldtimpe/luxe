"""Offline unit tests for benchmarks/_eval_common/choices.py."""
from __future__ import annotations

import pytest

from benchmarks._eval_common.choices import format_mc_prompt


class TestFormatMcPrompt:
    def test_basic_4_options(self):
        out = format_mc_prompt("What is 2+2?", ["3", "4", "5", "6"])
        assert "What is 2+2?" in out
        assert "A. 3" in out
        assert "D. 6" in out
        assert out.endswith("Answer:")
        assert "\nAnswer: " not in out  # no trailing space after Answer:

    def test_3_options(self):
        out = format_mc_prompt("Pick.", ["x", "y", "z"])
        assert "A. x" in out
        assert "C. z" in out
        assert "D." not in out

    def test_5_options_arc_style(self):
        out = format_mc_prompt("Pick.", ["a", "b", "c", "d", "e"])
        assert "E. e" in out

    def test_too_many_options_raises(self):
        with pytest.raises(ValueError, match="Too many options"):
            format_mc_prompt("?", ["1", "2", "3", "4", "5", "6"])

    def test_with_fewshot_examples(self):
        examples = [
            ("Cap of France?", ["London", "Paris", "Rome"], "B"),
            ("2+2?", ["3", "4", "5"], "B"),
        ]
        out = format_mc_prompt(
            "Test question?",
            ["a", "b", "c"],
            fewshot_examples=examples,
        )
        assert out.count("Answer:") == 3  # 2 examples + 1 final
        # Examples appear before the test question
        assert out.index("Cap of France?") < out.index("Test question?")
        assert out.index("Test question?") > out.index("Answer: B")

    def test_with_instruction(self):
        out = format_mc_prompt(
            "Q?",
            ["a", "b"],
            instruction="Answer concisely with a single letter.",
        )
        assert out.startswith("Answer concisely with a single letter.")

    def test_deterministic_output(self):
        # Same inputs => byte-identical output (important for the golden fixture)
        a = format_mc_prompt("Q?", ["x", "y"])
        b = format_mc_prompt("Q?", ["x", "y"])
        assert a == b
