"""Phased runner — quality-first orchestration where a high-weight architect
explicitly plans and reviews, while a coder executes atomic tasks in tight
context windows.

State machine
-------------
    PLAN     →  architect (32B Instruct) decomposes goal into atomic
                tasks, groups them into review checkpoints, persists to
                blackboard. Architect unloads.
    EXECUTE  →  coder (14B Coder) loads, walks the current group,
                writing tool-call results onto the blackboard. Each task
                runs in a fresh message list (≤ 4-8K effective ctx).
                Coder unloads at group boundary.
    REVIEW   →  architect re-loads, reads blackboard for the completed
                group, verifies via read_file / lint / typecheck. Decides
                per task: PASS / RETRY / ABORT. Read-only by default;
                opts into runnable verification (lint/typecheck/bash) on
                RETRY decisions.
    RETRY    →  if any task got RETRY: coder re-loads, executes only the
                flagged tasks with the architect's reason injected.
                Bounded by per-task retry budget (default 2) and a
                global hard cap (default 3 task failures across the run).
                Exhausting either → graceful ABORT with a markdown report.

The architect/coder swap is amortized across many atomic tasks (one swap
per group, not per task), keeping the load/unload cost manageable.

Reuses
------
- Blackboard atomic-write helpers in `run_state.py`.
- Tool surface from `worker.py:_build_tools_for_role`.
- Strict tool-side guards in `tools/fs.py` (placeholder/role-leak/
  mass-deletion are blocked at write time, not just at PR time).

Returns
-------
A tuple `(AgentResult, telemetry_dict)` matching the swarm/micro contract
so the orchestrator can fold it into StageMetrics without special-casing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from luxe.agents.architect import _parse_objectives
from luxe.agents.loop import AgentResult, OnToolEvent, run_agent
from luxe.agents.worker import _ROLE_PROMPTS, _build_tools_for_role
from luxe.backend import Backend
from luxe.config import PipelineConfig, RoleConfig
from luxe.run_state import save_blackboard
from luxe.tools.base import ToolCache, ToolDef, ToolFn

BackendForRole = Callable[[str], Backend]

# --- runtime constants ----------------------------------------------------

# Per-task retry budget — architect's RETRY decisions add up to this many
# extra attempts before a task is marked failed.
DEFAULT_PER_TASK_RETRIES = 2
# Global hard cap on task failures across the whole run. Hitting this
# triggers graceful abort with a markdown report; we'd rather quit than
# ship hallucinated code.
DEFAULT_RUN_FAILURE_CAP = 3
# Default group size when the architect doesn't specify one. Smaller groups
# give the architect more frequent checkpoints; larger groups amortize
# model-swap cost. 4 strikes a reasonable balance for ≤10-task runs.
DEFAULT_GROUP_SIZE = 4


# --- data shapes (for the blackboard) -------------------------------------

@dataclass
class AtomicTask:
    """One unit of work the coder executes in a single agent loop."""
    id: str
    title: str
    role: str = "worker_code"   # which tool surface to use
    scope: str = "."
    success_criterion: str = ""  # what "done" looks like; the architect uses this on review
    group: int = 0               # which review checkpoint this belongs to
    status: str = "pending"      # pending | running | done | rejected | aborted
    retries: int = 0
    last_output: str = ""
    last_reason: str = ""        # architect's last rejection reason (injected on retry)
    tool_calls_count: int = 0
    wall_s: float = 0.0


@dataclass
class PhasedTelemetry:
    """Aggregated stats for the orchestrator + bench harness."""
    plan_wall_s: float = 0.0
    execute_wall_s: float = 0.0
    review_wall_s: float = 0.0
    total_atomic_tasks: int = 0
    tasks_passed: int = 0
    tasks_rejected: int = 0
    tasks_aborted: int = 0
    review_cycles: int = 0
    retries_used: int = 0
    aborted: bool = False
    abort_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_wall_s": round(self.plan_wall_s, 2),
            "execute_wall_s": round(self.execute_wall_s, 2),
            "review_wall_s": round(self.review_wall_s, 2),
            "total_atomic_tasks": self.total_atomic_tasks,
            "tasks_passed": self.tasks_passed,
            "tasks_rejected": self.tasks_rejected,
            "tasks_aborted": self.tasks_aborted,
            "review_cycles": self.review_cycles,
            "retries_used": self.retries_used,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
        }


# --- prompts ---------------------------------------------------------------

_PLAN_SYSTEM = """\
You are a chief architect for a code-modification pipeline. Decompose the
user's goal into atomic tasks that a focused coder can execute one at a
time. Group related tasks into review checkpoints — after each group, you
will personally inspect the work.

