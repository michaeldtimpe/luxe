"""Multiple-choice prompt formatting for MMLU / ARC-style evals."""
from __future__ import annotations

from typing import Sequence

DEFAULT_LETTERS = ("A", "B", "C", "D", "E")


def format_mc_prompt(
    question: str,
    options: Sequence[str],
    *,
    fewshot_examples: Sequence[tuple[str, Sequence[str], str]] = (),
    letters: Sequence[str] = DEFAULT_LETTERS,
    instruction: str | None = None,
) -> str:
    """Render an MCQ prompt that ends with `Answer:` (no trailing space).

    The first token the model generates after this should be a choice
    letter. Few-shot examples are (question, options, gold_letter) tuples
    rendered identically before the test question.

    Handles variable choice counts up to len(letters) (default A-E).
    """
    if len(options) > len(letters):
        raise ValueError(
            f"Too many options ({len(options)}) for letters {letters}"
        )

    parts: list[str] = []
    if instruction:
        parts.append(f"{instruction.rstrip()}\n\n")

    for ex_q, ex_opts, ex_ans in fewshot_examples:
        parts.append(_render_one(ex_q, ex_opts, letters))
        parts.append(f"Answer: {ex_ans}\n\n")

    parts.append(_render_one(question, options, letters))
    parts.append("Answer:")
    return "".join(parts)


def _render_one(question: str, options: Sequence[str], letters: Sequence[str]) -> str:
    lines = [f"{question.rstrip()}\n"]
    for letter, opt in zip(letters, options):
        lines.append(f"{letter}. {opt}\n")
    return "".join(lines)
