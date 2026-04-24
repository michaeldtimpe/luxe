"""Metrics aggregator for tool-use, throughput, and memory.

Designed to be fed one event per model turn inside an agent loop. Produces
a per-run summary that slots into the Phase A / Phase D comparison tables.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

from harness.backends import GenerationTiming, Response, ToolCall


@dataclass
class TurnRecord:
    step: int
    response: Response
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    had_recoverable_error: bool = False


@dataclass
class RunMetrics:
    candidate_id: str
    config_id: str
    benchmark: str
    task_id: str

    turns: list[TurnRecord] = field(default_factory=list)
    peak_rss_bytes: int = 0
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    known_tool_names: set[str] = field(default_factory=set)

    # Compression-benchmark extensions. Zero/empty when the benchmark
    # doesn't run retrieval or compression, so reports can safely aggregate.
    t_retrieval_s: float = 0.0
    t_compression_s: float = 0.0
    peak_context_tokens: int = 0
    file_precision: float = 0.0
    file_recall: float = 0.0

    def record_turn(
        self,
        step: int,
        response: Response,
        tool_results: list[dict[str, Any]] | None = None,
        had_recoverable_error: bool = False,
    ) -> None:
        self.turns.append(
            TurnRecord(
                step=step,
                response=response,
                tool_results=tool_results or [],
                had_recoverable_error=had_recoverable_error,
            )
        )

    def finish(self) -> None:
        self.ended_at = time.time()

    @property
    def wall_s(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    @property
    def total_steps(self) -> int:
        return len(self.turns)

    def tool_call_stats(self) -> dict[str, float | int]:
        total_calls = 0
        successful = 0
        drift = 0
        parse_errors = 0
        recovered_after_error = 0
        saw_error_last = False

        for turn in self.turns:
            calls: list[ToolCall] = turn.response.tool_calls or []
            for call in calls:
                total_calls += 1
                if call.arguments.get("__parse_error__"):
                    parse_errors += 1
                    continue
                if self.known_tool_names and call.name not in self.known_tool_names:
                    drift += 1
                    continue
                successful += 1

            # Recovery: did the step after an error turn produce a clean call + progress?
            if saw_error_last and not turn.had_recoverable_error and calls:
                recovered_after_error += 1
                saw_error_last = False
            if turn.had_recoverable_error:
                saw_error_last = True

        def rate(n: int, d: int) -> float:
            return (n / d * 100) if d else 0.0

        return {
            "total_calls": total_calls,
            "successful": successful,
            "drift": drift,
            "parse_errors": parse_errors,
            "success_rate_pct": rate(successful, total_calls),
            "drift_rate_pct": rate(drift, total_calls),
            "parse_error_rate_pct": rate(parse_errors, total_calls),
            "recovered_after_error": recovered_after_error,
        }

    def throughput_stats(self) -> dict[str, float]:
        timings: list[GenerationTiming] = [t.response.timing for t in self.turns]
        if not timings:
            return {"ttft_s": 0.0, "decode_tok_s": 0.0, "prompt_tokens_total": 0, "completion_tokens_total": 0}
        return {
            "ttft_s": mean(t.time_to_first_token_s for t in timings if t.time_to_first_token_s > 0) if any(t.time_to_first_token_s > 0 for t in timings) else 0.0,
            "decode_tok_s": mean(t.decode_tok_per_s for t in timings if t.completion_tokens > 0) if any(t.completion_tokens > 0 for t in timings) else 0.0,
            "prompt_tokens_total": sum(t.prompt_tokens for t in timings),
            "completion_tokens_total": sum(t.completion_tokens for t in timings),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "config_id": self.config_id,
            "benchmark": self.benchmark,
            "task_id": self.task_id,
            "wall_s": self.wall_s,
            "total_steps": self.total_steps,
            "peak_rss_bytes": self.peak_rss_bytes,
            "tool_call": self.tool_call_stats(),
            "throughput": self.throughput_stats(),
            "compression": {
                "t_retrieval_s": self.t_retrieval_s,
                "t_compression_s": self.t_compression_s,
                "peak_context_tokens": self.peak_context_tokens,
                "file_precision": self.file_precision,
                "file_recall": self.file_recall,
            },
        }


class RssSampler:
    """Background thread that records peak RSS of a process tree.

    Usage:
        sampler = RssSampler(pid=server_pid, interval_s=2.0)
        sampler.start()
        ...
        sampler.stop()
        print(sampler.peak_rss_bytes)
    """

    def __init__(self, pid: int, interval_s: float = 2.0) -> None:
        self.pid = pid
        self.interval_s = interval_s
        self.peak_rss_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        from harness.server import sample_peak_rss

        while not self._stop.is_set():
            rss = sample_peak_rss(self.pid)
            if rss > self.peak_rss_bytes:
                self.peak_rss_bytes = rss
            self._stop.wait(self.interval_s)
