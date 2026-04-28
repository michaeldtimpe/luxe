"""Tests for architect output parsing."""

from luxe.agents.architect import _parse_objectives


def test_parse_valid_json():
    text = '''[
        {"title": "Read config files", "role": "worker_read", "expected_tools": 3, "scope": "config/"},
        {"title": "Run linter", "role": "worker_analyze", "expected_tools": 2, "scope": "."}
    ]'''
    result = _parse_objectives(text)
    assert len(result) == 2
    assert result[0]["role"] == "worker_read"
    assert result[1]["role"] == "worker_analyze"


def test_parse_with_markdown_fences():
    text = '''```json
    [
        {"title": "Survey repo", "role": "worker_read", "expected_tools": 3, "scope": "."}
    ]
    ```'''
    result = _parse_objectives(text)
    assert len(result) == 1
    assert result[0]["title"] == "Survey repo"


def test_parse_with_surrounding_text():
    text = '''Here's my plan:
    [{"title": "Check auth", "role": "worker_analyze", "expected_tools": 2, "scope": "auth/"}]
    That should cover it.'''
    result = _parse_objectives(text)
    assert len(result) == 1


def test_parse_invalid_role_falls_back():
    text = '[{"title": "Do something", "role": "invalid_role"}]'
    result = _parse_objectives(text)
    assert result[0]["role"] == "worker_read"


def test_parse_garbage_falls_back():
    text = "I couldn't understand the task."
    result = _parse_objectives(text)
    assert len(result) == 1
    assert "fallback" in result[0]["title"].lower() or "failed" in result[0]["title"].lower()


def test_parse_empty_array_falls_back():
    text = "[]"
    result = _parse_objectives(text)
    assert len(result) == 1
