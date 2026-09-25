"""MMLU row → MLXDirectBackend prompt.

Builds 5-shot Hendrycks-style prompts that end with `Answer:` so the
model's next-token distribution has A/B/C/D as the high-probability
candidates. The chat template is applied by the caller (run.py) via the
mlx_lm tokenizer so the in-process scoring path mirrors the same template
oMLX serves with.

Scoring docstring (important):
  This benchmark scores via the first-generated-assistant-token AFTER the
  chat template is applied. Logprobs here are not equivalent to raw-completion
  logprobs from lm-eval-harness's `mmlu` task — they include role-marker
  context. Comparing to published lm-eval-harness numbers requires matching
  this scoring mode (chat-template + first-token).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from benchmarks._eval_common.choices import format_mc_prompt
from benchmarks._eval_common.fewshot import deterministic_sample

LETTERS = ("A", "B", "C", "D")


def index_by_subject(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["subject"]].append(r)
    return dict(out)


def select_stratified(rows: list[dict], limit: int | None) -> list[dict]:
    """Select up to `limit` rows, stratified as evenly as possible across subjects.

    If `limit` is None or covers the whole set, every row is used (no
    stratification needed). Otherwise rows are handed out round-by-round in
    even per-subject shares; a subject that runs out of rows before its
    share is exhausted has its unused quota redistributed to subjects that
    still have rows left, so the result always has exactly
    `min(limit, len(rows))` rows rather than under-selecting when subjects
    are small. Deterministic: subjects are visited in sorted order each
    round and rows are kept in their original per-subject order.
    """
    if limit is None or limit >= len(rows):
        return list(rows)

    by_subject = index_by_subject(rows)
    subjects = sorted(by_subject)
    remaining = {s: list(by_subject[s]) for s in subjects}
    taken: dict[str, list[dict]] = {s: [] for s in subjects}

    quota = limit
    active = subjects
    while quota > 0 and active:
        share, rem = divmod(quota, len(active))
        next_active = []
        for i, s in enumerate(active):
            want = share + (1 if i < rem else 0)
            avail = remaining[s]
            take = min(want, len(avail))
            if take:
                taken[s].extend(avail[:take])
                remaining[s] = avail[take:]
                quota -= take
            if remaining[s]:
                next_active.append(s)
        active = next_active

    return [row for s in subjects for row in taken[s]]


def fewshot_for_subject(
    dev_rows: list[dict],
    subject: str,
    *,
    k: int = 5,
    seed: int = 42,
) -> list[tuple[str, Sequence[str], str]]:
    """Return k (question, choices, gold_letter) tuples for the subject's dev split."""
    by_subj = [r for r in dev_rows if r["subject"] == subject]
    chosen = deterministic_sample(by_subj, k=k, seed=seed)
    return [(r["question"], r["choices"], LETTERS[r["answer"]]) for r in chosen]


def build_prompt(row: dict, fewshot: list[tuple[str, Sequence[str], str]]) -> str:
    """Render a single MMLU row as a 5-shot MCQ prompt ending with `Answer:`."""
    return format_mc_prompt(
        row["question"],
        row["choices"],
        fewshot_examples=fewshot,
        letters=LETTERS,
        instruction=(
            f"The following are multiple choice questions (with answers) "
            f"about {row['subject'].replace('_', ' ')}."
        ),
    )


# Hendrycks category groupings (STEM / humanities / social sciences / other).
# https://github.com/hendrycks/test/blob/master/categories.py
CATEGORIES = {
    "STEM": {
        "abstract_algebra", "anatomy", "astronomy", "college_biology",
        "college_chemistry", "college_computer_science", "college_mathematics",
        "college_physics", "computer_security", "conceptual_physics",
        "electrical_engineering", "elementary_mathematics", "high_school_biology",
        "high_school_chemistry", "high_school_computer_science",
        "high_school_mathematics", "high_school_physics", "high_school_statistics",
        "machine_learning",
    },
    "humanities": {
        "formal_logic", "high_school_european_history", "high_school_us_history",
        "high_school_world_history", "international_law", "jurisprudence",
        "logical_fallacies", "moral_disputes", "moral_scenarios", "philosophy",
        "prehistory", "professional_law", "world_religions",
    },
    "social_sciences": {
        "econometrics", "high_school_geography", "high_school_government_and_politics",
        "high_school_macroeconomics", "high_school_microeconomics",
        "high_school_psychology", "human_sexuality", "professional_psychology",
        "public_relations", "security_studies", "sociology", "us_foreign_policy",
    },
    "other": {
        "business_ethics", "clinical_knowledge", "college_medicine",
        "global_facts", "human_aging", "management", "marketing",
        "medical_genetics", "miscellaneous", "nutrition", "professional_accounting",
        "professional_medicine", "virology",
    },
}


def category_for(subject: str) -> str:
    for cat, subjects in CATEGORIES.items():
        if subject in subjects:
            return cat
    return "uncategorized"
