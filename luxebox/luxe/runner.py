"""Dispatch a RouterDecision to the right specialist agent."""

from __future__ import annotations

from luxe.agents import code, general, image, research, writing
from luxe.agents.base import AgentResult
from luxe.backend import make_backend
from luxe.registry import LuxeConfig
from luxe.router import RouterDecision
from luxe.session import Session
from luxe.tools import draw_things

_SPECIALISTS = {
    "general": general.run,
    "research": research.run,
    "writing": writing.run,
    "image": image.run,
    "code": code.run,
}


def dispatch(
    decision: RouterDecision,
    cfg: LuxeConfig,
    *,
    session: Session | None = None,
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
    backend = make_backend(agent_cfg.model, base_url=cfg.ollama_base_url)
    runner = _SPECIALISTS[decision.agent]
    return runner(backend, agent_cfg, task=decision.task, session=session)
