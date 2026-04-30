"""Core tool types — mirrors luxe's ToolDef/ToolCall pattern."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

ToolFn = Callable[[dict[str, Any]], tuple[str, str | None]]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    result: str = ""
    error: str | None = None
    wall_s: float = 0.0
    bytes_out: int = 0
    cached: bool = False
    duplicate: bool = False


@dataclass
class ToolResult:
    content: str
    error: str | None = None


@dataclass
class ToolCache:
    """Per-task memoization for read-only tools."""
    _store: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def _key(self, name: str, args: dict[str, Any]) -> str:
        import json
        return f"{name}:{json.dumps(args, sort_keys=True)}"

    def get_or_run(
        self,
        name: str,
        args: dict[str, Any],
        fn: ToolFn,
    ) -> tuple[str, str | None, bool]:
        key = self._key(name, args)
        if key in self._store:
            self.hits += 1
            result, err = self._store[key]
            return result, err, True
        self.misses += 1
        result, err = fn(args)
        self._store[key] = (result, err)
        return result, err, False


def dispatch_tool(
    name: str,
    args: dict[str, Any],
    tool_fns: dict[str, ToolFn],
    cache: ToolCache | None = None,
    cacheable: set[str] | None = None,
) -> ToolCall:
    tc = ToolCall(id="", name=name, arguments=args)
    if name not in tool_fns:
        tc.error = f"Unknown tool: {name}"
        return tc

    fn = tool_fns[name]
    t0 = time.monotonic()

    # Capture exceptions so the agent loop can surface them to the model
    # as retry-able errors instead of crashing luxe. A tool that raises
    # (e.g. fs._safe rejecting an absolute path) used to escape run_agent
    # entirely; now the model sees a normal tool-error message and can
    # self-correct on the next turn.
    try:
        if cache and cacheable and name in cacheable:
            result, err, was_cached = cache.get_or_run(name, args, fn)
            tc.cached = was_cached
        else:
            result, err = fn(args)
    except Exception as e:
        result, err = "", f"{type(e).__name__}: {e}"

    tc.wall_s = time.monotonic() - t0
    tc.result = result
    tc.error = err
    tc.bytes_out = len(result.encode("utf-8", errors="replace"))
    return tc


def validate_args(defn: ToolDef, args: dict[str, Any]) -> str | None:
    """Light JSON schema validation — required fields + primitive types."""
    params = defn.parameters
    required = params.get("required", [])
    props = params.get("properties", {})

    for r in required:
        if r not in args:
            return f"Missing required argument: {r}"

    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
    for k, v in args.items():
        if k in props:
            expected = props[k].get("type")
            if expected and expected in type_map:
                if not isinstance(v, type_map[expected]):
                    return f"Argument '{k}' should be {expected}, got {type(v).__name__}"
    return None
