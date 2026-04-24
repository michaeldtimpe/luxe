"""Deterministic pre-router — short-circuits the LLM router when the
prompt is decisive. Rule-based scorer over literal pattern tables so
behaviour is auditable and replay-friendly.

Policy:
- Never scores `review` / `refactor` — those are command-driven (via
  /review, /refactor), not prompt-driven. Scoring them from free text
  mis-fires on code-review-related conversation.
- Never scores `general` — it's the residual. A zero-confidence miss
  falls through to the LLM, which will pick `general` itself when no
  specialist fits.
- On short prompts (< 3 words) and meta questions ("can you …",
  "do you …"), returns None so the LLM gets to use its wider
  understanding.
"""

from __future__ import annotations

import re
from typing import Any

from shared.trace_hints import TRACE_PATH_RE


_FILE_HINT_RE = re.compile(
    r"\b(folder|directory|document|documents|file|files|notes?|draft|drafts|"
    r"manuscript|essay|chapter|letter|readme|\.md|\.txt|\.rst)\b",
    re.IGNORECASE,
)

# Source-path tokens — single-word check for any popular extension.
_SOURCE_PATH_RE = re.compile(
    r"\b[\w/.-]+\.(py|ts|tsx|js|jsx|go|rs|java|rb|php|c|cc|cpp|h|hpp|swift|kt)\b",
    re.IGNORECASE,
)

# Code verbs — action words that strongly imply source-editing intent.
_CODE_VERBS = (
    "fix", "debug", "refactor", "rewrite", "rename", "extract", "inline",
    "optimize", "optimise", "implement", "patch", "edit", "modify", "update",
    "stub", "port", "migrate",
)
_CODE_VERB_RE = re.compile(
    r"\b(" + "|".join(_CODE_VERBS) + r")\b", re.IGNORECASE
)
_TEST_RE = re.compile(r"\b(pytest|unittest|tests?|test_\w+|spec\b)", re.IGNORECASE)

# "Latest/current" sense of currency → research / lookup.
_CURRENCY_RE = re.compile(
    r"\b(latest|current|recent|this (week|month|year)|today|yesterday|"
    r"news|breaking|just released|announced)\b",
    re.IGNORECASE,
)

# Short factual question shape.
_INTERROGATIVE_RE = re.compile(
    r"^\s*(what|when|where|who|which|how many|how much)\b",
    re.IGNORECASE,
)
_FACT_NOUN_RE = re.compile(
    r"\b(version|release|price|cost|year|date|gdp|population|capital|"
    r"distance|height|weight|speed)\b",
    re.IGNORECASE,
)

# Arithmetic / numerical.
_ARITH_CHAR_RE = re.compile(r"[0-9].*[0-9]")
_ARITH_OP_RE = re.compile(r"[+\-*/=^%]|÷|×")
_COMPUTE_VERB_RE = re.compile(
    r"\b(convert|compute|calculate|estimate|how (long|much|far|many)|"
    r"percent|percentage|ratio|rate|sum|average|mean|median)\b",
    re.IGNORECASE,
)

# Image generation.
_IMAGE_RE = re.compile(
    r"\b(draw|paint|render|generate an? (image|picture|portrait|"
    r"photo|render|illustration)|illustration of|picture of|photo of)\b",
    re.IGNORECASE,
)

# Meta questions the LLM is better at reading.
_META_RE = re.compile(
    r"^\s*(can|could|do|does|will|would|should) (you|i|we)\b",
    re.IGNORECASE,
)


