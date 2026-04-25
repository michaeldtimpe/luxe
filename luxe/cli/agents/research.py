"""Deep research agent — web-enabled.

Tools: web_search, fetch_url, fetch_urls + browse_navigate, browse_read
(JS-rendered fallback). Synthesizes answers with inline citations.
"""

from __future__ import annotations

from typing import Any, Callable

from harness.backends import Backend

from cli.agents.base import AgentResult, run_agent
from cli.registry import AgentConfig
from cli.session import Session
from cli.tools import browser, web


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    tool_defs = web.tool_defs() + browser.tool_defs()
    tool_fns = {**web.TOOL_FNS, **browser.TOOL_FNS}
    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
        on_tool_event=on_tool_event,
    )
