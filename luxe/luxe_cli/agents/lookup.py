"""Quick factual lookup — `web_search` + browser escape hatch.

Purpose: answer one-sentence factual questions (dates, versions, specs,
prices, etc.) in 10–20 s instead of the 90–150 s the full `research`
agent spends fetching + prefilling pages. A small fast model is
grounded by live search snippets, so hallucinations are bounded.

Browser tools (`browse_navigate`, `browse_read`) are exposed as a
last-resort escape hatch — the system prompt instructs the agent to
prefer "snippets don't contain the answer" over an escalation, and
the 3-step / 60-second budgets cap any accidental over-use.
"""

from __future__ import annotations

from typing import Any, Callable

from harness.backends import Backend

from luxe_cli.agents.base import AgentResult, run_agent
from luxe_cli.registry import AgentConfig
from luxe_cli.session import Session
from luxe_cli.tools import browser, web


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    # web_search + browser navigate/read. Deliberately no fetch_url —
    # the snippet-first flow is the fast path; if a JS-rendered page
    # is the only way to ground an answer, the browser tools are
    # there but the system prompt discourages routine use.
    tool_defs = [t for t in web.tool_defs() if t.name == "web_search"]
    tool_defs += browser.tool_defs()
    tool_fns = {"web_search": web.TOOL_FNS["web_search"], **browser.TOOL_FNS}
    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
        on_tool_event=on_tool_event,
    )