def _score(prompt: str, enabled: list[str]) -> dict[str, float]:
    """Weighted sum of per-agent features. Pure function — no I/O."""
    scores: dict[str, float] = {}
    lower = prompt.lower()

    def add(agent: str, amount: float) -> None:
        if agent in enabled:
            scores[agent] = scores.get(agent, 0.0) + amount

    # --- code -----------------------------------------------------------
    if _SOURCE_PATH_RE.search(prompt):
        add("code", 3.0)
    if TRACE_PATH_RE.search(prompt):
        # Pasted traceback is almost always a fix-the-bug ask.
        add("code", 4.0)
    if _CODE_VERB_RE.search(prompt):
        add("code", 1.5)
    if _TEST_RE.search(prompt):
        add("code", 1.5)
    if re.search(r"\b(bug|error|exception|traceback|stack trace|crash)\b", lower):
        add("code", 1.5)

    # --- writing --------------------------------------------------------
    if _FILE_HINT_RE.search(prompt):
        add("writing", 2.0)
    if re.search(
        r"\b(draft|revise|essay|story|poem|chapter|outline|brainstorm|prose)\b",
        lower,
    ):
        add("writing", 2.0)
    if re.search(r"\b(write|rewrite) (a|an|the) (letter|story|essay|draft|note)", lower):
        add("writing", 2.0)

    # --- research -------------------------------------------------------
    # Currency alone isn't research — a fresh fact can be a lookup too.
    # Require multi-source / comparison verbs to score research highly.
    if re.search(
        r"\b(compare|survey|investigate|analysis of|trends? (in|of)|"
        r"across (sources|papers)|synthesize|synthesise|literature review)\b",
        lower,
    ):
        add("research", 3.0)
        if _CURRENCY_RE.search(prompt):
            add("research", 1.0)

    # --- lookup ---------------------------------------------------------
    # Lookup absorbs short fresh-data questions — snippet-only lookup
    # is the right tool for "latest version of X" / "when did Y ship".
    wc = len(prompt.split())
    if _CURRENCY_RE.search(prompt) and wc < 14:
        add("lookup", 2.5)
    if _INTERROGATIVE_RE.search(prompt) and _FACT_NOUN_RE.search(prompt):
        if wc < 15:
            add("lookup", 2.5)
    if _INTERROGATIVE_RE.search(prompt) and wc < 12:
        add("lookup", 1.0)

    # --- calc -----------------------------------------------------------
    if _COMPUTE_VERB_RE.search(prompt):
        add("calc", 2.0)
    # Dense digits + an op symbol — strong arithmetic tell.
    if _ARITH_CHAR_RE.search(prompt) and _ARITH_OP_RE.search(prompt):
        add("calc", 2.0)

    # --- image ----------------------------------------------------------
    if _IMAGE_RE.search(prompt):
        add("image", 3.0)

    return scores


def score_prompt(prompt: str, enabled: list[str]) -> dict[str, float]:
    """Public entry. Returns dict of {agent_name: raw_score}. Callers
    can inspect the full scoring for telemetry; `decide` applies the
    threshold."""
    return _score(prompt, enabled)


def decide(
    prompt: str,
    enabled: list[str],
    *,
    threshold: float = 0.35,
) -> tuple[str | None, float, dict[str, float]]:
    """Return (agent, confidence, scores). `agent` is None when the
    heuristic is not confident enough — caller should fall through to
    the LLM router. Confidence is a normalized margin in [0.0, 1.0].

    Short-circuit rules that return (None, 0.0, {}):
    - empty or whitespace-only prompt
    - less than 3 words
    - meta questions ("can you …", "do you …")
    """
    text = (prompt or "").strip()
    if not text:
        return None, 0.0, {}
    if len(text.split()) < 3:
        return None, 0.0, {}
    if _META_RE.search(text):
        return None, 0.0, {}

    scores = _score(text, enabled)
    if not scores:
        return None, 0.0, {}

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top_name, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = top_score - second_score
    # Normalize against top_score so the threshold is a relative margin.
    confidence = margin / top_score if top_score > 0 else 0.0
    # Also require an absolute floor: a single weak match (e.g. single
    # interrogative rule firing for "what time") shouldn't short-circuit.
    if top_score < 2.0 or confidence < threshold:
        return None, confidence, scores

    return top_name, confidence, scores


__all__ = ["score_prompt", "decide"]
