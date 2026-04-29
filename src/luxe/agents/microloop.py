"""Microloop runner — small-model draft/verify feedback loop.

Alternative to run_worker: for each parent worker subtask, the micro-architect
breaks the objective into 3-6 atomic sub-objectives. Each sub-objective is
executed by a specialist drafter (using the parent role's tool surface) and
reviewed by a small verifier. Verifier rejections trigger one bounded retry.

Per-step state lives on a structured blackboard JSON at
~/.luxe/runs/<run_id>/blackboard/<subtask_idx>.json (atomic write per step,
mirroring save_stage semantics).

Each step builds a fresh message list — this is the KV-cache reset proxy that
keeps the model in its high-attention zone (≤2-4K effective context).

Returns (AgentResult, telemetry dict) so the orchestrator can fold microloop
specifics (microstep_count, rejects, blackboard size, avg decode rate) onto
StageMetrics without changing the AgentResult schema for the swarm path.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from luxe.agents.architect import _parse_objectives
from luxe.agents.loop import AgentResult, OnToolEvent, run_agent
from luxe.agents.worker import _ROLE_PROMPTS, _build_tools_for_role
from luxe.backend import Backend
from luxe.config import PipelineConfig
from luxe.run_state import save_blackboard
from luxe.tools.base import ToolCache, ToolDef, ToolFn

BackendForRole = Callable[[str], Backend]

_MAX_VERIFIER_RETRIES = 1


_MICRO_ARCH_SYSTEM = """\
You are a micro-architect. Break the given objective into 3-6 atomic
sub-objectives that can each be executed in <=2 tool calls. Each
sub-objective targets a tiny, verifiable outcome.

Respond with ONLY a JSON array. No markdown.

Example output (objective: "find off-by-one in pagination"):
[
  {"title": "Read src/api/list.py focusing on pagination logic"},
  {"title": "Grep for offset/limit calculations across api module"},
  {"title": "Identify the line where offset is computed incorrectly"},
  {"title": "Confirm the bug by tracing one example input"}
]
"""


_VERIFIER_SYSTEM = """\
You are a verifier. Given a sub-objective and a draft output, decide if the
draft accomplishes the sub-objective. Be strict but charitable: accept if the
draft makes substantial progress; reject only if it is empty, off-topic, or
contradicted by cited code.

