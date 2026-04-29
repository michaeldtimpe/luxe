"""Metrics collector — gathers and persists pipeline run metrics."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from luxe.pipeline.model import PipelineRun, StageMetrics


@dataclass
class RunMetrics:
    run_id: str = ""
    goal: str = ""
    task_type: str = ""
    repo_path: str = ""
    total_wall_s: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tool_calls: int = 0
    total_schema_rejects: int = 0
    subtask_count: int = 0
    subtasks_done: int = 0
    subtasks_blocked: int = 0
    escalations: int = 0
    peak_context_pressure: float = 0.0
    cache_hit_rate: float = 0.0
    # Microloop-only aggregates (zero for swarm runs).
    total_microstep_count: int = 0
    total_microstep_rejects: int = 0
    total_blackboard_bytes: int = 0
    decode_tok_per_s_avg: float = 0.0
    per_role: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


def collect(run: PipelineRun) -> RunMetrics:
    """Extract metrics from a completed pipeline run."""
    m = RunMetrics(
        run_id=run.id,
        goal=run.goal,
        task_type=run.task_type,
        repo_path=run.repo_path,
        total_wall_s=run.total_wall_s,
        subtask_count=len(run.subtasks),
        events=run.events,
    )

    total_cache_hits = 0
    total_cache_misses = 0

    micro_decode_rates: list[tuple[float, int]] = []  # (rate, microstep_count) for weighted avg

    for sub in run.subtasks:
        m.total_prompt_tokens += sub.metrics.prompt_tokens
        m.total_completion_tokens += sub.metrics.completion_tokens
        m.total_tool_calls += sub.metrics.tool_calls
        m.total_schema_rejects += sub.metrics.schema_rejects
        m.peak_context_pressure = max(m.peak_context_pressure, sub.metrics.peak_context_pressure)
        total_cache_hits += sub.metrics.cache_hits
        total_cache_misses += sub.metrics.cache_misses
        m.total_microstep_count += sub.metrics.microstep_count
        m.total_microstep_rejects += sub.metrics.microstep_rejects
        m.total_blackboard_bytes += sub.metrics.blackboard_bytes
        if sub.metrics.microstep_count > 0 and sub.metrics.decode_tok_per_s_avg > 0:
            micro_decode_rates.append((sub.metrics.decode_tok_per_s_avg, sub.metrics.microstep_count))

        if sub.status.value == "done":
            m.subtasks_done += 1
        elif sub.status.value == "blocked":
            m.subtasks_blocked += 1
        if sub.escalated_from:
            m.escalations += 1

    if micro_decode_rates:
        total_w = sum(w for _, w in micro_decode_rates)
        m.decode_tok_per_s_avg = sum(rate * w for rate, w in micro_decode_rates) / total_w

    total_cache = total_cache_hits + total_cache_misses
    m.cache_hit_rate = total_cache_hits / total_cache if total_cache > 0 else 0.0

    for role_name, agg in run.stage_summary.items():
        m.per_role[role_name] = {
            "wall_s": agg.wall_s,
            "prompt_tokens": agg.prompt_tokens,
            "completion_tokens": agg.completion_tokens,
            "tool_calls": agg.tool_calls,
            "decode_tok_per_s": agg.decode_tok_per_s,
            "peak_context_pressure": agg.peak_context_pressure,
            "model": agg.model,
        }

    return m


def save_metrics(metrics: RunMetrics, output_dir: str | Path) -> Path:
    """Persist metrics as JSON."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"run_{metrics.run_id}.json"
    path.write_text(json.dumps(asdict(metrics), indent=2, default=str))
    return path
