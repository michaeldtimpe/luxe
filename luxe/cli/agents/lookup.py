"""Quick factual lookup — `web_search` only, no `fetch_url`.

Purpose: answer one-sentence factual questions (dates, versions, specs,
prices, etc.) in 10–20 s instead of the 90–150 s the full `research`
agent spends fetching + prefilling pages. A small fast model is
grounded by live search snippets, so hallucinations are bounded.
"""

from __future__ import annotations

from typing import Any, Callable

from harness.backends import Backend

from cli.agents.base import AgentResult, run_agent
from cli.registry import AgentConfig
from cli.session import Session
from cli.tools import web


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    # Only `web_search` — deliberately no fetch_url. If the snippet
    # doesn't have the answer, we want the agent to say so rather than
    # quietly escalating into a multi-page fetch.
    tool_defs = [t for t in web.tool_defs() if t.name == "web_search"]
    tool_fns = {"web_search": web.TOOL_FNS["web_search"]}
    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
        on_tool_event=on_tool_event,
    )
