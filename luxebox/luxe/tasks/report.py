"""Markdown report assembly for a finished Task.

Shared between the background auto-save path (tasks/run.py) and the
interactive `/tasks save` command (repl/tasks.py) so both produce the
same output.
"""

from __future__ import annotations

from luxe.tasks.model import Task


def build_markdown_report(task: Task) -> str:
    from luxe.repl.status import _fmt_wall

    lines: list[str] = [
        f"# Task report — {task.id}",
        "",
        f"- **Goal**: {task.goal}",
        f"- **Status**: {task.status}",
        f"- **Started**: {task.created_at}",
        f"- **Finished**: {task.completed_at}",
        "",
    ]
    for s in task.subtasks:
        lines.append(f"## {s.index}. {s.title}")
        lines.append(
            f"*Agent: `{s.agent or 'route'}` · status: {s.status} · "
            f"wall: {_fmt_wall(s.wall_s)} · tool calls: {s.tool_calls_total}*"
        )
        lines.append("")
        if s.error:
            lines.append(f"> **Error:** {s.error}")
            lines.append("")
        if s.result_text:
            lines.append(s.result_text.rstrip())
            lines.append("")
    return "\n".join(lines) + "\n"
