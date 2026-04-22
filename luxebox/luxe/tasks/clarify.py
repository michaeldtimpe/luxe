"""Pre-planning clarification screen.

Given a user goal, ask the small router LLM whether additional info
would materially change the plan. Returns a list of short questions
(empty if the goal is specific enough). The REPL prompts the user
interactively and folds the answers back into the goal before planning.
"""

from __future__ import annotations

import json
import re

from luxe.backend import make_backend
from luxe.registry import LuxeConfig

_CLARIFY_SYSTEM_PROMPT = """You are a task intake screener inside luxe.
Given a user's goal, decide whether you can plan it into specialist
subtasks or whether key information is missing that would otherwise
waste time/compute.

Return ONLY a JSON array — no prose, no markdown fence. Each entry is
a short clarifying question. Return `[]` if the goal is specific
enough to proceed.

Ask ONLY when the answer would meaningfully change the outcome or pick
a different specialist. Don't ask for style preferences, nice-to-haves,
or trivia. Cap at 3 questions. Prefer one excellent question over three
mediocre ones.

Examples:
- Goal "summarize Python list comprehensions" → `[]`
- Goal "plan a trip from 1464 Keeler Dr in Irving TX to the closest beach"
  → `["Does 'beach' mean an ocean coast, or is a lake beach acceptable?"]`
- Goal "review the repo" → `["Which repository URL?"]`
- Goal "write a short story about a lighthouse keeper" → `[]`
"""


def clarify(goal: str, cfg: LuxeConfig) -> list[str]:
    """Return up to 3 clarifying questions for `goal`, or [] if none needed."""
    router_cfg = cfg.get("router")
    backend = make_backend(router_cfg.model, base_url=cfg.ollama_base_url)
    try:
        resp = backend.chat(
            [
                {"role": "system", "content": _CLARIFY_SYSTEM_PROMPT},
                {"role": "user", "content": goal},
            ],
            max_tokens=256,
            temperature=0.1,
            stream=False,
        )
    except Exception:  # noqa: BLE001
        return []
    if not resp or not resp.text:
        return []

    m = re.search(r"\[.*\]", resp.text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(arr, list):
        return []
    questions = [str(q).strip() for q in arr if isinstance(q, str) and q.strip()]
    return questions[:3]
