"""Text-channel tool-call recovery (backend.recover_tool_calls_from_text).

Qwen2.5-Coder models emit tool calls as fenced ```json blocks in `content`
instead of the chat template's native wrapper, so the server reports
`tool_calls: null` and the call arrives as prose. Recovery salvages exactly
that shape — and nothing looser. See the 2026-08-03 bake-off root-cause note
in backend.py.
"""

from luxe.backend import recover_tool_calls_from_text

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Edit a file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]},
    }},
]


def test_recovers_fenced_json_block():
    # The exact Qwen2.5-Coder-3B shape observed against llama-server, temp 0.
    text = '```json\n{\n  "name": "read_file",\n  "arguments": {\n    "path": "notes.txt"\n  }\n}\n```'
    calls = recover_tool_calls_from_text(text, TOOLS)
    assert len(calls) == 1
    assert calls[0].name == "read_file"
    assert calls[0].arguments == {"path": "notes.txt"}


def test_recovers_tool_call_tagged_json():
    text = '<tool_call>{"name": "edit_file", "arguments": {"path": "a.py"}}</tool_call>'
    calls = recover_tool_calls_from_text(text, TOOLS)
    assert len(calls) == 1
    assert calls[0].name == "edit_file"


def test_recovers_bare_json():
    text = '{"name": "read_file", "arguments": {"path": "x"}}'
    assert len(recover_tool_calls_from_text(text, TOOLS)) == 1


def test_recovers_json_with_prose_preamble():
    text = 'Sure — I will read the file:\n\n{"name": "read_file", "arguments": {"path": "x"}}'
    assert len(recover_tool_calls_from_text(text, TOOLS)) == 1


def test_string_arguments_parsed():
    text = '{"name": "read_file", "arguments": "{\\"path\\": \\"x\\"}"}'
    calls = recover_tool_calls_from_text(text, TOOLS)
    assert calls[0].arguments == {"path": "x"}


def test_unoffered_tool_not_recovered():
    # A model naming a tool we never offered must not be "recovered" into
    # calling it.
    text = '{"name": "delete_everything", "arguments": {}}'
    assert recover_tool_calls_from_text(text, TOOLS) == []


def test_prose_with_braces_not_recovered():
    text = "In Python, dicts look like {'a': 1} and that is not a tool call."
    assert recover_tool_calls_from_text(text, TOOLS) == []


def test_no_tools_offered_no_recovery():
    text = '{"name": "read_file", "arguments": {"path": "x"}}'
    assert recover_tool_calls_from_text(text, None) == []
    assert recover_tool_calls_from_text(text, []) == []


def test_empty_text_no_recovery():
    assert recover_tool_calls_from_text("", TOOLS) == []


def test_single_call_only():
    # Two calls in prose: only the first is acted on (single-action bias).
    text = ('{"name": "read_file", "arguments": {"path": "a"}}\n'
            '{"name": "edit_file", "arguments": {"path": "a"}}')
    calls = recover_tool_calls_from_text(text, TOOLS)
    assert len(calls) == 1
    assert calls[0].name == "read_file"


def test_braces_inside_strings_do_not_confuse_extractor():
    text = '{"name": "read_file", "arguments": {"path": "we{ird}.txt"}}'
    calls = recover_tool_calls_from_text(text, TOOLS)
    assert calls[0].arguments == {"path": "we{ird}.txt"}
