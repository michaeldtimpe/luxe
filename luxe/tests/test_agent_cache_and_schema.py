"""Integration-ish test: end-to-end exercise of the new cache + schema
hooks inside run_agent, driven by a scripted fake backend.

Covers:
- Schema-invalid tool call is counted on AgentResult.schema_rejects and
  the fn is NEVER invoked.
- Cache hit on a repeat read_file-shaped call returns the prior result
  without hitting the underlying fn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.backends import (
    Backend,
    GenerationTiming,
    Response,
    ToolCall,
    ToolDef,
)

from luxe_cli.agents.base import run_agent
from luxe_cli.registry import AgentConfig
from luxe_cli.tasks.cache import ToolCache, wrap_tool_fns


@dataclass
class _ScriptedBackend(Backend):
    """Returns the next prepared Response on each chat() call. Used so
    tests can drive the agent loop through a deterministic sequence."""
    responses: list[Response] = field(default_factory=list)
    idx: int = 0

    def __init__(self, responses: list[Response]) -> None:
        super().__init__(kind="mlx", base_url="mock://local", model_id="mock")
        self.responses = responses
        self.idx = 0

    def chat(self, messages, *, tools=None, max_tokens=2048,
             temperature=0.2, stream=False, extra_body=None):
        r = self.responses[self.idx]
        self.idx += 1
        return r


def _cfg() -> AgentConfig:
    # Use an arbitrary allowed specialist name; the loop doesn't branch
    # on it for schema/cache behavior.
    return AgentConfig(
        name="review",
        display="review",
        model="mock",
        system_prompt="sys",
        tools=[],
        max_steps=3,
        max_wall_s=30.0,
        max_tokens_per_turn=256,
        temperature=0.1,
        max_tool_calls_per_turn=3,
        min_tool_calls=0,
    )


def _done_response(text: str = "done") -> Response:
    return Response(
        text=text,
        tool_calls=[],
        finish_reason="stop",
        timing=GenerationTiming(
            prompt_tokens=10, completion_tokens=5, total_s=0.01
        ),
    )


def _call_response(name: str, args: dict[str, Any]) -> Response:
    import json
    return Response(
        text="",
        tool_calls=[ToolCall(
            id="c1", name=name, arguments=args,
            raw_arguments=json.dumps(args),
        )],
        finish_reason="tool_calls",
        timing=GenerationTiming(
            prompt_tokens=10, completion_tokens=5, total_s=0.01
        ),
    )


def test_schema_reject_counted_and_fn_not_called():
    """Model emits `grep` without `pattern`. Validation should reject
    the call before the fn runs; the agent then completes normally on
    the next turn."""
    called = {"n": 0}

    def grep(args):
        called["n"] += 1
        return ("result", None)

    tool_defs = [ToolDef(
        name="grep",
        description="rg",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["pattern"],
        },
    )]
    backend = _ScriptedBackend([
        _call_response("grep", {"glob": "*.py"}),  # missing `pattern`
        _done_response("ok"),
    ])
    result = run_agent(
        backend, _cfg(),
        task="look for eval calls",
        tool_defs=tool_defs,
        tool_fns={"grep": grep},
    )
    assert result.schema_rejects == 1
    assert called["n"] == 0  # fn never ran — schema caught it first


def test_cache_hit_skips_inner_fn_on_repeat_call():
    """Two subsequent identical `read_file` calls. With a ToolCache
    wrapping the fn, the second must return the cached result without
    invoking the underlying fn."""
    reads = {"n": 0}

    def read_file(args):
        reads["n"] += 1
        return (f"content of {args['path']}", None)

    tool_defs = [ToolDef(
        name="read_file",
        description="read",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )]
    cache = ToolCache()
    tool_fns = wrap_tool_fns(
        {"read_file": read_file}, cache, cacheable={"read_file"}
    )
    backend = _ScriptedBackend([
        _call_response("read_file", {"path": "a.py"}),
        _call_response("read_file", {"path": "a.py"}),
        _done_response("summary"),
    ])
    result = run_agent(
        backend, _cfg(),
        task="read a.py twice",
        tool_defs=tool_defs,
        tool_fns=tool_fns,
    )
    assert reads["n"] == 1  # second call served from cache
    assert cache.hits == 1
    assert cache.misses == 1
    assert result.tool_calls_total == 2  # both visible to the model
