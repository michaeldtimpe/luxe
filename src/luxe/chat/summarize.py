"""Deterministic, non-model conversation summarizer for the chat REPL.

This is a *first-class, versioned* component (chat.sdd): the moment older
turns are folded into a summary, a second prompting system exists that shapes
all future agent behavior. To keep regressions explainable we deliberately do
NOT spend a model call here in v1 — `fold_history` is pure truncation:

  - the most recent `keep_recent` turns are kept verbatim
  - older turns are hard-truncated to `older_cap` characters
  - if the whole thing still exceeds `budget_chars`, the oldest turns are
    dropped (eldest first) and replaced by a single elision marker

The output is deterministic for a fixed input + version, and the produced fold
is persisted alongside the transcript (see memory/session.py `fold.jsonl`) so a
later summarizer change (which bumps `SUMMARIZER_VERSION`) is attributable.
"""

from __future__ import annotations

# Bump this when the folding algorithm changes; transcripts record which
# version produced their context.
SUMMARIZER_VERSION = "trunc-v1"

_ELISION = "[… older turns elided …]"
_TRUNC_MARK = " …[truncated]"


def _truncate(text: str, cap: int) -> str:
    text = text.strip()
    if cap <= 0 or len(text) <= cap:
        return text
    return text[: max(0, cap - len(_TRUNC_MARK))].rstrip() + _TRUNC_MARK


def _format_turn(user: str, assistant: str, *, cap: int | None) -> str:
    u = user.strip() if cap is None else _truncate(user, cap)
    a = assistant.strip() if cap is None else _truncate(assistant, cap)
    lines = [f"[user] {u}"]
    if a:
        lines.append(f"[assistant] {a}")
    return "\n".join(lines)


def fold_history(
    turns: list[tuple[str, str]],
    *,
    budget_chars: int = 4000,
    keep_recent: int = 3,
    older_cap: int = 400,
    version: str = SUMMARIZER_VERSION,
) -> str:
    """Fold prior `(user, assistant)` turns into a compact history string.

    Returns the formatted body (no surrounding tags — the caller wraps it in
    `<conversation_history>`). Empty string when there are no prior turns.

    Deterministic: same input + version → same output.
    """
    if version != SUMMARIZER_VERSION:
        raise ValueError(
            f"Unknown summarizer version {version!r}; this build is "
            f"{SUMMARIZER_VERSION!r}."
        )
    if not turns:
        return ""

    keep_recent = max(0, keep_recent)
    n = len(turns)
    split = max(0, n - keep_recent)
    older = turns[:split]
    recent = turns[split:]

    blocks: list[str] = []
    for user, assistant in older:
        blocks.append(_format_turn(user, assistant, cap=older_cap))
    for user, assistant in recent:
        blocks.append(_format_turn(user, assistant, cap=None))

    # Drop oldest blocks until under budget. Never drop the recent (verbatim)
    # tail — those are the load-bearing context for the next turn.
    floor = len(blocks) - len(recent)
    elided = False
    while len(blocks) > 1 and _joined_len(blocks, elided) > budget_chars and len(blocks) > len(recent):
        blocks.pop(0)
        elided = True
        floor -= 1

    if elided:
        blocks.insert(0, _ELISION)
    return "\n\n".join(blocks)


def _joined_len(blocks: list[str], elided: bool) -> int:
    body = "\n\n".join(blocks)
    extra = len(_ELISION) + 2 if elided else 0
    return len(body) + extra
