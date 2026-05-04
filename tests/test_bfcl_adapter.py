"""Tests for benchmarks/bfcl/adapter.py — problem loading + dispatch shape.

PRELIMINARY (2026-05-03). Validates message construction and tool-spec
extraction against the installed bfcl_eval data layout. Backend
interaction is mocked.
"""

from __future__ import annotations

from typing import Any

import pytest

from benchmarks.bfcl.adapter import (
    SUPPORTED_CATEGORIES,
    _problem_messages,
    _problem_tools,
    load_ground_truth,
    load_problems,
    run_problem_raw,
)
from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse


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
