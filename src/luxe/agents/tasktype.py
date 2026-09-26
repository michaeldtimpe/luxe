"""Task-type inference — the keyword heuristic behind persona/slot routing.

Moved out of `cli.py` 2026-08-05 (consolidation deferred-list #6): every
run-path front-end needs it (`maintain`, chat repl/tui slot routing, `pr`),
so its home is the agents tier, not the CLI. Known limitation, documented at
its consumers: ordinary chat messages ("explain…", "add…", "fix…") match the
coding keywords — which is why chat keys the PERSONA on `chat_conversational`
and uses this heuristic for slot/model routing only (see chat.sdd).
"""

from __future__ import annotations


import re

# Keywords match on WORD boundaries (2026-09 review): plain substring tests
# routed "the error report" / "the important module" to `implement` ("port")
# and "the specific behaviour" to `manage` ("ci"). A trailing `s` is allowed
# so "fixes", "bugs", "deps" still match their stem. Multi-word `manage`
# phrases are tested before the single-word `implement` list, so "update deps"
# is no longer swallowed by "update".
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("manage", ("update deps", "update dependencies", "github action")),
    ("implement", (
        "implement", "add", "build", "create", "introduce", "refactor",
        "rewrite", "optimize", "change", "modify", "delete", "remove",
        "support", "improve", "tweak", "adjust", "polish", "re-implement",
        "update", "migrate", "port", "enable", "disable", "clean",
        "restructure")),
    ("bugfix", (
        "fix", "bug", "broken", "regression", "patch", "resolve", "correct",
        "mend", "handle")),
    ("document", (
        "document", "docs", "readme", "docstring", "comment", "documentation",
        "typehint", "typing", "types")),
    ("manage", (
        "upgrade", "ci", "config", "dep", "dependency", "docker", "workflow")),
    ("summarize", ("summarize", "summary", "explain", "describe")),
)
def _word(k: str) -> str:
    # "create" → create, creates, created, creating; "fix" → fixes, fixed …
    stem = k[:-1] if k.endswith("e") else k
    return rf"(?:{re.escape(k)}(?:e?s|d|ed|ing)?|{re.escape(stem)}ing)"


_PATTERNS = tuple(
    (kind, re.compile(r"\b(?:" + "|".join(_word(k) for k in words) + r")\b"))
    for kind, words in _RULES
)


def infer_task_type(goal: str) -> str:
    g = goal.lower()
    for kind, pat in _PATTERNS:
        if pat.search(g):
            return kind
    return "review"
