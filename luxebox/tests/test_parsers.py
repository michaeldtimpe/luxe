"""Unit tests for the tool-call text parsers in luxe.agents.base.

These paths are where model output meets our dispatch loop — regressions
here tend to show up as silently dropped tool calls, so pinning them in
tests is worth the small maintenance cost.
"""

from __future__ import annotations

from luxe.agents.base import (
    _parse_python_calls,
    _parse_text_tool_calls,
)


KNOWN = {"list_dir", "read_file", "write_file", "grep", "glob", "edit_file"}


# ── JSON / <tool_call> recovery ──────────────────────────────────────


def test_qwen_tool_call_tag():
    text = '<tool_call>\n{"name": "list_dir", "arguments": {"path": "."}}\n</tool_call>'
    calls = _parse_text_tool_calls(text, KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "list_dir"
    assert calls[0].arguments == {"path": "."}


def test_fenced_json_block():
    text = '```json\n{"name": "read_file", "arguments": {"path": "README.md"}}\n```'
    calls = _parse_text_tool_calls(text, KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "read_file"


def test_bare_json_line_with_parameters_alias():
    # Some models emit `parameters` instead of `arguments`; the parser
    # accepts either so this doesn't silently drop calls.
    text = '{"name": "grep", "parameters": {"pattern": "TODO"}}'
    calls = _parse_text_tool_calls(text, KNOWN)
    assert len(calls) == 1
    assert calls[0].arguments == {"pattern": "TODO"}


def test_unknown_function_is_ignored():
    text = '<tool_call>\n{"name": "exec_shell", "arguments": {"cmd": "rm -rf /"}}\n</tool_call>'
    assert _parse_text_tool_calls(text, KNOWN) == []


# ── Gemma 3 tool_code / python fences ────────────────────────────────


def test_gemma_tool_code_block():
    text = 'Sure.\n```tool_code\nlist_dir(path=".")\n```'
    calls = _parse_text_tool_calls(text, KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "list_dir"
    assert calls[0].arguments == {"path": "."}


def test_gemma_python_fence_with_print_unwrap():
    # The real Gemma 3 27B tends to emit ```python + print(...)``` when
    # `tool_code` priming is imperfect. We unwrap one layer of print().
    text = '```python\nprint(read_file(path="foo.md"))\n```'
    calls = _parse_text_tool_calls(text, KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "foo.md"}


def test_python_parser_rejects_unknown_call():
    parsed = _parse_python_calls('os.system("bad")', KNOWN)
    assert parsed == []


def test_python_parser_rejects_non_literal_kwargs():
    # The AST path uses ast.literal_eval on kwarg values; a bare name
    # (undefined variable) gets silently dropped rather than raising.
    parsed = _parse_python_calls("list_dir(path=unbound_var)", KNOWN)
    # kwarg dropped, call still recognized with empty args
    assert len(parsed) == 1
    assert parsed[0]["name"] == "list_dir"
    assert parsed[0]["arguments"] == {}


def test_empty_text_parses_to_empty():
    assert _parse_text_tool_calls("", KNOWN) == []
    assert _parse_text_tool_calls("just prose, no call", KNOWN) == []


# ── Multi-line bare JSON (Qwen pretty-printed tool calls) ────────────


def test_multiline_bare_json_single_call():
    # Qwen2.5-coder sometimes emits pretty-printed bare JSON without
    # any code fence when it fails to use the structured tool-call
    # channel. This was silently dropped before — now recovered.
    text = """## Subtask 3
{
  "name": "grep",
  "arguments": {
    "pattern": "eval|exec"
  }
}"""
    calls = _parse_text_tool_calls(text, KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "grep"
    assert calls[0].arguments == {"pattern": "eval|exec"}


def test_multiline_bare_json_multiple_calls():
    text = """First:
{
  "name": "grep",
  "arguments": {"pattern": "foo"}
}

Second:
{
  "name": "read_file",
  "arguments": {"path": "bar.py"}
}"""
    calls = _parse_text_tool_calls(text, KNOWN)
    assert len(calls) == 2
    assert calls[0].name == "grep"
    assert calls[1].name == "read_file"


def test_multiline_bare_json_filters_unknown():
    # Unrelated JSON object in prose shouldn't be interpreted as a call.
    text = """Config example:
{
  "timeout": 30,
  "retries": 3
}

Then: {"name": "list_dir", "arguments": {}}"""
    calls = _parse_text_tool_calls(text, KNOWN)
    assert len(calls) == 1
    assert calls[0].name == "list_dir"
