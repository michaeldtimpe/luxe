"""Task-orchestration package for luxe.

Ported in spirit from elara-mk.2/elara_task.py but integrated with luxe's
structured types (harness.backends.ToolCall) and multi-agent runner, so
each subtask can route to the correct specialist. Phase 1: synchronous.
"""

from luxe.tasks.clarify import clarify
from luxe.tasks.model import Subtask, Task, append_log_event, list_all, load, persist
from luxe.tasks.orchestrator import Orchestrator
from luxe.tasks.planner import plan
from luxe.tasks.spawn import abort_task, spawn_background

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
    "spawn_background",
]
