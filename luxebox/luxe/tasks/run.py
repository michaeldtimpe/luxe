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

from luxe.registry import load_config
from luxe.tasks.model import _now, append_log_event, load, persist
from luxe.tasks.orchestrator import Orchestrator


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
