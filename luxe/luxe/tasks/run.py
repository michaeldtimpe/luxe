"""Subprocess entry point for background task execution.

Invoked as:  python -m luxe.tasks.run <task-id>

Loads the pre-persisted Task from disk, installs a SIGTERM handler so the
parent REPL can ask us to stop cleanly at the next subtask boundary, then
runs the orchestrator. All progress lands in ~/.luxe/tasks/<id>/
(state.json + log.jsonl + stdout.log).
"""

from __future__ import annotations

import os
import signal
import sys
import traceback
from pathlib import Path

from luxe.registry import load_config
from luxe.tasks.model import _now, append_log_event, load, persist
from luxe.tasks.orchestrator import Orchestrator
from luxe.tasks.report import build_markdown_report


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m luxe.tasks.run <task-id>", file=sys.stderr)
        return 2
    task_id = sys.argv[1]
    task = load(task_id)
    if task is None:
        print(f"task not found: {task_id}", file=sys.stderr)
        return 2

    # Record that we own this run.
    task.pid = os.getpid()
    persist(task)
    append_log_event(task, {"event": "subprocess_start", "pid": os.getpid()})

    abort = {"flag": False}

    def _sig(_num, _frame):
        abort["flag"] = True
        append_log_event(task, {"event": "sigterm_received"})

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    cfg = load_config()

    try:
        Orchestrator(cfg, session=None).run(task, should_abort=lambda: abort["flag"])
    except Exception as e:  # noqa: BLE001
        task.status = "blocked"
        task.completed_at = _now()
        persist(task)
        append_log_event(task, {
            "event": "crashed",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        })
        return 1

    _auto_save_report(task)
    return 0


def _auto_save_report(task) -> None:
    """Write a markdown report to the review/refactor target dir so the
    user doesn't have to remember `/tasks save` after a background run.
    No-op unless the task has a `repo_path` pointer (written by the
    review flow) and reached a usable terminal state."""
    if task.status not in ("done", "blocked"):
        return
    repo_ptr = task.dir() / "repo_path"
    if not repo_ptr.exists():
        return
    try:
        target_dir = Path(repo_ptr.read_text().strip())
    except OSError:
        return
    if not target_dir.exists() or not target_dir.is_dir():
        return
    first_agent = task.subtasks[0].agent if task.subtasks else ""
    prefix = "REFACTOR" if first_agent == "refactor" else "REVIEW"
    out = target_dir / f"{prefix}-{task.id}.md"
    try:
        out.write_text(build_markdown_report(task))
    except OSError as e:
        append_log_event(task, {"event": "report_save_failed", "error": str(e)})
        return
    append_log_event(task, {
        "event": "report_saved", "path": str(out), "bytes": out.stat().st_size,
    })


if __name__ == "__main__":
    sys.exit(main())
