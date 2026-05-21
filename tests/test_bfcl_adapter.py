"""Tests for benchmarks/bfcl/adapter.py — problem loading + dispatch shape.

Validates message construction and tool-spec extraction against the
vendored BFCL v4 data layout. Backend interaction is mocked.

Data is loaded from `~/.luxe/bfcl-data/` (or `LUXE_BFCL_DATA_DIR`).
Populate via `scripts/fetch_bfcl_data.sh`.
"""

from __future__ import annotations

from typing import Any

import pytest

from benchmarks.bfcl.adapter import (
    SUPPORTED_CATEGORIES,
    _bfcl_data_dir,
    _problem_messages,
    _problem_tools,
    load_ground_truth,
    load_problems,
    run_problem_raw,
)
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse

try:
    _DATA_DIR = _bfcl_data_dir()
    _HAS_DATA = _DATA_DIR.is_dir()
except FileNotFoundError:
    _HAS_DATA = False

pytestmark = pytest.mark.skipif(
    not _HAS_DATA,
    reason="BFCL v4 data not vendored. Run scripts/fetch_bfcl_data.sh.",
)


def test_load_problems_each_category():
    """All five supported categories load without error."""
    for cat in SUPPORTED_CATEGORIES:
        problems = load_problems(cat, limit=2)
        assert len(problems) >= 1, f"{cat} returned 0 problems"
        first = problems[0]
        assert "id" in first
        assert "question" in first
        assert "function" in first


def test_load_ground_truth_simple_python():
    gt = load_ground_truth("simple_python")
    assert len(gt) > 0
    # Each entry maps id → list of GT call options
    sample_id = next(iter(gt))
    assert isinstance(gt[sample_id], list)


def test_load_ground_truth_irrelevance_returns_empty():
    """Irrelevance has no positive ground truth; the grader expects no
    tool calls. The loader should return an empty dict cleanly."""
    assert load_ground_truth("irrelevance") == {}


def test_problem_messages_unwraps_nested_question():
    """BFCL wraps question in [[{...}]] — we should flatten to single-turn."""
    problem = {
        "id": "test_0",
        "question": [[{"role": "user", "content": "hello"}]],
        "function": [{"name": "f", "parameters": {}}],
    }
    msgs = _problem_messages(problem)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello"


def test_problem_messages_handles_missing_question():
    problem = {"id": "x", "function": []}
    msgs = _problem_messages(problem)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_problem_tools_extracts_all():
    problem = {
        "function": [
            {"name": "a", "description": "a", "parameters": {"type": "object"}},
            {"name": "b", "description": "b", "parameters": {"type": "object"}},
        ]
    }
    tools = _problem_tools(problem)
    assert len(tools) == 2
    assert {t.name for t in tools} == {"a", "b"}


def test_problem_tools_handles_dict_function_field():
    """Some BFCL entries put a single function dict (not list) — tolerate."""
    problem = {"function": {"name": "f", "parameters": {}}}
    tools = _problem_tools(problem)
    assert len(tools) == 1
    assert tools[0].name == "f"


class _MockBackend:
    """Minimal backend that returns scripted ChatResponses."""

    def __init__(self, response: ChatResponse) -> None:
        self._response = response
        self.last_messages: list[dict[str, Any]] | None = None
        self.last_tools: list[dict[str, Any]] | None = None

    def chat(self, messages, tools=None, **kwargs) -> ChatResponse:
        self.last_messages = list(messages)
        self.last_tools = tools
        return self._response


def test_run_problem_raw_captures_tool_calls():
    """Backend returns one tool call → adapter surfaces (name, args)."""
    problem = load_problems("simple_python", limit=1)[0]
    fake_resp = ChatResponse(
        text="",
        tool_calls=[ToolCallResponse(id="x", name="calculate_triangle_area",
                                     arguments={"base": 10, "height": 5})],
        finish_reason="tool_calls",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=20),
    )
    backend = _MockBackend(fake_resp)
    result = run_problem_raw(backend, problem)
    assert result.problem_id == problem["id"]
    assert len(result.actual_calls) == 1
    name, args = result.actual_calls[0]
    assert name == "calculate_triangle_area"
    assert args == {"base": 10, "height": 5}
    # Backend was called with the tool spec
    assert backend.last_tools is not None
    assert len(backend.last_tools) >= 1


def test_run_problem_raw_handles_no_tool_calls():
    """Model returns prose only — adapter records empty actual_calls."""
    problem = load_problems("simple_python", limit=1)[0]
    fake_resp = ChatResponse(
        text="I'm not sure what to do here.",
        tool_calls=[],
        finish_reason="stop",
        timing=GenerationTiming(prompt_tokens=100, completion_tokens=10),
    )
    result = run_problem_raw(_MockBackend(fake_resp), problem)
    assert result.actual_calls == []
    assert result.error == ""


def test_run_problem_raw_captures_backend_errors():
    class _FailingBackend:
        def chat(self, *args, **kwargs):
            raise RuntimeError("oMLX is down")

    problem = load_problems("simple_python", limit=1)[0]
    result = run_problem_raw(_FailingBackend(), problem)
    assert result.actual_calls == []
    assert "oMLX is down" in result.error


