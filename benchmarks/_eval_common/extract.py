"""Answer extractors for static-eval benchmarks.

Pure functions: take strings, return parsed values + diagnostic reasons.
No Backend, no I/O. Offline-testable.
"""
from __future__ import annotations

import re

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think_blocks(text: str) -> str:
    """Strip <think>...</think> blocks emitted by reasoning models.

    Qwen3-series models put intermediate reasoning here, which often
    contains numbers / answers that aren't the final answer. Always strip
    before applying answer regexes.
    """
    return _THINK_BLOCK_RE.sub("", text)


_GSM8K_HASH_RE = re.compile(r"####\s*(-?\d[\d,]*\.?\d*)")
_GSM8K_ANSWER_IS_RE = re.compile(
    r"answer\s+is\s*\$?\s*(-?\d[\d,]*\.?\d*)", re.IGNORECASE
)
_GSM8K_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def extract_gsm8k_answer(output: str) -> tuple[float | None, str]:
    """Extract a numeric answer from GSM8K-style model output.

    Returns (value, failure_reason) where failure_reason is:
      "none"             — parsed successfully
      "think_only"       — output was empty after stripping <think> blocks
      "no_answer_marker" — no `####`, no "answer is X", no numbers
      "non_numeric"      — pattern matched but couldn't parse as a number

    Priority: `#### N` (canonical) > "The answer is N" (Wei-style CoT) >
    last number in stripped output (fallback).
    """
    stripped = strip_think_blocks(output).strip()
    if not stripped:
        return None, "think_only"

    candidate: str | None = None
    if (m := _GSM8K_HASH_RE.search(stripped)):
        candidate = m.group(1)
    elif (m := _GSM8K_ANSWER_IS_RE.search(stripped)):
        candidate = m.group(1)
    else:
        nums = _GSM8K_NUM_RE.findall(stripped)
        if nums:
            candidate = nums[-1]
        else:
            return None, "no_answer_marker"

    try:
        return float(candidate.replace(",", "")), "none"
    except ValueError:
        return None, "non_numeric"


_LETTER_ANSWER_RE = re.compile(
    r"answer\s*(?:is\s*)?:?\s*\(?([A-E])\)?", re.IGNORECASE
)
_LETTER_BARE_RE = re.compile(r"(?<![A-Za-z])\(?([A-E])\)?(?![A-Za-z])")


def extract_choice_letter(
    output: str,
    valid: tuple[str, ...] = ("A", "B", "C", "D"),
) -> str | None:
    """Extract an MCQ letter (A–E) from generated text.

    Tolerant of: "B", "(B)", "B.", "The answer is B", "Answer: B".
    Strips <think> blocks first. Falls back to the last bare letter mention
    on the principle that models often restate their final answer last.

    Returns None if no letter from `valid` appears.
    """
    stripped = strip_think_blocks(output).strip()
    valid_set = set(valid)

    for m in _LETTER_ANSWER_RE.finditer(stripped):
        letter = m.group(1).upper()
        if letter in valid_set:
            return letter

    matches = [
        m.group(1).upper()
        for m in _LETTER_BARE_RE.finditer(stripped)
        if m.group(1).upper() in valid_set
    ]
    return matches[-1] if matches else None
