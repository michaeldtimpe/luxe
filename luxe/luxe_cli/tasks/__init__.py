"""Task-orchestration package for luxe.

Ported in spirit from elara-mk.2/elara_task.py but integrated with luxe's
structured types (harness.backends.ToolCall) and multi-agent runner, so
each subtask can route to the correct specialist. Phase 1: synchronous.
"""

from luxe_cli.tasks.clarify import clarify
from luxe_cli.tasks.model import (
    Subtask,
    Task,
    append_log_event,
    list_all,
    load,
    persist,
    reset_incomplete_subtasks,
)
from luxe_cli.tasks.orchestrator import Orchestrator
from luxe_cli.tasks.planner import plan
from luxe_cli.tasks.spawn import abort_task, spawn_background

__all__ = [
    "Subtask",
    "Task",
    "Orchestrator",
    "abort_task",
    "append_log_event",
    "clarify",
    "list_all",
    "load",
    "persist",
    "plan",
    "reset_incomplete_subtasks",
    "spawn_background",
]
