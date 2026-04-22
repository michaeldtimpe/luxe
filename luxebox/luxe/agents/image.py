"""Image agent — prompt expander + Draw Things dispatcher.

Takes the user's request, rewrites it into a rich visual prompt, calls
`draw_things_generate`, and returns the saved path.
"""

from __future__ import annotations

from harness.backends import Backend

from luxe.agents.base import AgentResult, run_agent
from luxe.registry import AgentConfig
from luxe.session import Session
from luxe.tools import draw_things


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
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
    )