# --- v1.7 Lever 1 wiring tests --------------------------------------------

from benchmarks.bfcl.adapter import _spec_from_problem, _system_prompt_for


def test_system_prompt_irrelevance_has_abstain_clause():
    p = _system_prompt_for("irrelevance")
    # Must explicitly authorize decline and forbid invented calls.
    lowered = p.lower()
    assert "decline" in lowered
    assert "do not invent" in lowered


def test_system_prompt_default_unchanged_for_other_categories():
    """parallel, simple_python, etc. fall back to the default — abstain
    language MUST NOT leak into categories where tool calls are correct."""
    for cat in ("simple_python", "multiple", "parallel", "parallel_multiple"):
        p = _system_prompt_for(cat)
        assert "decline" not in p.lower(), (
            f"abstain clause leaked into {cat}; would suppress correct tool use"
        )


def test_spec_from_problem_irrelevance_zero_calls():
    spec = _spec_from_problem({"id": "irrelevance_0"}, "irrelevance")
    assert spec is not None
    assert len(spec.requirements) == 1
    assert spec.requirements[0].kind == "expects_zero_calls"


def test_spec_from_problem_parallel_min_tool_calls():
    gt = [
        {"toolkit.f1": {"a": [1]}},
        {"toolkit.f2": {"b": [2]}},
        {"toolkit.f3": {"c": [3]}},
    ]
    spec = _spec_from_problem({"id": "parallel_multiple_0"},
                              "parallel_multiple", ground_truth=gt)
    assert spec is not None
    req = spec.requirements[0]
    assert req.kind == "min_tool_calls"
    assert req.min_matches == 3


def test_spec_from_problem_single_call_returns_none():
    """simple_python / multiple problems are single-call — no Lever 1 spec.
    A None spec means run_agent runs as v1.6 did (no mid-loop reprompt)."""
    spec = _spec_from_problem({"id": "simple_0"}, "simple_python",
                              ground_truth=[{"f": {"a": [1]}}])
    assert spec is None


def test_spec_from_problem_parallel_with_one_call_returns_none():
    """If GT length is 1, parallel categories don't need Lever 1 — falls
    back to the v1.6 agent loop behavior."""
    spec = _spec_from_problem({"id": "parallel_0"}, "parallel",
                              ground_truth=[{"f": {"a": [1]}}])
    assert spec is None


def test_spec_from_problem_missing_ground_truth_returns_none():
    """parallel category without a GT (e.g., not in the GT map) safely
    returns None rather than raising."""
    spec = _spec_from_problem({"id": "parallel_0"}, "parallel",
                              ground_truth=None)
    assert spec is None


def test_run_problem_agent_passes_spec_for_irrelevance():
    """End-to-end: irrelevance category produces an expects_zero_calls
    Spec and the run_agent loop receives it."""
    from benchmarks.bfcl.adapter import run_problem_agent
    from luxe.config import RoleConfig
    role = RoleConfig(model_key="test", num_ctx=4096, max_steps=2,
                      max_tokens_per_turn=512, temperature=0.0)

    # Backend returns no tool calls — model abstains correctly.
    fake_resp = ChatResponse(
        text="I cannot answer this with the available tools.",
        tool_calls=[],
        finish_reason="stop",
        timing=GenerationTiming(prompt_tokens=50, completion_tokens=15),
    )
    problem = {
        "id": "irrelevance_0",
        "question": [[{"role": "user", "content": "What's the weather in Tokyo?"}]],
        "function": [{"name": "calculate_bmi",
                      "description": "BMI",
                      "parameters": {"type": "object",
                                     "properties": {"w": {"type": "number"}},
                                     "required": ["w"]}}],
    }
    backend = _MockBackend(fake_resp)
    result = run_problem_agent(backend, role, problem, category="irrelevance")
    assert result.actual_calls == []
    # Backend received the irrelevance-flavored system prompt, not the default.
    sys_msg = next(m for m in backend.last_messages
                   if m.get("role") == "system")
    assert "decline" in sys_msg["content"].lower()


def test_run_problem_agent_default_prompt_for_simple_category():
    """simple_python falls back to the default tool-eagerness prompt —
    abstain language must NOT appear."""
    from benchmarks.bfcl.adapter import run_problem_agent
    from luxe.config import RoleConfig
    role = RoleConfig(model_key="test", num_ctx=4096, max_steps=2,
                      max_tokens_per_turn=512, temperature=0.0)
    fake_resp = ChatResponse(
        text="done", tool_calls=[], finish_reason="stop",
        timing=GenerationTiming(prompt_tokens=50, completion_tokens=10),
    )
    problem = {
        "id": "simple_0",
        "question": [[{"role": "user", "content": "Compute 1+1"}]],
        "function": [{"name": "add",
                      "parameters": {"type": "object",
                                     "properties": {"a": {"type": "number"}},
                                     "required": ["a"]}}],
    }
    backend = _MockBackend(fake_resp)
    run_problem_agent(backend, role, problem, category="simple_python")
    sys_msg = next(m for m in backend.last_messages
                   if m.get("role") == "system")
    assert "decline" not in sys_msg["content"].lower()
