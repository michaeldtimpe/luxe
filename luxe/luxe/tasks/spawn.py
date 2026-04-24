"""Spawn + signal a background task subprocess.

Detaches the child into its own session so exiting the REPL doesn't kill
the job — a multi-hour task stays alive until it finishes, crashes, or
receives /tasks abort.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from luxe.tasks.model import Task


def spawn_background(task: Task) -> int:
    """Launch `python -m luxe.tasks.run <task-id>` detached. Returns the
    child's PID. stdout/stderr go into task.dir()/stdout.log."""
    log_path = task.dir() / "stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # open in unbuffered append so we can tail it live
    f = log_path.open("ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "luxe.tasks.run", task.id],
        stdout=f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=os.getcwd(),
    )
    return proc.pid


def abort_task(task: Task, grace_s: float = 5.0) -> bool:
    """Ask the running subprocess to stop. SIGTERM first; if it's still
    alive after `grace_s`, SIGKILL. On SIGKILL the subprocess can't update
    state.json, so we reconcile it here. Returns True if we signalled."""
    from luxe.tasks.model import _now, append_log_event, persist

    if not task.is_alive():
        return False
    try:
        os.kill(task.pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    append_log_event(task, {"event": "abort_sigterm", "grace_s": grace_s})
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not task.is_alive():
            return True
        time.sleep(0.2)
    # Still up after grace — SIGKILL and reconcile state (subprocess
    # can't write anything after SIGKILL).
    try:
        os.kill(task.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    task.status = "aborted"
    task.completed_at = _now()
    for sub in task.subtasks:
        if sub.status in ("running", "pending"):
            sub.status = "blocked" if sub.status == "running" else "skipped"
            if not sub.error:
                sub.error = "killed by SIGKILL during abort"
            if not sub.completed_at:
                sub.completed_at = _now()
    persist(task)
    append_log_event(task, {"event": "abort_sigkill"})
    return True
