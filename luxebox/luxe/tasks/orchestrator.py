"""Multi-subtask runner.

Drives a Task through its Subtasks, dispatching each to the appropriate
specialist via luxe.runner, persisting progress after every state change,
and enforcing the per-task wall budget. Phase 1 is synchronous — Phase 2
will wrap this in a subprocess for background runs.
"""

from __future__ import annotations

import time
from typing import Callable

import httpx

from luxe import runner as _runner
from luxe.registry import LuxeConfig
from luxe.router import RouterDecision, route as _route
from luxe.session import Session
from luxe.tasks.model import Subtask, Task, _now, append_log_event, persist


class Orchestrator:
    def __init__(
        self,
        cfg: LuxeConfig,
        session: Session | None = None,
    ) -> None:
        self.cfg = cfg
        self.session = session

    def run(
        self,
        task: Task,
        should_abort: Callable[[], bool] = lambda: False,
    ) -> Task:
        """Drive `task` to completion. Idempotent: already-finished subtasks
        are skipped. `should_abort()` is polled at subtask boundaries so a
        SIGTERM to a background subprocess can stop the run cleanly."""
        if not task.subtasks:
            raise ValueError("task has no subtasks; plan before running")

        task.status = "running"
        persist(task)
        append_log_event(task, {"event": "start", "n_subtasks": len(task.subtasks)})

        t0 = time.monotonic()
        aborted = False

        for sub in task.subtasks:
            if sub.status != "pending":
                continue

            if should_abort():
                aborted = True
                sub.status = "skipped"
                sub.error = "aborted before start"
                sub.completed_at = _now()
                persist(task)
                append_log_event(task, {
                    "event": "skip", "subtask": sub.id, "reason": "aborted",
                })
                continue

            if time.monotonic() - t0 > task.max_wall_s:
                sub.status = "skipped"
                sub.error = "task wall budget exhausted"
                sub.completed_at = _now()
                persist(task)
                append_log_event(task, {
                    "event": "skip", "subtask": sub.id,
                    "reason": "task wall budget",
                })
                continue

            sub.status = "running"
            sub.started_at = _now()
            persist(task)
            append_log_event(task, {
                "event": "begin", "subtask": sub.id,
                "title": sub.title, "agent": sub.agent or "(route)",
            })

            self._run_subtask(sub, task)

            if not sub.completed_at:
                sub.completed_at = _now()
            persist(task)
            append_log_event(task, {
                "event": "end", "subtask": sub.id,
                "status": sub.status, "error": sub.error,
                "tool_calls": sub.tool_calls_total, "steps": sub.steps_taken,
                "wall_s": round(sub.wall_s, 1),
            })

        if aborted:
            task.status = "aborted"
        else:
            all_ok = all(s.status in ("done", "skipped") for s in task.subtasks)
            task.status = "done" if all_ok else "blocked"
        task.completed_at = _now()
        persist(task)
        append_log_event(task, {"event": "finish", "status": task.status})
        return task

    # ── internals ──────────────────────────────────────────────────────

    def _run_subtask(self, sub: Subtask, task: Task) -> None:
        for attempt in range(2):  # initial + at most one retry
            try:
                self._dispatch_subtask(sub, task)
                return
            except (httpx.TransportError, ConnectionError) as e:
                if task.retry_on_transport_error and attempt == 0:
                    sub.attempt += 1
                    append_log_event(task, {
                        "event": "retry_transport", "subtask": sub.id,
                        "error": f"{type(e).__name__}: {e}",
                    })
                    continue
                sub.status = "blocked"
                sub.error = f"{type(e).__name__}: {e}"
                return
            except KeyboardInterrupt:
                sub.status = "blocked"
                sub.error = "interrupted"
                task.status = "aborted"
                return
            except Exception as e:  # noqa: BLE001
                sub.status = "blocked"
                sub.error = f"{type(e).__name__}: {e}"
                return

    def _dispatch_subtask(self, sub: Subtask, task: Task) -> None:
        agent = sub.agent or self._pick_agent(sub.title)
        augmented = _augment_with_prior(task, sub)
        decision = RouterDecision(
            agent=agent,
            task=augmented,
            reasoning=f"task orchestrator (subtask {sub.id})",
        )
        result = _runner.dispatch(decision, self.cfg, session=self.session)
        sub.agent = agent
        sub.result_text = result.final_text or ""
        sub.tool_calls = list(result.tool_calls)
        sub.tool_calls_total = result.tool_calls_total
        sub.steps_taken = result.steps_taken
        sub.prompt_tokens = result.prompt_tokens
        sub.completion_tokens = result.completion_tokens
        sub.wall_s = result.wall_s
        if result.aborted:
            sub.status = "blocked"
            sub.error = result.abort_reason or "agent aborted"
        else:
            sub.status = "done"

    def _pick_agent(self, title: str) -> str:
        """Use the router to pick an agent for an unassigned subtask. No
        clarifying questions allowed — this runs inside a task, not an
        interactive turn."""
        decision = _route(
            title, self.cfg,
            ask_fn=lambda _q: "",
            session=None,
        )
        return decision.agent


def _summarize_result(text: str, max_chars: int = 800) -> str:
    """Trim a subtask's final_text for use as prior context. Prefer
    cutting on a sentence boundary so we don't hand the next subtask a
    half-finished thought."""
    t = (text or "").strip()
    if not t or len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    dot = cut.rfind(". ")
    if dot > int(max_chars * 0.5):
        return cut[: dot + 1] + " …"
    return cut + "…"


def _augment_with_prior(task: Task, sub: Subtask) -> str:
    """Prepend a terse summary of completed earlier subtasks to `sub`'s
    title so the dispatched agent can build on prior work. Serial
    execution guarantees a stable order; we never include blocked /
    skipped / pending peers."""
    prior = [
        s for s in task.subtasks
        if s.index < sub.index and s.status == "done" and s.result_text
    ]
    if not prior:
        return sub.title
    parts = ["# Prior findings in this task (use them; don't re-do them)"]
    for s in prior:
        parts.append(f"## Subtask {s.index}. {s.title}")
        parts.append(_summarize_result(s.result_text))
    parts.append("")
    parts.append("# Your task")
    parts.append(sub.title)
    return "\n\n".join(parts)