Each task must:
- Be expressible in ≤ 3 tool calls (read_file, edit_file, write_file, grep).
- Have a clear success criterion that you can verify by reading code.
- Reference exact file paths or symbols when possible.

Group tasks so that:
- Each group can be reviewed end-to-end without needing intermediate context.
- Read-and-understand tasks come before write tasks.
- A failed task in a group does not silently break later groups.

Respond with ONLY a JSON array. Each entry has:
{
  "id": "t1",
  "title": "<short imperative>",
  "role": "worker_read" | "worker_code" | "worker_analyze",
  "scope": "<file or module hint>",
  "success_criterion": "<what done looks like, verifiable by re-reading>",
  "group": 1
}
"""


_REVIEW_SYSTEM = """\
You are a chief architect reviewing a coder's work. You have read the
blackboard for a group of completed tasks. For each task, decide:

- PASS: the work satisfies the success criterion. Verifiable from the
  files in the repo right now.
- RETRY: the work is incomplete, wrong, or bypasses the spirit of the
  task (e.g., placeholder code, wrong language, orphan files that aren't
  imported anywhere). Provide a concrete reason the coder can act on.
- ABORT: the task is impossible as specified, or the model has exhausted
  its retry budget without producing usable code. Document why.

Rules:
- Verify by reading the actual files (use read_file / grep). Do not trust
  the coder's self-report.
- Reject placeholder text, role-named files (worker_*, drafter_*,
  verifier_*), and orphan modules that aren't imported anywhere in the
  project's primary language.
- Reject diffs that delete substantial code without equivalent additions.
- Quality is paramount. Do not approve borderline work to make progress.

