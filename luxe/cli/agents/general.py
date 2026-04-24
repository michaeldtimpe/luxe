"""General-purpose chat agent. No tools — just conversation."""

from __future__ import annotations

from typing import Any, Callable

from harness.backends import Backend

from cli.agents.base import AgentResult, run_agent
from cli.registry import AgentConfig
from cli.session import Session


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=[],
        tool_fns={},
        session=session,
        on_tool_event=on_tool_event,
    )
