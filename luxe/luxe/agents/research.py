"""Deep research agent — web-enabled.

Tools: web_search, fetch_url. Synthesizes answers with inline citations.
"""

from __future__ import annotations

from typing import Any, Callable

from harness.backends import Backend

from luxe.agents.base import AgentResult, run_agent
from luxe.registry import AgentConfig
from luxe.session import Session
from luxe.tools import web


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
        tool_defs=web.tool_defs(),
        tool_fns=web.TOOL_FNS,
        session=session,
        on_tool_event=on_tool_event,
    )
