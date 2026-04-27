"""Image agent — prompt expander + Draw Things dispatcher.

Takes the user's request, rewrites it into a rich visual prompt, calls
`draw_things_generate`, and returns the saved path.
"""

from __future__ import annotations

from typing import Any, Callable

from harness.backends import Backend

from luxe_cli.agents.base import AgentResult, run_agent
from luxe_cli.registry import AgentConfig
from luxe_cli.session import Session
from luxe_cli.tools import draw_things


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    ok, detail = draw_things.health_check()
    if not ok:
        return AgentResult(
            final_text=(
                f"**Draw Things not reachable** on {draw_things._URL}. "
                "Launch the app and enable its HTTP server, "
                f"then retry.\n\n_Detail: {detail}_"
            ),
            steps_taken=0,
            tool_calls_total=0,
            aborted=True,
            abort_reason="draw_things_unreachable",
        )

    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=draw_things.tool_defs(),
        tool_fns=draw_things.TOOL_FNS,
        session=session,
        on_tool_event=on_tool_event,
    )