Respond with ONLY a JSON object: {"accept": true|false, "reason": "..."}
"""


def _plan_microsteps(
    backend: Backend,
    role_cfg,
    *,
    objective: str,
    scope: str,
    prior_findings: str,
) -> tuple[AgentResult, list[dict[str, Any]]]:
    """Decompose one parent objective into atomic micro-objectives."""
    pf = (prior_findings or "")[:1200] or "(none)"
    prompt = (
        f"Objective: {objective}\n"
        f"Scope: {scope}\n"
        f"Prior findings (truncated):\n{pf}"
    )
    result = run_agent(
        backend, role_cfg,
        system_prompt=_MICRO_ARCH_SYSTEM,
        task_prompt=prompt,
        tool_defs=[],
        tool_fns={},
    )
    parsed = _parse_objectives(result.final_text)
    cleaned: list[dict[str, Any]] = []
    for s in parsed:
        title = s.get("title", "").strip()
        if title:
            cleaned.append({"title": title})
    return result, cleaned[:6]


def _verify(
    backend: Backend,
    role_cfg,
    *,
    objective: str,
    draft_text: str,
) -> tuple[AgentResult, bool, str]:
    """Verifier pass — return (agent_result, accept, reason)."""
    prompt = f"Sub-objective: {objective}\n\nDraft output:\n{draft_text[:2000]}"
    result = run_agent(
        backend, role_cfg,
        system_prompt=_VERIFIER_SYSTEM,
        task_prompt=prompt,
        tool_defs=[],
        tool_fns={},
    )

    text = (result.final_text or "").strip()
    if text.startswith("```"):
        lines = [l for l in text.splitlines() if not l.strip().startswith("```")]
        text = "\n".join(lines)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return result, True, "verifier emitted no JSON; default-accept"

    try:
        obj = json.loads(text[start:end + 1])
        accept = bool(obj.get("accept", True))
        reason = str(obj.get("reason", ""))[:500]
        return result, accept, reason
    except (json.JSONDecodeError, KeyError, AttributeError):
        return result, True, "verifier output unparseable; default-accept"


def run_microloop(
    backend_for: BackendForRole,
    config: PipelineConfig,
    *,
    role: str,
    task_prompt: str,
    objective_title: str = "",
    scope: str = ".",
    prior_findings: str = "",
    languages: frozenset[str] | None = None,
    extra_tool_defs: list[ToolDef] | None = None,
    extra_tool_fns: dict[str, ToolFn] | None = None,
    cache: ToolCache | None = None,
    on_tool_event: OnToolEvent | None = None,
    run_id: str | None = None,
    subtask_idx: int = 0,
) -> tuple[AgentResult, dict[str, Any]]:
    """Run a draft/verify microloop for one worker subtask.

    The aggregated AgentResult mirrors run_worker's return contract so the
    orchestrator's downstream wiring (status, escalation gate, checkpoint
    serialization) is unchanged. Microloop-specific telemetry comes back in
    a separate dict so StageMetrics can absorb it without polluting the
    AgentResult schema for the swarm path.
    """
    t0 = time.monotonic()
    aggregate = AgentResult()

    # Specialist roles, with graceful fallback if a config doesn't define
    # the new microloop-specific roles yet.
    micro_arch_role = "micro_architect" if "micro_architect" in config.roles else "architect"
    drafter_role = role  # parent worker role drives tool surface
    verifier_role = "verifier" if "verifier" in config.roles else "validator"

    # 1. Plan
    plan_backend = backend_for(micro_arch_role)
    plan_cfg = config.role(micro_arch_role)
    plan_objective = objective_title or task_prompt[:200]
    plan_result, micro_steps = _plan_microsteps(
        plan_backend, plan_cfg,
        objective=plan_objective,
        scope=scope,
        prior_findings=prior_findings,
    )
    aggregate.prompt_tokens += plan_result.prompt_tokens
    aggregate.completion_tokens += plan_result.completion_tokens

    if not micro_steps:
        # Empty plan → single-step fallback covering the full parent objective.
        micro_steps = [{"title": plan_objective}]

    # 2. Per-step draft → verify (with bounded retry).
    drafter_backend = backend_for(drafter_role)
    drafter_cfg = config.role(drafter_role)
    drafter_defs, drafter_fns, drafter_cacheable = _build_tools_for_role(drafter_role, languages)
    if extra_tool_defs:
        drafter_defs = drafter_defs + list(extra_tool_defs)
    if extra_tool_fns:
        drafter_fns = {**drafter_fns, **extra_tool_fns}
    drafter_system = _ROLE_PROMPTS.get(drafter_role, _ROLE_PROMPTS["worker_read"])

    verifier_backend = backend_for(verifier_role)
    verifier_cfg = config.role(verifier_role)

    bb_steps: list[dict[str, Any]] = []
    facts: dict[str, list[str]] = {"file_paths": []}
    decisions: list[dict[str, Any]] = []
    accepted_outputs: list[str] = []
    rejects = 0
    decode_rates: list[float] = []
    bb_path = None

    for i, ms in enumerate(micro_steps):
        spec = ms["title"]
        retries_left = _MAX_VERIFIER_RETRIES
        injected_reason = ""
        last_draft_text = ""
        last_draft_calls: list[Any] = []
        accepted = False
        last_reason = ""

        while True:
            # Fresh message list per step (KV-reset proxy).
            step_prompt_parts = [
                f"Sub-objective: {spec}",
                f"Scope: {scope}",
                f"Parent objective: {objective_title}",
            ]
            if accepted_outputs:
                ctx = "\n".join(accepted_outputs)[-600:]
                step_prompt_parts.extend(["", "Prior accepted findings:", ctx])
            if injected_reason:
                step_prompt_parts.extend([
                    "",
                    f"Verifier rejected previous attempt: {injected_reason}",
                    "Address this and try again.",
                ])

            draft = run_agent(
                drafter_backend, drafter_cfg,
                system_prompt=drafter_system,
                task_prompt="\n".join(step_prompt_parts),
                tool_defs=drafter_defs,
                tool_fns=drafter_fns,
                cache=cache,
                cacheable=drafter_cacheable,
                on_tool_event=on_tool_event,
            )

            aggregate.prompt_tokens += draft.prompt_tokens
            aggregate.completion_tokens += draft.completion_tokens
            aggregate.tool_calls_total += draft.tool_calls_total
            aggregate.schema_rejects += draft.schema_rejects
            aggregate.peak_context_pressure = max(
                aggregate.peak_context_pressure, draft.peak_context_pressure
            )
            aggregate.tool_calls.extend(draft.tool_calls)
            if draft.completion_tokens > 0 and draft.wall_s > 0:
                decode_rates.append(draft.completion_tokens / draft.wall_s)

            last_draft_text = draft.final_text
            last_draft_calls = draft.tool_calls

            verify_res, accept, reason = _verify(
                verifier_backend, verifier_cfg,
                objective=spec, draft_text=draft.final_text,
            )
            aggregate.prompt_tokens += verify_res.prompt_tokens
            aggregate.completion_tokens += verify_res.completion_tokens
            if verify_res.completion_tokens > 0 and verify_res.wall_s > 0:
                decode_rates.append(verify_res.completion_tokens / verify_res.wall_s)

            last_reason = reason
            if accept or retries_left <= 0:
                accepted = accept
                if not accept:
                    rejects += 1
                break
            rejects += 1
            retries_left -= 1
            injected_reason = reason

        bb_steps.append({
            "id": f"s{i}",
            "kind": "draft+verify",
            "spec": spec,
            "status": "done" if accepted else "rejected",
            "specialist": drafter_role,
            "verdict": {"accept": accepted, "reason": "" if accepted else last_reason},
            "output_text": last_draft_text[:4000],
            "tool_call_count": len(last_draft_calls),
        })

        for tc in last_draft_calls:
            args = getattr(tc, "arguments", {}) or {}
            if isinstance(args, dict):
                p = args.get("path") or args.get("file_path")
                if isinstance(p, str) and p not in facts["file_paths"]:
                    facts["file_paths"].append(p)

        if accepted:
            accepted_outputs.append(last_draft_text)
            decisions.append({"step": f"s{i}", "choice": "accepted", "spec": spec})
        else:
            decisions.append({
                "step": f"s{i}", "choice": "rejected",
                "spec": spec, "reason": last_reason,
            })

        if run_id:
            bb_path = save_blackboard(run_id, subtask_idx, {
                "subtask_idx": subtask_idx,
                "objective": objective_title,
                "scope": scope,
                "micro_steps": bb_steps,
                "facts": facts,
                "decisions": decisions,
                "version": 1,
            })

    if accepted_outputs:
        aggregate.final_text = "\n\n".join(accepted_outputs)
    else:
        aggregate.final_text = (
            "Microloop produced no accepted outputs. Verifier rejected all "
            f"{len(micro_steps)} sub-objective(s)."
        )
        aggregate.aborted = True
        aggregate.abort_reason = "all microsteps rejected"

    aggregate.wall_s = time.monotonic() - t0
    aggregate.steps = len(micro_steps)

    bb_size = bb_path.stat().st_size if bb_path is not None else 0
    avg_decode = sum(decode_rates) / len(decode_rates) if decode_rates else 0.0

    telemetry = {
        "microstep_count": len(micro_steps),
        "microstep_rejects": rejects,
        "blackboard_bytes": bb_size,
        "decode_tok_per_s_avg": avg_decode,
    }
    return aggregate, telemetry
