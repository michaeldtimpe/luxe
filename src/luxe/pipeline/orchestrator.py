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
from luxe.agents.synthesizer import run_synthesizer
from luxe.agents.validator import ValidatorEnvelope, run_validator
from luxe.agents.worker import run_worker
from luxe.backend import Backend
from luxe.config import PipelineConfig
from luxe.pipeline.model import PipelineRun, StageMetrics, Status, Subtask
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
    """Quick repo survey: languages, LOC, file count."""
    p = Path(repo_path)
    if not p.is_dir():
        return f"Repo path not found: {repo_path}"

    extensions: dict[str, int] = {}
    file_count = 0
    total_lines = 0

    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}]
        for f in files:
            fp = Path(root) / f
            ext = fp.suffix.lower()
            if ext in {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".rb", ".c", ".cpp", ".h"}:
                file_count += 1
                extensions[ext] = extensions.get(ext, 0) + 1
                try:
                    total_lines += sum(1 for _ in fp.open(errors="replace"))
                except OSError:
                    pass

    lang_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "typescript", ".jsx": "javascript", ".rs": "rust",
        ".go": "go", ".java": "java", ".rb": "ruby",
        ".c": "c", ".cpp": "cpp", ".h": "c",
    }
    languages = set()
    for ext in extensions:
        if ext in lang_map:
            languages.add(lang_map[ext])

    ext_summary = ", ".join(f"{ext}: {n}" for ext, n in sorted(extensions.items(), key=lambda x: -x[1]))

    return (
        f"Files: {file_count} | LOC: {total_lines:,} | "
        f"Languages: {', '.join(sorted(languages))} | "
        f"Extensions: {ext_summary}"
    )


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


class PipelineOrchestrator:
    def __init__(
        self,
        config: PipelineConfig,
        on_event: OnEvent | None = None,
    ):
        self.config = config
        self.on_event = on_event
        self._backends: dict[str, Backend] = {}

    def _get_backend(self, role_name: str) -> Backend:
        model = self.config.model_for_role(role_name)
        if model not in self._backends:
            self._backends[model] = Backend(
                base_url=self.config.omlx_base_url,
                model=model,
            )
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

        console.print(f"\n[bold cyan]▶ Architect[/] — decomposing goal into micro-objectives"
                      f"  [dim]{_ts()}[/]")
        arch_backend = self._get_backend("architect")
        arch_cfg = self.config.role("architect")
        repo_summary = _survey_repo(repo_path)

        swap_t0 = time.monotonic()
        arch_result, objectives = run_architect(
            arch_backend, arch_cfg,
            goal=goal,
            task_type_prompt=task_cfg.architect_prompt,
            repo_summary=repo_summary,
            initial_context=initial_context,
        )
        swap_wall = time.monotonic() - swap_t0

        run.architect_result = arch_result.final_text
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

            worker_result = run_worker(
                worker_backend, role_cfg,
                role=sub.role,
                task_prompt=f"Objective: {sub.title}\nScope: {sub.scope}",
                prior_findings=prior,
                languages=languages,
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

            self._emit(run, "worker_end",
                       index=sub.index, status=sub.status.value,
                       wall_s=worker_result.wall_s,
                       tool_calls=worker_result.tool_calls_total,
                       context_pressure=worker_result.peak_context_pressure)

        # --- VALIDATOR ---
        if should_abort and should_abort():
            run.status = Status.BLOCKED
            run.total_wall_s = time.monotonic() - t0
            return run

        worker_findings = self._collect_worker_findings(run)
        val_result = None
        envelope = ValidatorEnvelope(status="cleared", summary="No worker findings.")
        if worker_findings.strip():
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
