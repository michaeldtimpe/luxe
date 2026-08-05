"""Task-type inference — the keyword heuristic behind persona/slot routing.

Moved out of `cli.py` 2026-08-05 (consolidation deferred-list #6): every
run-path front-end needs it (`maintain`, chat repl/tui slot routing, `pr`),
so its home is the agents tier, not the CLI. Known limitation, documented at
its consumers: ordinary chat messages ("explain…", "add…", "fix…") match the
coding keywords — which is why chat keys the PERSONA on `chat_conversational`
and uses this heuristic for slot/model routing only (see chat.sdd).
"""

from __future__ import annotations


def infer_task_type(goal: str) -> str:
    g = goal.lower()
    if any(k in g for k in (
        "implement", "add ", "build", "create", "introduce", "refactor", "rewrite",
        "optimize", "change", "modify", "delete", "remove", "support", "improve",
        "tweak", "adjust", "polish", "re-implement", "update", "migrate", "port",
        "enable", "disable", "clean", "restructure"
    )):
        return "implement"
    if any(k in g for k in (
        "fix", "bug", "broken", "regression", "patch", "resolve", "correct",
        "mend", "handle"
    )):
        return "bugfix"
    if any(k in g for k in (
        "document", "docs", "readme", "docstring", "comment", "documentation",
        "typehint", "typing", "types"
    )):
        return "document"
    if any(k in g for k in (
        "update deps", "upgrade", "ci", "config", "dep", "dependency", "docker",
        "github action", "workflow"
    )):
        return "manage"
    if any(k in g for k in ("summarize", "summary", "explain", "describe")):
        return "summarize"
    return "review"
