"""Stub specialist agents used during phased rollout.

Each stub records that it was called and echoes the task back, so the
router can be tested end-to-end before the real specialists land.
Replaced one-by-one as each phase ships.
"""

from __future__ import annotations

from harness.backends import Backend

from luxe_cli.agents.base import AgentResult
from luxe_cli.registry import AgentConfig
from luxe_cli.session import Session


def make_stub(agent_name: str, phase_label: str):
    def _run(
        backend: Backend,
        cfg: AgentConfig,
        *,
        task: str,
        session: Session | None = None,
    ) -> AgentResult:
        text = (
            f"**[{agent_name} stub]** — received task:\n\n> {task}\n\n"
            f"(real {agent_name} agent lands in {phase_label})"
        )
        if session:
            session.append({"role": "assistant", "agent": agent_name, "content": text})
        return AgentResult(
            final_text=text, steps_taken=1, tool_calls_total=0
        )

    return _run


research_stub = make_stub("research", "Phase 3")
writing_stub = make_stub("writing", "Phase 4")
image_stub = make_stub("image", "Phase 5")
code_stub = make_stub("code", "Phase 6")
