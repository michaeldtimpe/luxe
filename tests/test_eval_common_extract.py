"""Offline unit tests for benchmarks/_eval_common/extract.py."""
from __future__ import annotations

import pytest

from benchmarks._eval_common.extract import (
    extract_choice_letter,
    extract_gsm8k_answer,
    strip_think_blocks,
)


class TestStripThinkBlocks:
    def test_strips_single_block(self):
        assert strip_think_blocks("foo <think>blah</think> bar") == "foo  bar"

    def test_strips_multiple_blocks(self):
        text = "a <think>x</think> b <think>y</think> c"
        assert strip_think_blocks(text) == "a  b  c"

    def test_strips_multiline_block(self):
        text = "before <think>\nline 1\nline 2\n#### 99\n</think> after #### 42"
        assert strip_think_blocks(text) == "before  after #### 42"

    def test_passthrough_when_absent(self):
        assert strip_think_blocks("plain text") == "plain text"


class TestExtractGsm8kAnswer:
    def test_hash_marker(self):
        assert extract_gsm8k_answer("Working it out... #### 42") == (42.0, "none")

    def test_hash_marker_preferred_over_answer_is(self):
        # `####` is canonical — it wins over "answer is X" if both present
        out = "The answer is 7. So we conclude. #### 42"
        assert extract_gsm8k_answer(out) == (42.0, "none")

    def test_answer_is_pattern(self):
        assert extract_gsm8k_answer("So the answer is 33.") == (33.0, "none")

    def test_answer_is_with_dollar(self):
        assert extract_gsm8k_answer("the answer is $8") == (8.0, "none")

    def test_last_number_fallback(self):
        out = "Step 1: 10 cars. Step 2: 5 leave. Result: 5"
        assert extract_gsm8k_answer(out) == (5.0, "none")

    def test_commas_in_number(self):
        assert extract_gsm8k_answer("#### 1,234") == (1234.0, "none")

    def test_negative(self):
        assert extract_gsm8k_answer("#### -7") == (-7.0, "none")

    def test_decimal(self):
        assert extract_gsm8k_answer("#### 3.14") == (3.14, "none")

    def test_think_block_stripped_before_extraction(self):
        # `#### 99` inside <think> should be ignored; the real answer comes after
        out = "<think>I'll guess #### 99</think>\nActually, the answer is 42."
        assert extract_gsm8k_answer(out) == (42.0, "none")

    def test_think_only(self):
        out = "<think>just thinking</think>"
        assert extract_gsm8k_answer(out) == (None, "think_only")

    def test_no_marker_no_numbers(self):
        assert extract_gsm8k_answer("I have no idea.") == (None, "no_answer_marker")

    def test_non_numeric_after_hash(self):
        # `####` followed by non-number — falls through to last-number, which
        # also won't find one. Should be no_answer_marker.
        assert extract_gsm8k_answer("#### abc") == (None, "no_answer_marker")


class TestExtractChoiceLetter:
    def test_bare_letter(self):
        assert extract_choice_letter("B") == "B"

    def test_parenthesized(self):
        assert extract_choice_letter("(C)") == "C"

    def test_letter_with_period(self):
        assert extract_choice_letter("D.") == "D"

    def test_answer_is_pattern(self):
        assert extract_choice_letter("The answer is B.") == "B"

    def test_answer_colon_pattern(self):
        assert extract_choice_letter("Answer: A") == "A"

    def test_answer_with_parens(self):
        assert extract_choice_letter("The answer is (D).") == "D"

    def test_last_letter_wins_in_bare_chain(self):
        # Model reasoning: mentions multiple letters, lands on B
        assert extract_choice_letter("Maybe A or C? Probably B") == "B"

    def test_answer_is_overrides_later_bare(self):
        # "answer is" is the explicit pattern; trust it over later bare mentions
        assert extract_choice_letter("The answer is A. Or maybe B.") == "A"

    def test_think_block_stripped(self):
        out = "<think>I'll say A</think>The answer is C."
        assert extract_choice_letter(out) == "C"

    def test_no_letter(self):
        assert extract_choice_letter("I don't know.") is None

    def test_letter_in_word_ignored(self):
        # "Apple" should not match A
        assert extract_choice_letter("Apple banana") is None

    def test_custom_valid(self):
        # ARC sometimes has E option
        assert extract_choice_letter("E", valid=("A", "B", "C", "D", "E")) == "E"
        assert extract_choice_letter("E", valid=("A", "B", "C", "D")) is None
