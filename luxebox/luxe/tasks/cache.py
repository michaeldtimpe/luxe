"""Task-scoped memoization for deterministic read-only tools.

One ToolCache lives for a single Task (driven by the Orchestrator).
Read-only fs / git / static-analysis tools with identical arguments
return the same result during a task run, so repeating them across
subtasks wastes both wall time and context tokens. The cache collapses
those repeats into a single disk/subprocess hit; the second and later
calls return in ~microseconds and the model still receives the same
tool_result content it would have seen.

Scope is deliberately per-Task, not process-global: file mtimes can
change between runs, and the background subprocess model already tears
down state at task boundaries — we don't need to reason about
invalidation across tasks.

Mutation tools (write_file / edit_file / bash) are NEVER cached. The
wrapping layer gates membership in the `cacheable` set, so the cache
class itself doesn't need to know which tools are safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

ToolFn = Callable[[dict[str, Any]], tuple[Any, str | None]]


def _hash_args(args: dict[str, Any]) -> str:
    """Stable serialization of a tool's arguments for cache keying.
    `sort_keys=True` so argument ordering doesn't split otherwise-
    identical calls. `default=str` so odd values (Path objects, etc.)
    don't crash the hasher — they serialize deterministically even if
    the repr-level text differs from what a literal json.dumps would
    emit."""
    return json.dumps(args, sort_keys=True, default=str)


@dataclass
class ToolCache:
    """In-memory `(tool_name, args_hash) → (result, err)` map.

    Hits/misses are tracked so the orchestrator can surface cache
    effectiveness in its log events and the benchmark harness can
    measure the dedup payoff across commits."""

    entries: dict[tuple[str, str], tuple[Any, str | None]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get_or_run(
        self, name: str, args: dict[str, Any], fn: ToolFn
    ) -> tuple[Any, str | None, bool]:
        """Return `(result, err, cached)`. `cached=True` when the answer
        came from a prior call with identical arguments.

        Errors are cached too — a malformed call won't magically succeed
        on retry with the same args, so re-running it is pure waste."""
        key = (name, _hash_args(args))
        if key in self.entries:
            self.hits += 1
            result, err = self.entries[key]
            return result, err, True
        self.misses += 1
        result, err = fn(args)
        self.entries[key] = (result, err)
        return result, err, False


def wrap_tool_fns(
    fns: dict[str, ToolFn],
    cache: ToolCache,
    cacheable: set[str],
) -> dict[str, ToolFn]:
    """Return a new fn dict where names in `cacheable` route through
    the cache. Names outside the set keep their original fn so mutation
    tools (write_file / edit_file / bash) never get memoized.

    The wrapped fn matches the normal `(result, err)` contract so
    callers don't need to care whether they got a hit or miss — the
    orchestrator reads hits/misses off the ToolCache directly."""
    out: dict[str, ToolFn] = {}
    for name, fn in fns.items():
        if name in cacheable:
            def _wrapped(
                args: dict[str, Any],
                _name: str = name,
                _fn: ToolFn = fn,
            ) -> tuple[Any, str | None]:
                result, err, _ = cache.get_or_run(_name, args, _fn)
                return result, err
            out[name] = _wrapped
        else:
            out[name] = fn
    return out
