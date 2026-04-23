"""Multi-subtask runner.

Drives a Task through its Subtasks, dispatching each to the appropriate
specialist via luxe.runner, persisting progress after every state change,
and enforcing the per-task wall budget. Phase 1 is synchronous — Phase 2
will wrap this in a subprocess for background runs.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

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
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.session = session
        # Optional live-event hook. The REPL wires this up in sync mode
        # to tail-print progress lines. Background subprocess runs leave
        # it None — their equivalent is log.jsonl + /tasks tail.
        self.on_event = on_event

    def _emit(self, task: Task, event: dict[str, Any]) -> None:
        """Persist the event to log.jsonl AND fan out to the optional
        live-stream callback, if any."""
        append_log_event(task, event)
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001
                pass  # never let a broken UI sink abort a task

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
        self._emit(task, {"event": "start", "n_subtasks": len(task.subtasks)})

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
                self._emit(task, {
                    "event": "skip", "subtask": sub.id, "reason": "aborted",
                })
                continue

            if time.monotonic() - t0 > task.max_wall_s:
                sub.status = "skipped"
                sub.error = "task wall budget exhausted"
                sub.completed_at = _now()
                persist(task)
                self._emit(task, {
                    "event": "skip", "subtask": sub.id,
                    "reason": "task wall budget",
                })
                continue

            sub.status = "running"
            sub.started_at = _now()
            persist(task)
            self._emit(task, {
                "event": "begin", "subtask": sub.id,
                "title": sub.title, "agent": sub.agent or "(route)",
            })

            self._run_subtask(sub, task)

            if not sub.completed_at:
                sub.completed_at = _now()
            persist(task)
            self._emit(task, {
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
        self._emit(task, {"event": "finish", "status": task.status})
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
                    self._emit(task, {
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
            return

        # Tool-use enforcement for review/refactor inspection subtasks.
        # A proper pass requires BOTH orientation (list_dir/glob) AND
        # real reading (read_file/grep). "1 call, list_dir only" is
        # barely better than 0 — the model peeked at filenames and
        # guessed. So the threshold is tool-type diversity, not just
        # a count.
        if (
            agent in ("review", "refactor")
            and _is_inspection_title(sub.title)
            and _inspection_too_shallow(result.tool_calls)
        ):
            shallow_reason = (
                "no tool calls at all"
                if result.tool_calls_total == 0
                else f"only used {', '.join({c.name for c in result.tool_calls})} — "
                     "no real reading"
            )
            self._emit(task, {
                "event": "tool_use_retry",
                "subtask": sub.id,
                "reason": f"inspection too shallow: {shallow_reason}",
            })
            nudge = (
                "\n\n# Retry — inspection was too shallow\n"
                f"Your previous attempt made shallow tool use ({shallow_reason}). "
                "A real inspection pass needs BOTH orientation (list_dir/glob "
                "to find files) AND reading (grep/read_file against specific "
                "files or patterns). Re-attempt now: first list or glob, then "
                "grep for relevant patterns or read specific files, then "
                "produce findings grounded in what you read.\n"
                "\n"
                "If the repo genuinely has no source files (just .gitignore "
                "or metadata), report that explicitly: 'Repo has no source "
                "files; no inspection surface.' Do NOT write literal "
                "placeholder phrases like 'I grepped X and read Y' — cite "
                "the actual files and patterns you used."
            )
            retry_decision = RouterDecision(
                agent=agent,
                task=augmented + nudge,
                reasoning=f"task orchestrator retry (subtask {sub.id}, shallow inspection)",
            )
            retry_result = _runner.dispatch(
                retry_decision, self.cfg, session=self.session
            )
            # Accept the retry only if it did better on the depth axis.
            if not _inspection_too_shallow(retry_result.tool_calls):
                sub.result_text = retry_result.final_text or sub.result_text
                sub.tool_calls = list(retry_result.tool_calls)
                sub.tool_calls_total = retry_result.tool_calls_total
                sub.steps_taken += retry_result.steps_taken
                sub.prompt_tokens += retry_result.prompt_tokens
                sub.completion_tokens += retry_result.completion_tokens
                sub.wall_s += retry_result.wall_s
            else:
                sub.error = (
                    "shallow inspection (retry also didn't read files) — "
                    "findings may not be grounded in code"
                )

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


_INSPECTION_VERBS = re.compile(
    r"\b(search|look|find|check|scan|inspect|identify|analyze|audit|"
    r"review\s+for|list\s+(directory|files)|read\s+(the\s+)?readme)\b",
    re.IGNORECASE,
)
_SYNTHESIS_VERBS = re.compile(
    r"\b(summari[zs]e|synthesi[zs]e|generate\s+(a\s+)?report|write\s+(the\s+)?"
    r"(report|summary)|produce\s+(a\s+)?report)\b",
    re.IGNORECASE,
)


def _is_inspection_title(title: str) -> bool:
    """Heuristic: true if the subtask title sounds like code inspection
    (tool use expected), false if it sounds like synthesis (tool-free
    report generation from earlier findings)."""
    t = (title or "").strip()
    if not t:
        return False
    if _SYNTHESIS_VERBS.search(t):
        return False
    return bool(_INSPECTION_VERBS.search(t))


# Real inspection = orientation (list_dir / glob) + reading
# (grep / read_file). One-call-only or list_dir-only runs are flagged
# as shallow and retried with a stronger nudge.
_ORIENTATION_TOOLS = frozenset({"list_dir", "glob"})
_READING_TOOLS = frozenset({"read_file", "grep"})


def _inspection_too_shallow(tool_calls) -> bool:
    """True when an inspection subtask didn't do enough to be credible.
    Zero calls, a single list_dir, or orientation-only (list_dir/glob
    with no reading) all count as shallow."""
    if not tool_calls:
        return True
    names = {c.name for c in tool_calls}
    did_orient = bool(names & _ORIENTATION_TOOLS)
    did_read = bool(names & _READING_TOOLS)
    # Need real reading — just walking the tree doesn't count.
    if not did_read:
        return True
    # Having read but not orient is fine (the planner/prior subtask
    # may have supplied filenames already).
    _ = did_orient
    return False


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
