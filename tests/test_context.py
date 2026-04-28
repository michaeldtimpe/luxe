"""Tests for context pressure monitoring."""

from luxe.context import context_pressure, elide_old_tool_results, estimate_tokens


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") == 2  # 11 chars // 4


def test_context_pressure_empty():
    assert context_pressure([], 8192) == 0.0


def test_context_pressure_calculation():
    messages = [{"role": "user", "content": "x" * 4000}]
    pressure = context_pressure(messages, 2000)
    assert pressure > 0.4


def test_elide_below_threshold():
    messages = [
        {"role": "user", "content": "short"},
        {"role": "tool", "name": "read_file", "content": "data"},
    ]
    result = elide_old_tool_results(messages, 100000)
    assert result[1]["content"] == "data"  # not elided


def test_elide_above_threshold():
    big_content = "x" * 10000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "name": "read_file", "content": big_content},
        {"role": "tool", "name": "grep", "content": big_content},
        {"role": "tool", "name": "read_file", "content": big_content},
        {"role": "tool", "name": "grep", "content": big_content},
        {"role": "tool", "name": "read_file", "content": "keep1"},
        {"role": "tool", "name": "read_file", "content": "keep2"},
        {"role": "tool", "name": "read_file", "content": "keep3"},
        {"role": "tool", "name": "read_file", "content": "keep4"},
    ]
    result = elide_old_tool_results(messages, 1000, threshold=0.1)
    assert "[elided:" in result[1]["content"]
    assert result[-1]["content"] == "keep4"  # recent kept
