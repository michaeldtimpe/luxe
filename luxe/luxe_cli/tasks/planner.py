"""LLM-driven subtask planner. Takes a user goal plus the list of available
specialists, returns an ordered list of Subtask with title + agent."""

from __future__ import annotations

import json
import re

from luxe_cli.backend import make_backend
from luxe_cli.registry import LuxeConfig
from luxe_cli.tasks import plan_cache
from luxe_cli.tasks.model import Subtask, subtask_id


_SYNTHESIS_RE = re.compile(
    r"\b(summari[zs]e|synthesi[zs]e|"
    r"(generate|write|produce|compile|assemble|emit)\s+[\w\s-]*?"
    r"\b(report|summary|writeup|write[\s-]?up))\b",
    re.IGNORECASE,
)


def _subtask_overrides(title: str) -> dict[str, int]:
    """Per-subtask budget overrides, chosen from the title shape. The
    synthesis subtask in /review assembles the final severity-grouped
    report by concatenating all prior findings — it needs a bigger
    per-turn output cap than inspection subtasks, which typically
    emit one finding block per pass. Doubling the default 4096 keeps
    the report from getting truncated mid-category."""
    if _SYNTHESIS_RE.search(title or ""):
        return {"max_tokens_per_turn_override": 8192}
    return {}

_PLAN_SYSTEM_PROMPT = """You are a task planner inside luxe, a local multi-agent CLI.
Given a user's goal, break it into 1–8 ordered subtasks that can each be
handed off to one specialist. Return ONLY a JSON array — no prose, no
markdown fence.

Each entry: {"title": "what to do", "agent": "<name>"}

Specialists (pick the best fit; misrouting wastes compute):
- general  : Short Q&A, explanations, definitions, simple factual
             answers from training knowledge. Quick and cheap.
             NOT for multi-step arithmetic, NOT for anything needing
             fresh web data.
- lookup   : ONE-sentence factual web lookup — dates, versions, specs,
             prices, release years, single numbers/strings. Single
             web_search, snippet-only. Use over `research` whenever a
             cited number/line suffices, because it's 5–10× faster.
- calc     : Arithmetic, unit conversion, cost/time/distance estimation,
             step-by-step reasoning over numbers. Use this whenever the
             subtask is "compute / estimate / how long / how much / how
             far / add / subtract". Bigger model than `general`, picks
             up where `general` would hallucinate the math.
- research : Deep web investigation — multi-page synthesis, comparison
             across sources, anything requiring reading full pages
             (driving routes with specific turn-by-turn instructions,
             store hours, current-event context, charging-station
             details across multiple networks). Use only when `lookup`
             isn't enough.
- writing  : Prose, editorial review, creative writing, drafting or
             revising documents in the user's local folder.
- code     : READ OR EDIT SOURCE CODE IN A LOCAL REPOSITORY. Running
             tests, refactoring files, fixing bugs in source. NOT for
             arithmetic, NOT for trip planning. Only for actual source
             code / repo work.
- review   : Read-only code review in a repository — bugs, security,
             flaws. Driven by /review.
- refactor : Read-only optimization/refactor suggestions for a
             repository. Driven by /refactor.
- image    : Generate an image from a description.

Execution model (important):
- Subtasks run serially. Later subtasks see a summary of earlier
  completed subtasks' results, so you CAN chain them ("look up X, then
  use X to compute Y").
- Prefer 3–5 substantive subtasks. Fewer when the goal is simple;
  rarely more than 6. Each subtask should be worth a standalone agent
  turn — do NOT split arithmetic across multiple `calc` subtasks
  unless the subproblems are genuinely independent ("charging time"
  and "fuel cost" can go in one subtask together).
- Do NOT add "summarize the previous findings" as its own subtask —
  the last substantive subtask can do the summary inline.
- If the goal is genuinely a single step, return a 1-element list.

Example — "plan a trip from A to B with charging stops, estimate time
and cost":
[
  {"title": "Determine driving route and distance from A to B", "agent": "research"},
  {"title": "Identify fast-charging stations along the route", "agent": "research"},
  {"title": "Estimate charging time and cost per stop given range, rate per kWh, and session charge", "agent": "calc"},
  {"title": "Summarize total trip time, total cost, and stop plan", "agent": "calc"}
]
WRONG for this example:
- `code` for any subtask (no source code is involved).
- `general` for the estimation subtasks (arithmetic → `calc`).
`research` handles real-world lookups; `calc` handles the arithmetic
over what research found.
"""


def _extract_json_array(text: str) -> list | None:
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    raw = m.group(1) if m else None
    if raw is None:
        m = re.search(r"(\[.*\])", text, re.DOTALL)
        raw = m.group(1) if m else None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


def plan(
    goal: str,
    cfg: LuxeConfig,
    task_id_full: str,
    *,
    cache_key: tuple[str, str] | None = None,
    use_cache: bool = True,
) -> list[Subtask]:
    """Ask the router LLM to decompose `goal`. Falls back to a single-subtask
    pseudo-plan if the LLM misbehaves — orchestrator will route it.

    `cache_key` is `(repo_path, mode)` for /review and /refactor flows;
    pass None for ad-hoc REPL goals (no cache lookup or store). Set
    `use_cache=False` to force a fresh LLM decomposition while still
    populating the cache on success.
    """
    entries: list | None = None
    if cache_key and use_cache:
        entries = plan_cache.lookup(*cache_key)

    if entries is None:
        router_cfg = cfg.get("router")
        endpoint = router_cfg.endpoint or cfg.ollama_base_url
        backend = make_backend(router_cfg.model, base_url=endpoint)
        try:
            resp = backend.chat(
                [
                    {"role": "system", "content": _PLAN_SYSTEM_PROMPT},
                    {"role": "user", "content": goal},
                ],
                max_tokens=1024,
                temperature=0.1,
                stream=False,
            )
        except Exception:  # noqa: BLE001
            resp = None

        if resp and resp.text:
            entries = _extract_json_array(resp.text)

        if entries and cache_key:
            plan_cache.store(*cache_key, entries)

    if not entries:
        entries = [{"title": goal, "agent": ""}]

    valid = {a.name for a in cfg.agents if a.enabled and a.name != "router"}
    subs: list[Subtask] = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title", "")).strip()
        if not title:
            continue
        agent = str(entry.get("agent") or "").strip().lower()
        if agent and agent not in valid:
            agent = ""
        subs.append(Subtask(
            id=subtask_id(task_id_full, i),
            parent_id=task_id_full,
            index=i,
            title=title,
            agent=agent,
            **_subtask_overrides(title),
        ))
    if not subs:
        subs.append(Subtask(
            id=subtask_id(task_id_full, 1),
            parent_id=task_id_full,
            index=1,
            title=goal,
            agent="",
        ))
    return subs