Respond with ONLY a JSON array, one entry per task:
[{"id": "t1", "decision": "PASS" | "RETRY" | "ABORT", "reason": "..."}]
"""


# --- runner ----------------------------------------------------------------

def _safe_role_cfg(config: PipelineConfig, *names: str) -> tuple[str, RoleConfig]:
    """Resolve the first matching role; raises if none configured."""
    for n in names:
        if n in config.roles:
            return n, config.role(n)
    raise KeyError(f"None of {names} configured in pipeline.roles")


def _plan(
    backend_for: BackendForRole,
    config: PipelineConfig,
    *,
    goal: str,
    task_type: str,
    repo_summary: str,
) -> tuple[AgentResult, list[AtomicTask]]:
    """PLAN phase: architect decomposes the goal into atomic tasks + groups."""
    role_name, role_cfg = _safe_role_cfg(config, "chief_architect", "architect")
    backend = backend_for(role_name)

    prompt = (
        f"Goal: {goal}\n"
        f"Task type: {task_type}\n\n"
        f"Repository summary:\n{repo_summary}\n\n"
        "Decompose the goal as instructed."
    )
    result = run_agent(
        backend, role_cfg,
        system_prompt=_PLAN_SYSTEM,
        task_prompt=prompt,
        tool_defs=[],
        tool_fns={},
    )
    raw = _parse_objectives(result.final_text)
    tasks: list[AtomicTask] = []
    for i, item in enumerate(raw):
        tasks.append(AtomicTask(
            id=str(item.get("id") or f"t{i+1}"),
            title=item.get("title", "")[:160],
            role=item.get("role", "worker_code"),
            scope=item.get("scope", "."),
            success_criterion=str(item.get("success_criterion", ""))[:300],
            group=int(item.get("group") or ((i // DEFAULT_GROUP_SIZE) + 1)),
        ))
    return result, tasks


def _execute_group(
    backend_for: BackendForRole,
    config: PipelineConfig,
    *,
    tasks: list[AtomicTask],
    languages: frozenset[str] | None,
    extra_tool_defs: list[ToolDef] | None,
    extra_tool_fns: dict[str, ToolFn] | None,
    cache: ToolCache | None,
    on_tool_event: OnToolEvent | None,
    accumulator: AgentResult,
) -> None:
    """EXECUTE phase: coder runs each pending/retry task in the group with a
    fresh message list. Updates each task's status/output in place; appends
    tokens + tool calls to `accumulator`.
    """
    for task in tasks:
        if task.status not in ("pending", "rejected"):
            continue
        coder_role = task.role if task.role in config.roles else "worker_code"
        if coder_role not in config.roles:
            coder_role, _ = _safe_role_cfg(config, "worker_code", "coder")
        role_cfg = config.role(coder_role)
        backend = backend_for(coder_role)
        defs, fns, cacheable = _build_tools_for_role(coder_role, languages)
        if extra_tool_defs:
            defs = defs + list(extra_tool_defs)
        if extra_tool_fns:
            fns = {**fns, **extra_tool_fns}

        system = _ROLE_PROMPTS.get(coder_role, _ROLE_PROMPTS["worker_code"])
        prompt_parts = [
            f"Task: {task.title}",
            f"Scope: {task.scope}",
            f"Success criterion: {task.success_criterion or '(none specified)'}",
        ]
        if task.last_reason:
            prompt_parts.extend([
                "",
                "Architect rejected the prior attempt. Address this exactly:",
                task.last_reason,
            ])
        task.status = "running"
        t0 = time.monotonic()
        out = run_agent(
            backend, role_cfg,
            system_prompt=system,
            task_prompt="\n".join(prompt_parts),
            tool_defs=defs,
            tool_fns=fns,
            cache=cache,
            cacheable=cacheable,
            on_tool_event=on_tool_event,
        )
        task.wall_s += time.monotonic() - t0
        task.last_output = out.final_text[:2000]
        task.tool_calls_count += out.tool_calls_total
        # Status stays "running" — review phase will set PASS/RETRY/ABORT.
        # If the agent aborted (stuck loop, schema rejects), pre-mark
        # rejected so review can decide quickly.
        if out.aborted:
            task.status = "rejected"
            task.last_reason = (
                f"agent aborted before completion: {out.abort_reason}"
            )

        accumulator.prompt_tokens += out.prompt_tokens
        accumulator.completion_tokens += out.completion_tokens
        accumulator.tool_calls_total += out.tool_calls_total
        accumulator.schema_rejects += out.schema_rejects
        accumulator.peak_context_pressure = max(
            accumulator.peak_context_pressure, out.peak_context_pressure,
        )
        accumulator.tool_calls.extend(out.tool_calls)


def _review_group(
    backend_for: BackendForRole,
    config: PipelineConfig,
    *,
    tasks: list[AtomicTask],
    cache: ToolCache | None,
    accumulator: AgentResult,
) -> list[dict[str, str]]:
    """REVIEW phase: architect reads blackboard for completed tasks and
    decides PASS / RETRY / ABORT per task. Returns the verdicts.

    Read-only by default — the architect uses read_file + grep to verify.
    Runnable verification (lint/typecheck/bash) is enabled when any task
    was already rejected by the executor (i.e., needs deeper inspection).
    """
    role_name, role_cfg = _safe_role_cfg(config, "chief_architect", "architect")
    backend = backend_for(role_name)

    needs_runnable = any(t.status == "rejected" for t in tasks)
    review_role = "worker_analyze" if needs_runnable else "worker_read"
    defs, fns, cacheable = _build_tools_for_role(review_role, languages=None)

    summary_lines: list[str] = []
    for t in tasks:
        summary_lines.append(
            f"- id={t.id} title={t.title!r} status={t.status} "
            f"scope={t.scope!r} retries={t.retries}\n"
            f"  success_criterion: {t.success_criterion}\n"
            f"  last_output: {t.last_output[:600]!r}"
        )
    prompt = (
        f"Review the following {len(tasks)} task(s) executed by the coder. "
        "Verify each by reading the actual files in the repo. Be strict: "
        "reject placeholder code, role-named files, orphan modules, and "
        "language-mismatched files (e.g. .py in a JS project).\n\n"
        + "\n".join(summary_lines)
    )

    out = run_agent(
        backend, role_cfg,
        system_prompt=_REVIEW_SYSTEM,
        task_prompt=prompt,
        tool_defs=defs,
        tool_fns=fns,
        cache=cache,
        cacheable=cacheable,
    )
    accumulator.prompt_tokens += out.prompt_tokens
    accumulator.completion_tokens += out.completion_tokens
    accumulator.tool_calls_total += out.tool_calls_total
    accumulator.tool_calls.extend(out.tool_calls)

    verdicts: list[dict[str, str]] = []
    text = (out.final_text or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines()
                         if not l.strip().startswith("```"))
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            arr = json.loads(text[start:end + 1])
            if isinstance(arr, list):
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    verdicts.append({
                        "id": str(item.get("id", "")),
                        "decision": str(item.get("decision", "RETRY")).upper(),
                        "reason": str(item.get("reason", ""))[:600],
                    })
        except json.JSONDecodeError:
            pass

    # Default-RETRY safety: if the architect couldn't emit clean JSON, we
    # don't auto-pass. Mark every still-running task as RETRY with a
    # diagnostic so the next pass gets a fresh chance.
    by_id = {v["id"]: v for v in verdicts}
    for t in tasks:
        if t.id not in by_id:
            verdicts.append({
                "id": t.id,
                "decision": "RETRY",
                "reason": "architect emitted no verdict for this task — "
                          "default-retry to avoid silent passes",
            })
    return verdicts


def run_phased(
    backend_for: BackendForRole,
    config: PipelineConfig,
    *,
    goal: str,
    task_type: str,
    repo_summary: str = "",
    languages: frozenset[str] | None = None,
    extra_tool_defs: list[ToolDef] | None = None,
    extra_tool_fns: dict[str, ToolFn] | None = None,
    cache: ToolCache | None = None,
    on_tool_event: OnToolEvent | None = None,
    run_id: str | None = None,
    per_task_retries: int = DEFAULT_PER_TASK_RETRIES,
    run_failure_cap: int = DEFAULT_RUN_FAILURE_CAP,
) -> tuple[AgentResult, PhasedTelemetry]:
    """Phased runner — see module docstring for the state machine.

    The orchestrator integrates this by branching on `RunMode.PHASED`,
    similar to how `microloop` is dispatched today.
    """
    aggregate = AgentResult()
    telem = PhasedTelemetry()
    overall_t0 = time.monotonic()

    # --- PLAN ---
    plan_t0 = time.monotonic()
    plan_result, tasks = _plan(
        backend_for, config,
        goal=goal, task_type=task_type, repo_summary=repo_summary,
    )
    telem.plan_wall_s = time.monotonic() - plan_t0
    aggregate.prompt_tokens += plan_result.prompt_tokens
    aggregate.completion_tokens += plan_result.completion_tokens
    telem.total_atomic_tasks = len(tasks)

    if not tasks:
        aggregate.final_text = (
            "Phased runner: architect produced no atomic tasks. "
            "Goal is either too vague to plan or the architect failed to "
            "emit valid JSON. Aborting before any code changes."
        )
        aggregate.aborted = True
        aggregate.abort_reason = "no_plan"
        telem.aborted = True
        telem.abort_reason = "no_plan"
        aggregate.wall_s = time.monotonic() - overall_t0
        if run_id:
            save_blackboard(run_id, 0, _blackboard_dict(tasks, telem))
        return aggregate, telem

    # --- EXECUTE / REVIEW / RETRY loop, per group ---
    groups = sorted({t.group for t in tasks})
    for grp in groups:
        group_tasks = [t for t in tasks if t.group == grp]

        for attempt in range(per_task_retries + 1):
            # EXECUTE
            exec_t0 = time.monotonic()
            _execute_group(
                backend_for, config,
                tasks=group_tasks, languages=languages,
                extra_tool_defs=extra_tool_defs, extra_tool_fns=extra_tool_fns,
                cache=cache, on_tool_event=on_tool_event,
                accumulator=aggregate,
            )
            telem.execute_wall_s += time.monotonic() - exec_t0

            # REVIEW
            review_t0 = time.monotonic()
            verdicts = _review_group(
                backend_for, config,
                tasks=group_tasks, cache=cache, accumulator=aggregate,
            )
            telem.review_wall_s += time.monotonic() - review_t0
            telem.review_cycles += 1

            # Apply verdicts. Tasks that PASS finalize; RETRY/ABORT updated.
            still_pending: list[AtomicTask] = []
            for t in group_tasks:
                v = next((x for x in verdicts if x["id"] == t.id), None)
                if v is None:
                    continue
                d = v["decision"]
                t.last_reason = v["reason"]
                if d == "PASS":
                    t.status = "done"
                elif d == "ABORT":
                    t.status = "aborted"
                else:  # RETRY
                    if t.retries < per_task_retries:
                        t.retries += 1
                        t.status = "pending"  # will rerun next attempt
                        still_pending.append(t)
                        telem.retries_used += 1
                    else:
                        t.status = "aborted"
                        t.last_reason = (
                            f"retry budget exhausted ({per_task_retries}); "
                            f"last reason: {v['reason']}"
                        )

            # Persist after every review cycle so an interrupted run is
            # inspectable.
            if run_id:
                save_blackboard(run_id, 0, _blackboard_dict(tasks, telem))

            # Hard-cap: too many failed tasks across the run = abort.
            failed_so_far = sum(1 for t in tasks if t.status == "aborted")
            if failed_so_far >= run_failure_cap:
                aggregate.aborted = True
                aggregate.abort_reason = (
                    f"run_failure_cap reached ({failed_so_far}/{run_failure_cap}); "
                    "graceful abort"
                )
                telem.aborted = True
                telem.abort_reason = aggregate.abort_reason
                break

            if not still_pending:
                break  # group complete

        if aggregate.aborted:
            break

    telem.tasks_passed = sum(1 for t in tasks if t.status == "done")
    telem.tasks_rejected = sum(1 for t in tasks if t.status == "rejected")
    telem.tasks_aborted = sum(1 for t in tasks if t.status == "aborted")

    # Synthesize final report. If aborted, surface the abort context so
    # downstream code (PR cycle, bench grader) can detect graceful failure.
    if aggregate.aborted:
        aggregate.final_text = _build_abort_report(tasks, telem)
    else:
        aggregate.final_text = _build_success_report(tasks, telem)

    aggregate.wall_s = time.monotonic() - overall_t0
    aggregate.steps = telem.review_cycles
    if run_id:
        save_blackboard(run_id, 0, _blackboard_dict(tasks, telem))
    return aggregate, telem


# --- blackboard + reporting helpers ---------------------------------------

def _blackboard_dict(tasks: list[AtomicTask], telem: PhasedTelemetry) -> dict:
    return {
        "subtask_idx": 0,
        "version": 1,
        "objective": "phased run",
        "scope": ".",
        "telemetry": telem.to_dict(),
        "micro_steps": [
            {
                "id": t.id, "kind": "phased",
                "spec": t.title, "scope": t.scope,
                "specialist": t.role, "status": t.status,
                "verdict": {"accept": t.status == "done", "reason": t.last_reason},
                "output_text": t.last_output[:1500],
                "tool_call_count": t.tool_calls_count,
                "wall_s": round(t.wall_s, 2),
                "retries": t.retries,
                "group": t.group,
                "success_criterion": t.success_criterion,
            }
            for t in tasks
        ],
        "facts": {},
        "decisions": [],
    }


def _build_abort_report(tasks: list[AtomicTask], telem: PhasedTelemetry) -> str:
    lines = [
        "# Phased run — aborted",
        "",
        f"**Reason**: {telem.abort_reason}",
        "",
        f"- Total atomic tasks: {telem.total_atomic_tasks}",
        f"- Passed: {telem.tasks_passed}",
        f"- Aborted: {telem.tasks_aborted}",
        f"- Retries used: {telem.retries_used}",
        f"- Review cycles: {telem.review_cycles}",
        "",
        "## Per-task status",
        "",
    ]
    for t in tasks:
        lines.append(
            f"- **{t.id}** [{t.status}] {t.title}\n"
            f"  - Group: {t.group}\n"
            f"  - Retries: {t.retries}\n"
            f"  - Last reason: {t.last_reason or '(none)'}"
        )
    lines.extend([
        "",
        "_The architect chose graceful abort over shipping suspect changes. "
        "Inspect the blackboard at ~/.luxe/runs/<run_id>/blackboard/0.json "
        "for the full trace._",
    ])
    return "\n".join(lines)


def _build_success_report(tasks: list[AtomicTask], telem: PhasedTelemetry) -> str:
    lines = [
        "# Phased run — complete",
        "",
        f"- Total atomic tasks: {telem.total_atomic_tasks}",
        f"- Passed: {telem.tasks_passed}",
        f"- Retries used: {telem.retries_used}",
        f"- Review cycles: {telem.review_cycles}",
        f"- Wall (plan/execute/review): "
        f"{telem.plan_wall_s:.0f}s / {telem.execute_wall_s:.0f}s / "
        f"{telem.review_wall_s:.0f}s",
        "",
        "## Tasks completed",
        "",
    ]
    for t in tasks:
        marker = "✓" if t.status == "done" else "·"
        lines.append(f"- {marker} **{t.id}** {t.title} (group {t.group})")
    return "\n".join(lines)
