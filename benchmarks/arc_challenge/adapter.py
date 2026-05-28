"""ARC-Challenge row → prompt.

ARC questions have a variable number of choices (3, 4, or 5). The dataset
labels them either A/B/C/D/E or 1/2/3/4/5 depending on the question; we
always re-render with A/B/C/D/E letters for uniform scoring, and remap
the gold answerKey to whichever letter ends up at that position.

0-shot: no exemplars (more diagnostic than the 25-shot leaderboard default;
expect ~3–8 points below published Qwen 25-shot ARC-C figures).
"""
from __future__ import annotations

from benchmarks._eval_common.choices import format_mc_prompt

LETTERS = ("A", "B", "C", "D", "E")


def gold_letter(row: dict) -> str:
    """Map ARC's answerKey to our letter at the same position.

    answerKey is a string like "A", "B", "1", "2", etc. We look up its
    index in `choices.label` and return the LETTERS entry at that index.
    """
    key = row["answerKey"]
    labels = row["choices"]["label"]
    idx = labels.index(key)
    return LETTERS[idx]


def build_prompt(row: dict) -> str:
    return format_mc_prompt(
        row["question"],
        row["choices"]["text"],
        letters=LETTERS,
        instruction="The following is a multiple choice science question. Pick the best answer.",
    )


def valid_letters_for(row: dict) -> tuple[str, ...]:
    """Slice LETTERS to the actual number of choices."""
    return LETTERS[: len(row["choices"]["text"])]
