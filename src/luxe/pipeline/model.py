"""Data models for pipeline tasks, subtasks, and results."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from luxe.tools.base import ToolCall


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass
class StageMetrics:
    wall_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    schema_rejects: int = 0
    peak_context_pressure: float = 0.0
    model: str = ""
    model_swap_s: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0

    @property
    def decode_tok_per_s(self) -> float:
        if self.wall_s <= 0 or self.completion_tokens <= 0:
            return 0.0
        return self.completion_tokens / self.wall_s


@dataclass
class Subtask:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    index: int = 0
    title: str = ""
    role: str = ""
    scope: str = "."
    expected_tools: int = 3
    status: Status = Status.PENDING
    result_text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    metrics: StageMetrics = field(default_factory=StageMetrics)
    escalated_from: str | None = None


@dataclass
class PipelineRun:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    goal: str = ""
    task_type: str = ""
    repo_path: str = ""
    status: Status = Status.PENDING
    subtasks: list[Subtask] = field(default_factory=list)
    architect_result: str = ""
    validator_result: str = ""
    synthesizer_result: str = ""
    final_report: str = ""
    total_wall_s: float = 0.0
    started_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)

    def add_event(self, kind: str, **data: Any) -> None:
        self.events.append({
            "kind": kind,
            "ts": time.time(),
            "run_id": self.id,
            **data,
        })

    @property
    def stage_summary(self) -> dict[str, StageMetrics]:
        """Aggregate metrics by role."""
        by_role: dict[str, StageMetrics] = {}
        for sub in self.subtasks:
            if sub.role not in by_role:
                by_role[sub.role] = StageMetrics(model=sub.metrics.model)
            agg = by_role[sub.role]
            agg.wall_s += sub.metrics.wall_s
            agg.prompt_tokens += sub.metrics.prompt_tokens
            agg.completion_tokens += sub.metrics.completion_tokens
            agg.tool_calls += sub.metrics.tool_calls
            agg.schema_rejects += sub.metrics.schema_rejects
            agg.cache_hits += sub.metrics.cache_hits
            agg.cache_misses += sub.metrics.cache_misses
            agg.peak_context_pressure = max(
                agg.peak_context_pressure, sub.metrics.peak_context_pressure
            )
        return by_role
