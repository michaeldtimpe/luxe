"""LLM-driven subtask planner. Takes a user goal plus the list of available
specialists, returns an ordered list of Subtask with title + agent."""

from __future__ import annotations

import json
import re

from luxe.backend import make_backend
from luxe.registry import LuxeConfig
from luxe.tasks.model import Subtask, subtask_id

_PLAN_SYSTEM_PROMPT = """You are a task planner inside luxe, a local multi-agent CLI.
Given a user's goal, break it into 1–8 ordered subtasks that can each be
handed off to one specialist. Return ONLY a JSON array — no prose, no
markdown fence.

Each entry: {"title": "what to do", "agent": "<name>"}

Specialists (pick the best fit; misrouting wastes compute):
- general  : Short Q&A, explanations, definitions, simple factual
             answers. Quick and cheap. NOT for multi-step arithmetic.
- calc     : Arithmetic, unit conversion, cost/time/distance estimation,
             step-by-step reasoning over numbers. Use this whenever the
             subtask is "compute / estimate / how long / how much / how
             far / add / subtract". Bigger model than `general`, picks
             up where `general` would hallucinate the math.
- research : Needs fresh web data — current events, pricing lookups,
             driving routes, store hours, charging-station locations,
             geography. Use whenever the answer depends on info outside
             the model's training cutoff.
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
- Pick the smallest number of subtasks that actually completes the goal.
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


def plan(goal: str, cfg: LuxeConfig, task_id_full: str) -> list[Subtask]:
    """Ask the router LLM to decompose `goal`. Falls back to a single-subtask
    pseudo-plan if the LLM misbehaves — orchestrator will route it."""
    router_cfg = cfg.get("router")
    backend = make_backend(router_cfg.model, base_url=cfg.ollama_base_url)
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

    entries: list | None = None
    if resp and resp.text:
        entries = _extract_json_array(resp.text)

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
