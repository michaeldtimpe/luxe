"""Dispatch a RouterDecision to the right specialist agent."""

from __future__ import annotations

from cli import prefs
from cli.agents import (
    calc, code, general, image, lookup, refactor, research, review, writing,
)
from typing import Any, Callable

from cli.agents.base import AgentResult
from cli.backend import make_backend
from cli.registry import LuxeConfig
from cli.router import RouterDecision
from cli.session import Session
from cli.tasks.cache import ToolCache
from cli.tools import draw_things

_SPECIALISTS = {
    "general": general.run,
    "research": research.run,
    "writing": writing.run,
    "image": image.run,
    "code": code.run,
    "review": review.run,
    "refactor": refactor.run,
    "calc": calc.run,
    "lookup": lookup.run,
}


def dispatch(
    decision: RouterDecision,
    cfg: LuxeConfig,
    *,
    session: Session | None = None,
    model_override: str | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
    tool_cache: ToolCache | None = None,
) -> AgentResult:
    if decision.agent not in _SPECIALISTS:
        return AgentResult(
            final_text=f"[luxe] agent '{decision.agent}' not implemented yet",
            steps_taken=0,
            tool_calls_total=0,
            aborted=True,
            abort_reason="specialist not registered",
        )

    # Configure cross-cutting tool endpoints from LuxeConfig once per dispatch.
    draw_things.set_endpoint(cfg.draw_things_url, cfg.image_output_dir)

    agent_cfg = cfg.get(decision.agent)
    memory = prefs.load_memory()
    updates: dict = {}
    if memory:
        updates["system_prompt"] = (
            agent_cfg.system_prompt + "\n\n# User memory (persistent)\n" + memory
        )
    if model_override:
        updates["model"] = model_override
    if updates:
        agent_cfg = agent_cfg.model_copy(update=updates)

    endpoint = agent_cfg.endpoint or cfg.ollama_base_url
    backend = make_backend(agent_cfg.model, base_url=endpoint)
    runner = _SPECIALISTS[decision.agent]
    # Task-scoped memoization is only meaningful for agents that dispatch
    # multiple read-only tool calls within one Task. Pass the cache only
    # to those; the rest keep their simpler signatures.
    if decision.agent in ("review", "refactor", "code") and tool_cache is not None:
        return runner(
            backend, agent_cfg,
            task=decision.task, session=session,
            on_tool_event=on_tool_event,
            tool_cache=tool_cache,
        )
    return runner(
        backend, agent_cfg,
        task=decision.task, session=session,
        on_tool_event=on_tool_event,
    )
