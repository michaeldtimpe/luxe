"""Pipeline orchestrator — drives Architect → Workers → Validator → Synthesizer."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from luxe.agents.architect import run_architect
from luxe.agents.loop import AgentResult
from luxe.agents.microloop import run_microloop
from luxe.agents.synthesizer import run_synthesizer
from luxe.agents.validator import (
    ValidatorEnvelope,
    ValidatorFinding,
    ValidatorRemoved,
    run_validator,
)
from luxe.agents.worker import run_worker
from luxe.backend import Backend
from luxe.config import PipelineConfig
from luxe.pipeline.model import PipelineRun, StageMetrics, Status, Subtask
from luxe.run_state import load_stage, save_stage
from luxe.tools.base import ToolCache, ToolCall
from luxe.tools.fs import set_repo_root

console = Console()
OnEvent = Callable[[dict[str, Any]], None]


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fmt_tok(prompt: int, completion: int) -> str:
    def _k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
    return f"{_k(prompt)}+{_k(completion)} tok"


def _survey_repo(repo_path: str) -> str:
    """Token-budgeted repo summary for the architect.

    Uses repo_index.build_repo_summary which surfaces symbol_index_coverage
    so the architect knows when to fall back to bm25_search vs find_symbol.
    The pipeline's session-level symbol index is consulted via the module
    global; if the index hasn't been built, coverage is reported as empty
    and the architect treats every language as BM25-only.
    """
    from luxe import symbols as symbols_mod
    from luxe.repo_index import build_repo_summary
    coverage = symbols_mod._index.coverage if symbols_mod._index else {}
    summary = build_repo_summary(repo_path, symbol_coverage=coverage)
    return summary.render()


def _detect_languages(repo_path: str) -> frozenset[str]:
    """Detect languages present in the repo."""
    p = Path(repo_path)
    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "typescript", ".jsx": "javascript", ".rs": "rust",
        ".go": "go",
    }
    found: set[str] = set()
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv"}]
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in lang_map:
                found.add(lang_map[ext])
    return frozenset(found)


def _build_prior_findings(
    run: PipelineRun,
    current_index: int,
) -> str:
    """Recency-weighted prior findings for worker context augmentation."""
    parts: list[str] = []

    for sub in run.subtasks:
        if sub.index >= current_index or sub.status != Status.DONE:
            continue
        if not sub.result_text:
            continue

        distance = current_index - sub.index

        if distance <= 1:
            text = sub.result_text[:800]
        elif distance <= 3:
            text = sub.result_text[:400]
        else:
            finding_count = sub.result_text.count("\n") + 1
            text = f"[{sub.title}] — done, {finding_count} lines of findings"

        parts.append(f"### Subtask {sub.index}: {sub.title} ({sub.role})\n{text}")

    return "\n\n".join(parts)


def _tool_call_to_dict(tc: ToolCall) -> dict:
    return {
        "id": tc.id, "name": tc.name, "arguments": tc.arguments,
        "result": tc.result, "error": tc.error,
        "cached": tc.cached, "duplicate": tc.duplicate,
        "bytes_out": tc.bytes_out, "wall_s": tc.wall_s,
    }


def _tool_call_from_dict(d: dict) -> ToolCall:
    return ToolCall(
        id=d.get("id", ""), name=d.get("name", ""),
        arguments=d.get("arguments", {}) or {},
        result=d.get("result", ""), error=d.get("error"),
        cached=bool(d.get("cached", False)),
        duplicate=bool(d.get("duplicate", False)),
        bytes_out=int(d.get("bytes_out", 0)),
        wall_s=float(d.get("wall_s", 0.0)),
    )


def _stage_metrics_to_dict(m: StageMetrics) -> dict:
    return {
        "wall_s": m.wall_s, "prompt_tokens": m.prompt_tokens,
        "completion_tokens": m.completion_tokens, "tool_calls": m.tool_calls,
        "schema_rejects": m.schema_rejects,
        "peak_context_pressure": m.peak_context_pressure,
        "model": m.model, "model_swap_s": m.model_swap_s,
        "cache_hits": m.cache_hits, "cache_misses": m.cache_misses,
        "microstep_count": m.microstep_count,
        "microstep_rejects": m.microstep_rejects,
        "blackboard_bytes": m.blackboard_bytes,
        "decode_tok_per_s_avg": m.decode_tok_per_s_avg,
    }


def _stage_metrics_from_dict(d: dict) -> StageMetrics:
    return StageMetrics(
        wall_s=float(d.get("wall_s", 0.0)),
        prompt_tokens=int(d.get("prompt_tokens", 0)),
        completion_tokens=int(d.get("completion_tokens", 0)),
        tool_calls=int(d.get("tool_calls", 0)),
        schema_rejects=int(d.get("schema_rejects", 0)),
        peak_context_pressure=float(d.get("peak_context_pressure", 0.0)),
        model=d.get("model", ""),
        model_swap_s=float(d.get("model_swap_s", 0.0)),
        cache_hits=int(d.get("cache_hits", 0)),
        cache_misses=int(d.get("cache_misses", 0)),
        microstep_count=int(d.get("microstep_count", 0)),
        microstep_rejects=int(d.get("microstep_rejects", 0)),
        blackboard_bytes=int(d.get("blackboard_bytes", 0)),
        decode_tok_per_s_avg=float(d.get("decode_tok_per_s_avg", 0.0)),
    )


def _envelope_to_dict(env: ValidatorEnvelope) -> dict:
    return {
        "status": env.status,
        "verified": [
            {"path": f.path, "line": f.line, "snippet": f.snippet,
             "severity": f.severity, "description": f.description}
            for f in env.verified
        ],
        "removed": [
            {"original": r.original, "reason": r.reason} for r in env.removed
        ],
        "summary": env.summary,
    }


def _envelope_from_dict(d: dict) -> ValidatorEnvelope:
    return ValidatorEnvelope(
        status=d.get("status", "cleared"),
        verified=[ValidatorFinding(**v) for v in d.get("verified", []) if isinstance(v, dict)],
        removed=[ValidatorRemoved(**r) for r in d.get("removed", []) if isinstance(r, dict)],
        summary=d.get("summary", ""),
    )


class PipelineOrchestrator:
    def __init__(
        self,
        config: PipelineConfig,
        on_event: OnEvent | None = None,
        run_id: str | None = None,
        extra_tool_defs: list[Any] | None = None,
        extra_tool_fns: dict[str, Any] | None = None,
        execution_mode: str | None = None,
    ):
        self.config = config
        self.on_event = on_event
        self._backends: dict[str, Backend] = {}
        # When set, each completed stage is checkpointed under
        # ~/.luxe/runs/<run_id>/stages/. Stages already on disk are reloaded
        # and skipped, enabling `luxe maintain --resume <run_id>`.
        self.run_id = run_id
        # MCP-discovered tools, injected into every worker invocation. They
        # are namespaced (mcp__server__tool) so they cannot collide with
        # native tools, and they are NOT added to ToolCache.
        self.extra_tool_defs = extra_tool_defs or []
        self.extra_tool_fns = extra_tool_fns or {}
        # Per-call override beats config default. "swarm" → run_worker per
        # subtask; "microloop" → run_microloop (small-agent feedback loop).
        self.execution_mode = execution_mode or config.execution

    # --- checkpoint helpers ------------------------------------------------

    def _ck_load(self, name: str) -> dict | None:
        if not self.run_id:
            return None
        return load_stage(self.run_id, name)

    def _ck_save(self, name: str, data: dict) -> None:
        if not self.run_id:
            return
        save_stage(self.run_id, name, data)

    def _ck_subtask_to_dict(self, sub: Subtask) -> dict:
        return {
            "index": sub.index,
            "id": sub.id,
            "title": sub.title,
            "role": sub.role,
            "scope": sub.scope,
            "expected_tools": sub.expected_tools,
            "status": sub.status.value,
            "result_text": sub.result_text,
            "escalated_from": sub.escalated_from,
            "tool_calls": [_tool_call_to_dict(tc) for tc in sub.tool_calls],
            "metrics": _stage_metrics_to_dict(sub.metrics),
        }

    def _ck_subtask_from_dict(self, d: dict) -> Subtask:
        sub = Subtask(
            id=d.get("id", ""),
            index=int(d.get("index", 0)),
            title=d.get("title", ""),
            role=d.get("role", ""),
            scope=d.get("scope", "."),
            expected_tools=int(d.get("expected_tools", 3)),
            status=Status(d.get("status", "pending")),
            result_text=d.get("result_text", ""),
            escalated_from=d.get("escalated_from"),
        )
        sub.tool_calls = [_tool_call_from_dict(tc) for tc in d.get("tool_calls", [])]
        sub.metrics = _stage_metrics_from_dict(d.get("metrics", {}))
        return sub

    def _get_backend(self, role_name: str) -> Backend:
        model = self.config.model_for_role(role_name)
        if model not in self._backends:
            self._backends[model] = Backend(
                base_url=self.config.omlx_base_url,
                model=model,
            )
            # First time we touch this model: thermal guard. oMLX may need a
            # moment to load it. Skip if /v1/models is unreachable — chat()
            # will then fail-fast or retry per its own rules.
            try:
                self._backends[model].thermal_guard(model, settle_s=2.0, max_wait_s=30.0)
            except Exception:
                pass
        return self._backends[model]

    def _emit(self, run: PipelineRun, kind: str, **data: Any) -> None:
        run.add_event(kind, **data)
        if self.on_event:
            self.on_event(run.events[-1])

    def run(
        self,
        goal: str,
        task_type: str,
        repo_path: str,
        should_abort: Callable[[], bool] | None = None,
        initial_context: str = "",
    ) -> PipelineRun:
        """Execute the full pipeline: Architect → Workers → Validator → Synthesizer.

        `initial_context` (optional) — passed to the architect, typically the
        rendered EscalationContext from a single→swarm hand-off.
        """

        set_repo_root(repo_path)
        task_cfg = self.config.task_type(task_type)
        languages = _detect_languages(repo_path)

        run = PipelineRun(goal=goal, task_type=task_type, repo_path=repo_path)
        run.status = Status.RUNNING
        t0 = time.monotonic()
        pipeline_start_ts = _ts()

        self._emit(run, "start", goal=goal, task_type=task_type, repo=repo_path)

        # --- ARCHITECT ---
        if should_abort and should_abort():
            run.status = Status.BLOCKED
            return run

        cached = self._ck_load("architect")
        if cached is not None:
            arch_result = AgentResult(
                final_text=cached.get("raw_text", ""),
                prompt_tokens=int(cached.get("prompt_tokens", 0)),
                completion_tokens=int(cached.get("completion_tokens", 0)),
                wall_s=float(cached.get("wall_s", 0.0)),
            )
            objectives = cached.get("objectives", [])
            run.architect_result = arch_result.final_text
            console.print(f"\n[dim]· Architect[/] loaded from checkpoint "
                          f"({len(objectives)} objectives)")
            self._emit(run, "architect_resumed", objectives=len(objectives))
        else:
            console.print(f"\n[bold cyan]▶ Architect[/] — decomposing goal into micro-objectives"
                          f"  [dim]{_ts()}[/]")
            arch_backend = self._get_backend("architect")
            arch_cfg = self.config.role("architect")
            repo_summary = _survey_repo(repo_path)

            arch_result, objectives = run_architect(
                arch_backend, arch_cfg,
                goal=goal,
                task_type_prompt=task_cfg.architect_prompt,
                repo_summary=repo_summary,
                initial_context=initial_context,
            )

            run.architect_result = arch_result.final_text
            self._ck_save("architect", {
                "raw_text": arch_result.final_text,
                "objectives": objectives,
                "prompt_tokens": arch_result.prompt_tokens,
                "completion_tokens": arch_result.completion_tokens,
                "wall_s": arch_result.wall_s,
            })
            self._emit(run, "architect_done",
                       objectives=len(objectives),
                       wall_s=arch_result.wall_s,
                       tokens=arch_result.prompt_tokens + arch_result.completion_tokens)

            console.print(f"  → {len(objectives)} micro-objectives planned "
                          f"({arch_result.wall_s:.1f}s, "
                          f"{_fmt_tok(arch_result.prompt_tokens, arch_result.completion_tokens)})")
            for i, obj in enumerate(objectives):
                console.print(f"    {i+1}. [{obj['role']}] {obj['title']}")

        # --- BUILD SUBTASKS ---
        for i, obj in enumerate(objectives):
            run.subtasks.append(Subtask(
                index=i,
                title=obj["title"],
                role=obj["role"],
                scope=obj.get("scope", "."),
                expected_tools=obj.get("expected_tools", 3),
            ))

        # --- WORKERS ---
        cache = ToolCache()

        for sub in run.subtasks:
            if should_abort and should_abort():
                sub.status = Status.SKIPPED
                continue

            stage_name = f"worker_{sub.index}"
            cached_w = self._ck_load(stage_name)
            if cached_w is not None:
                # Restore subtask state from checkpoint; skip the worker call.
                restored = self._ck_subtask_from_dict(cached_w)
                sub.id = restored.id or sub.id
                sub.title = restored.title or sub.title
                sub.role = restored.role or sub.role
                sub.scope = restored.scope or sub.scope
                sub.expected_tools = restored.expected_tools or sub.expected_tools
                sub.status = restored.status
                sub.result_text = restored.result_text
                sub.escalated_from = restored.escalated_from
                sub.tool_calls = restored.tool_calls
                sub.metrics = restored.metrics
                console.print(f"\n[dim]· Worker {sub.index + 1}/{len(run.subtasks)}[/] "
                              f"[{sub.role}] loaded from checkpoint "
                              f"({len(sub.tool_calls)} tool calls, status={sub.status.value})")
                self._emit(run, "worker_resumed", index=sub.index,
                           status=sub.status.value)
                continue

            sub.status = Status.RUNNING
            worker_start_ts = _ts()
            console.print(f"\n[bold yellow]▶ Worker {sub.index + 1}/{len(run.subtasks)}[/] "
                         f"[{sub.role}] {sub.title}"
                         f"  [dim]{worker_start_ts}[/]")

            self._emit(run, "worker_begin", index=sub.index, role=sub.role, title=sub.title)

            worker_backend = self._get_backend(sub.role)
            role_cfg = self.config.role(sub.role)
            prior = _build_prior_findings(run, sub.index)

            def _on_tool(tc: ToolCall) -> None:
                if tc.duplicate:
                    console.print(f"    🔧 {tc.name} [yellow](dup)[/] — skipped")
                elif tc.cached:
                    console.print(f"    🔧 {tc.name} (cached) — {tc.bytes_out} bytes, {tc.wall_s:.2f}s")
                else:
                    console.print(f"    🔧 {tc.name} — {tc.bytes_out} bytes, {tc.wall_s:.2f}s")

            micro_telem: dict[str, Any] = {}
            use_microloop = (
                self.execution_mode == "microloop"
                and sub.role in {"worker_read", "worker_code", "worker_analyze"}
            )
            if use_microloop:
                worker_result, micro_telem = run_microloop(
                    backend_for=self._get_backend,
                    config=self.config,
                    role=sub.role,
                    task_prompt=f"Objective: {sub.title}\nScope: {sub.scope}",
                    objective_title=sub.title,
                    scope=sub.scope,
                    prior_findings=prior,
                    languages=languages,
                    extra_tool_defs=self.extra_tool_defs or None,
                    extra_tool_fns=self.extra_tool_fns or None,
                    cache=cache,
                    on_tool_event=_on_tool,
                    run_id=self.run_id,
                    subtask_idx=sub.index,
                )
            else:
                worker_result = run_worker(
                    worker_backend, role_cfg,
                    role=sub.role,
                    task_prompt=f"Objective: {sub.title}\nScope: {sub.scope}",
                    prior_findings=prior,
                    languages=languages,
                    extra_tool_defs=self.extra_tool_defs or None,
                    extra_tool_fns=self.extra_tool_fns or None,
                    cache=cache,
                    on_tool_event=_on_tool,
                )

            sub.result_text = worker_result.final_text
            sub.tool_calls = worker_result.tool_calls
            sub.metrics = StageMetrics(
                wall_s=worker_result.wall_s,
                prompt_tokens=worker_result.prompt_tokens,
                completion_tokens=worker_result.completion_tokens,
                tool_calls=worker_result.tool_calls_total,
                schema_rejects=worker_result.schema_rejects,
                peak_context_pressure=worker_result.peak_context_pressure,
                model=self.config.model_for_role(sub.role),
                cache_hits=cache.hits,
                cache_misses=cache.misses,
                microstep_count=int(micro_telem.get("microstep_count", 0)),
                microstep_rejects=int(micro_telem.get("microstep_rejects", 0)),
                blackboard_bytes=int(micro_telem.get("blackboard_bytes", 0)),
                decode_tok_per_s_avg=float(micro_telem.get("decode_tok_per_s_avg", 0.0)),
            )

            wtok = _fmt_tok(worker_result.prompt_tokens, worker_result.completion_tokens)
            wctx = f"ctx {worker_result.peak_context_pressure:.0%}"

            if worker_result.aborted or worker_result.schema_rejects > 3:
                sub.status = Status.BLOCKED
                escalated = self._try_escalate(sub, run, languages, cache)
                if not escalated:
                    reason = worker_result.abort_reason or "too many schema rejects"
                    console.print(f"  [red]✗ Blocked: {reason}[/] "
                                  f"({worker_result.wall_s:.1f}s) | "
                                  f"{wtok} | {wctx}"
                                  f"  [dim]{_ts()}[/]")
            else:
                sub.status = Status.DONE
                finding_lines = len(sub.result_text.splitlines())
                console.print(f"  [green]✓[/] {finding_lines} lines of findings "
                             f"({worker_result.wall_s:.1f}s, "
                             f"{worker_result.tool_calls_total} tool calls) | "
                             f"{wtok} | {wctx}"
                             f"  [dim]{_ts()}[/]")

            self._ck_save(stage_name, self._ck_subtask_to_dict(sub))
            self._emit(run, "worker_end",
                       index=sub.index, status=sub.status.value,
                       wall_s=worker_result.wall_s,
                       tool_calls=worker_result.tool_calls_total,
                       context_pressure=worker_result.peak_context_pressure)

        # --- SILENT-DIFF GATE ---
        # When a write-mode task type ran but no worker invoked a mutation
        # tool, the synthesizer would otherwise produce a plausible-looking
        # report claiming success despite zero edits. Surface that as an
        # explicit warning event so upstream callers (bench grader, PR cycle)
        # can detect the silent failure without grepping diffs.
        WRITE_MODE_TASKS = {"implement", "bugfix", "document", "manage"}
        MUTATION_TOOLS = {"write_file", "edit_file", "bash"}
        if task_type in WRITE_MODE_TASKS:
            wrote = any(
                tc.name in MUTATION_TOOLS and not tc.error and not tc.duplicate
                for sub in run.subtasks for tc in sub.tool_calls
            )
            if not wrote:
                self._emit(run, "pipeline_no_diff_warning",
                           task_type=task_type,
                           subtasks_total=len(run.subtasks),
                           subtasks_done=sum(1 for s in run.subtasks
                                              if s.status == Status.DONE))
                console.print(
                    f"  [yellow]⚠ pipeline_no_diff_warning[/] — "
                    f"task_type={task_type} but no worker invoked "
                    f"write_file/edit_file/bash. Synthesizer report below "
                    f"will not reflect actual file changes."
                )

        # --- VALIDATOR ---
        if should_abort and should_abort():
            run.status = Status.BLOCKED
            run.total_wall_s = time.monotonic() - t0
            return run

        worker_findings = self._collect_worker_findings(run)
        val_result = None
        envelope = ValidatorEnvelope(status="cleared", summary="No worker findings.")

        cached_v = self._ck_load("validator")
        if cached_v is not None:
            val_result = AgentResult(
                final_text=cached_v.get("raw_text", ""),
                prompt_tokens=int(cached_v.get("prompt_tokens", 0)),
                completion_tokens=int(cached_v.get("completion_tokens", 0)),
                wall_s=float(cached_v.get("wall_s", 0.0)),
            )
            envelope = _envelope_from_dict(cached_v.get("envelope", {}))
            run.validator_result = val_result.final_text
            run.validator_envelope = envelope
            console.print(f"\n[dim]· Validator[/] loaded from checkpoint "
                          f"(status={envelope.status}, "
                          f"{len(envelope.verified)} verified, {len(envelope.removed)} removed)")
            self._emit(run, "validator_resumed",
                       status=envelope.status,
                       verified_count=len(envelope.verified))
        elif worker_findings.strip():
            console.print(f"\n[bold magenta]▶ Validator[/] — verifying citations"
                          f"  [dim]{_ts()}[/]")
            val_backend = self._get_backend("validator")
            val_cfg = self.config.role("validator")

            val_result, envelope = run_validator(
                val_backend, val_cfg,
                worker_findings=worker_findings,
                cache=cache,
                on_tool_event=lambda tc: console.print(
                    f"    🔍 {tc.name} — {tc.bytes_out} bytes, {tc.wall_s:.2f}s"
                ),
            )

            run.validator_result = val_result.final_text
            run.validator_envelope = envelope
            self._ck_save("validator", {
                "raw_text": val_result.final_text,
                "envelope": _envelope_to_dict(envelope),
                "prompt_tokens": val_result.prompt_tokens,
                "completion_tokens": val_result.completion_tokens,
                "wall_s": val_result.wall_s,
            })
            self._emit(run, "validator_done",
                       wall_s=val_result.wall_s,
                       tool_calls=val_result.tool_calls_total,
                       status=envelope.status,
                       verified_count=len(envelope.verified),
                       removed_count=len(envelope.removed))
            if envelope.is_ambiguous:
                self._emit(run, "validator_ambiguous_warning",
                           verified_count=len(envelope.verified),
                           removed_count=len(envelope.removed),
                           summary=envelope.summary)
                console.print(
                    f"  [yellow]! Validator status ambiguous[/] — "
                    f"{len(envelope.verified)} verified, {len(envelope.removed)} removed"
                )
            console.print(f"  [green]✓[/] Validation complete "
                          f"({val_result.wall_s:.1f}s, "
                          f"{_fmt_tok(val_result.prompt_tokens, val_result.completion_tokens)}) "
                          f"— status: {envelope.status}")
        else:
            run.validator_result = "(no findings to validate)"
            envelope = ValidatorEnvelope(status="cleared", summary="No worker findings.")
            run.validator_envelope = envelope

        # --- SYNTHESIZER ---
        if should_abort and should_abort():
            run.status = Status.BLOCKED
            run.total_wall_s = time.monotonic() - t0
            return run

        cached_s = self._ck_load("synthesizer")
        if cached_s is not None:
            synth_result = AgentResult(
                final_text=cached_s.get("final_report", ""),
                prompt_tokens=int(cached_s.get("prompt_tokens", 0)),
                completion_tokens=int(cached_s.get("completion_tokens", 0)),
                wall_s=float(cached_s.get("wall_s", 0.0)),
            )
            run.synthesizer_result = synth_result.final_text
            run.final_report = synth_result.final_text
            console.print(f"\n[dim]· Synthesizer[/] loaded from checkpoint "
                          f"({len(synth_result.final_text)} chars)")
            self._emit(run, "synthesizer_resumed",
                       chars=len(synth_result.final_text))
        else:
            console.print(f"\n[bold blue]▶ Synthesizer[/] — assembling final report"
                          f"  [dim]{_ts()}[/]")
            synth_backend = self._get_backend("synthesizer")
            synth_cfg = self.config.role("synthesizer")

            synth_result = run_synthesizer(
                synth_backend, synth_cfg,
                envelope=envelope,
                task_type=task_type,
                goal=goal,
            )

            run.synthesizer_result = synth_result.final_text
            run.final_report = synth_result.final_text
            self._ck_save("synthesizer", {
                "final_report": synth_result.final_text,
                "prompt_tokens": synth_result.prompt_tokens,
                "completion_tokens": synth_result.completion_tokens,
                "wall_s": synth_result.wall_s,
            })
            self._emit(run, "synthesizer_done",
                       wall_s=synth_result.wall_s,
                       tokens=synth_result.prompt_tokens + synth_result.completion_tokens)
            console.print(f"  [green]✓[/] Report assembled "
                          f"({synth_result.wall_s:.1f}s, "
                          f"{_fmt_tok(synth_result.prompt_tokens, synth_result.completion_tokens)})")

        # --- DONE ---
        run.status = Status.DONE
        run.total_wall_s = time.monotonic() - t0

        self._emit(run, "finish", total_wall_s=run.total_wall_s)

        total_prompt = sum(s.metrics.prompt_tokens for s in run.subtasks)
        total_prompt += arch_result.prompt_tokens + synth_result.prompt_tokens
        total_comp = sum(s.metrics.completion_tokens for s in run.subtasks)
        total_comp += arch_result.completion_tokens + synth_result.completion_tokens
        if val_result is not None:
            total_prompt += val_result.prompt_tokens
            total_comp += val_result.completion_tokens
        total_tok = total_prompt + total_comp

        n_done = sum(1 for s in run.subtasks if s.status == Status.DONE)
        n_blocked = sum(1 for s in run.subtasks if s.status == Status.BLOCKED)
        n_total = len(run.subtasks)
        total_tools = sum(s.metrics.tool_calls for s in run.subtasks)
        peak_ctx = max((s.metrics.peak_context_pressure for s in run.subtasks), default=0.0)
        total_hits = sum(s.metrics.cache_hits for s in run.subtasks)
        total_misses = sum(s.metrics.cache_misses for s in run.subtasks)
        cache_rate = total_hits / (total_hits + total_misses) if (total_hits + total_misses) else 0.0

        console.print(f"\n[bold green]✓ Pipeline complete[/] — {run.total_wall_s:.1f}s total"
                      f"  [dim]{pipeline_start_ts}→{_ts()}[/]")
        console.print(f"    Tokens: {total_tok:,} ({total_prompt:,} prompt + {total_comp:,} completion)")
        console.print(f"    Workers: {n_done}/{n_total} done, {n_blocked} blocked | "
                      f"Tools: {total_tools} | Cache: {cache_rate:.0%} | Peak ctx: {peak_ctx:.2f}")

        return run

    def _try_escalate(
        self,
        sub: Subtask,
        run: PipelineRun,
        languages: frozenset[str],
        cache: ToolCache,
    ) -> bool:
        """Try to escalate a failed subtask to a more capable role."""
        escalation_map = {
            self.config.escalation.worker_read: "worker_read",
            self.config.escalation.worker_analyze: "worker_analyze",
        }
        next_role = getattr(self.config.escalation, sub.role, None)
        if not next_role or next_role not in self.config.roles:
            return False

        console.print(f"  [yellow]↑ Escalating from {sub.role} → {next_role}[/]")
        self._emit(run, "escalate", from_role=sub.role, to_role=next_role, index=sub.index)

        worker_backend = self._get_backend(next_role)
        role_cfg = self.config.role(next_role)
        prior = _build_prior_findings(run, sub.index)

        result = run_worker(
            worker_backend, role_cfg,
            role=next_role,
            task_prompt=f"Objective: {sub.title}\nScope: {sub.scope}",
            prior_findings=prior,
            languages=languages,
            extra_tool_defs=self.extra_tool_defs or None,
            extra_tool_fns=self.extra_tool_fns or None,
            cache=cache,
        )

        if not result.aborted:
            sub.result_text = result.final_text
            sub.status = Status.DONE
            sub.escalated_from = sub.role
            sub.role = next_role
            sub.metrics.wall_s += result.wall_s
            sub.metrics.prompt_tokens += result.prompt_tokens
            sub.metrics.completion_tokens += result.completion_tokens
            sub.metrics.tool_calls += result.tool_calls_total
            console.print(f"  [green]✓ Escalation succeeded[/]")
            return True

        return False

    def _collect_worker_findings(self, run: PipelineRun) -> str:
        """Concatenate all worker findings for the validator."""
        parts: list[str] = []
        for sub in run.subtasks:
            if sub.status == Status.DONE and sub.result_text:
                parts.append(
                    f"## Subtask {sub.index + 1}: {sub.title} ({sub.role})\n\n"
                    f"{sub.result_text}"
                )
        return "\n\n---\n\n".join(parts)
