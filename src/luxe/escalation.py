"""Single → swarm escalation — preserves context across mode switches.

When single-mode emits the `escalate_to_swarm` signal, we don't want the swarm
architect re-discovering files single-mode already inspected. This module
captures a token-budgeted summary of the single-mode tool calls and the model's
last "plan" message, and formats it as an `initial_context` appendix for the
swarm architect's system prompt.

Cap: ~2 KB tokens ≈ 8000 chars (oMLX/Qwen tokenizer averages ~4 chars/token for
mixed natural-language + code). Cheaper to undercount and let the architect
re-read than to blow the architect's 8k context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from luxe.tools.base import ToolCall


_CHAR_CAP = 8000


@dataclass
class EscalationContext:
    files_read: list[str] = field(default_factory=list)
    tools_invoked: list[tuple[str, str, str]] = field(default_factory=list)  # (tool, target, summary)
    plan_excerpt: str = ""
    abort_reason: str = ""

    def render(self, char_cap: int = _CHAR_CAP) -> str:
        """Format as a section to append to the swarm architect's system prompt."""
        lines: list[str] = [
            "## Escalation context",
            "A single-model run was attempted on this goal and escalated to swarm.",
            "Use this to seed your decomposition rather than re-discovering.",
            "",
        ]

        if self.abort_reason:
            lines.append(f"Single-mode aborted with: {self.abort_reason}")
            lines.append("")

        if self.files_read:
            lines.append("Files already inspected by single-mode:")
            for f in self.files_read[:30]:
                lines.append(f"  - {f}")
            if len(self.files_read) > 30:
                lines.append(f"  ... and {len(self.files_read) - 30} more")
            lines.append("")

        if self.tools_invoked:
            lines.append("Other tool calls made (recent first):")
            for tool, target, summary in self.tools_invoked[:30]:
                snippet = summary[:120].replace("\n", " ").strip()
                lines.append(f"  - {tool}({target}) → {snippet}")
            if len(self.tools_invoked) > 30:
                lines.append(f"  ... and {len(self.tools_invoked) - 30} more")
            lines.append("")

        if self.plan_excerpt:
            lines.append("Single-mode's last plan / observation:")
            lines.append(self.plan_excerpt)
            lines.append("")

        text = "\n".join(lines)
        if len(text) > char_cap:
            text = text[: char_cap - 3] + "..."
        return text


def capture_from_single(
    tool_calls: Iterable[ToolCall],
    final_text: str,
    abort_reason: str = "",
) -> EscalationContext:
    """Build an EscalationContext from a single-mode AgentResult.

    `final_text` is the model's last assistant message, which often contains
    its plan or current understanding before signalling `escalate_to_swarm`.
    """
    ctx = EscalationContext(abort_reason=abort_reason)

    seen_files: set[str] = set()
    others: list[tuple[str, str, str]] = []

    for tc in tool_calls:
        # File reads → dedicated list (likely most useful for the architect)
        if tc.name == "read_file":
            path = str(tc.arguments.get("path", "")).strip()
            if path and path not in seen_files:
                seen_files.add(path)
                ctx.files_read.append(path)
            continue

        target = ""
        for key in ("path", "pattern", "command", "directory", "query"):
            if key in tc.arguments:
                target = str(tc.arguments[key])
                break
        summary = (tc.error or tc.result or "")[:200]
        others.append((tc.name, target, summary))

    # Most recent first; cap at 30 to bound the appendix size
    ctx.tools_invoked = list(reversed(others))[:30]

    if final_text:
        excerpt = final_text.strip()
        if len(excerpt) > 1500:
            excerpt = excerpt[:1500] + "..."
        ctx.plan_excerpt = excerpt

    return ctx
