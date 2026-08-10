"""Tests for the Muse Glimmer ATEM tool-call parser.

The parser lives in ``vendor/omlx_patches/muse_glimmer/`` because it is
installed into oMLX's Cellar tree, which ``brew upgrade`` replaces. Authoring
and testing it here is what keeps it from being silently lost.

End-to-end verification against the live model is impossible today — mlx-vlm
has no ``muse_glimmer`` architecture (open PR Blaizzy/mlx-vlm#1838), so the
weights cannot load. What *is* verifiable is the parsing contract, and it is
pinned two ways:

* ``_render_atem`` below mirrors the model's own jinja ``render_atem`` macro
  branch-for-branch, so the round-trip tests feed the parser exactly what the
  template emits rather than what the author imagined it emits.
* ``TEMPLATE_WORKED_EXAMPLE`` is copied verbatim out of the published
  ``chat_template.jinja`` — real template bytes, not a reconstruction.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PARSER_PATH = (
    Path(__file__).resolve().parents[1]
    / "vendor"
    / "omlx_patches"
    / "muse_glimmer"
    / "muse_glimmer_tool_parser.py"
)

_spec = importlib.util.spec_from_file_location("_muse_glimmer_parser", _PARSER_PATH)
assert _spec and _spec.loader
parser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parser)

parse_tool_call = parser.parse_tool_call


# ---------------------------------------------------------------------------
# Mirror of the template's render_atem macro.
#
# Verbatim from chat_template.jinja:
#   '<atem:function_calls>\n<atem:invoke name="' + tc.function.name + '">\n'
#   then per argument:
#     '<atem:parameter name="' + k + '">'
#     boolean -> true/false | none -> null
#     mapping or (iterable and not string) -> v | tojson
#     else -> v
#     '</atem:parameter>\n'
#   then '</atem:invoke>\n</atem:function_calls>'
# ---------------------------------------------------------------------------
def _render_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v)
    return str(v)


def _render_atem(name: str, arguments: dict) -> str:
    body = "".join(
        f'<atem:parameter name="{k}">{_render_value(v)}</atem:parameter>\n'
        for k, v in arguments.items()
    )
    return (
        f'<atem:function_calls>\n<atem:invoke name="{name}">\n'
        f"{body}</atem:invoke>\n</atem:function_calls>"
    )


def _tools(name: str, properties: dict) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "",
                "parameters": {"type": "object", "properties": properties},
            },
        }
    ]


class TestRoundTrip:
    """Emit through the template's contract, parse back, compare."""

    def test_simple_string_arguments(self):
        args = {"path": "src/main.py", "pattern": "def run"}
        tools = _tools(
            "grep", {"path": {"type": "string"}, "pattern": {"type": "string"}}
        )
        out = parse_tool_call(_render_atem("grep", args), tools)
        assert out == {"name": "grep", "arguments": args}

    def test_typed_scalars_round_trip(self):
        args = {"limit": 40, "recursive": True, "quiet": False, "cursor": None}
        props = {
            "limit": {"type": "integer"},
            "recursive": {"type": "boolean"},
            "quiet": {"type": "boolean"},
            "cursor": {"type": "string"},
        }
        out = parse_tool_call(_render_atem("scan", args), _tools("scan", props))
        # cursor is declared string, so the literal "null" survives as text —
        # the schema is authoritative over the JSON guess.
        assert out["arguments"]["limit"] == 40
        assert out["arguments"]["recursive"] is True
        assert out["arguments"]["quiet"] is False
        assert out["arguments"]["cursor"] == "null"

    def test_collections_round_trip_via_json(self):
        args = {"paths": ["a.py", "b.py"], "opts": {"deep": True, "n": 3}}
        props = {"paths": {"type": "array"}, "opts": {"type": "object"}}
        out = parse_tool_call(_render_atem("bulk", args), _tools("bulk", props))
        assert out["arguments"] == args


class TestWhitespaceIsPreserved:
    """The template states spaces are not stripped; stripping corrupts code."""

    def test_leading_and_trailing_spaces_survive(self):
        raw = "    indented = True  "
        tools = _tools("write_file", {"content": {"type": "string"}})
        out = parse_tool_call(_render_atem("write_file", {"content": raw}), tools)
        assert out["arguments"]["content"] == raw

    def test_multiline_value_survives_exactly(self):
        body = 'def f():\n    return "x"\n'
        tools = _tools("write_file", {"content": {"type": "string"}})
        out = parse_tool_call(_render_atem("write_file", {"content": body}), tools)
        assert out["arguments"]["content"] == body

    def test_value_containing_angle_brackets_and_quotes(self):
        # Not valid XML on purpose — the template says so explicitly.
        raw = 'if a < b and c > d: print("<tag>")'
        tools = _tools("bash", {"cmd": {"type": "string"}})
        out = parse_tool_call(_render_atem("bash", {"cmd": raw}), tools)
        assert out["arguments"]["cmd"] == raw


class TestSchemaTyping:
    def test_declared_string_is_not_coerced(self):
        # Without the schema this would decode to the float 1.10 -> 1.1.
        tools = _tools("pin", {"version": {"type": "string"}})
        out = parse_tool_call(_render_atem("pin", {"version": "1.10"}), tools)
        assert out["arguments"]["version"] == "1.10"

    def test_numeric_string_without_schema_is_decoded(self):
        out = parse_tool_call(_render_atem("pin", {"version": "1.10"}), None)
        assert out["arguments"]["version"] == 1.1

    def test_unparseable_value_falls_back_to_raw_text(self):
        out = parse_tool_call(_render_atem("say", {"msg": "hello there"}), None)
        assert out["arguments"]["msg"] == "hello there"


class TestMultipleInvokes:
    def test_two_invokes_return_a_list(self):
        text = (
            '<atem:function_calls>\n<atem:invoke name="a">\n'
            '<atem:parameter name="x">1</atem:parameter>\n</atem:invoke>\n'
            '<atem:invoke name="b">\n'
            '<atem:parameter name="y">2</atem:parameter>\n</atem:invoke>\n'
            "</atem:function_calls>"
        )
        out = parse_tool_call(text, None)
        assert isinstance(out, list)
        assert [c["name"] for c in out] == ["a", "b"]
        assert out[0]["arguments"] == {"x": 1}

    def test_single_invoke_returns_a_dict(self):
        out = parse_tool_call(_render_atem("solo", {"x": 1}), None)
        assert isinstance(out, dict)


class TestRobustness:
    def test_missing_wrapper_still_parses(self):
        # The caller may strip the tool_call_start sentinel before dispatching.
        text = (
            '<atem:invoke name="read_file">\n'
            '<atem:parameter name="path">a.py</atem:parameter>\n</atem:invoke>'
        )
        assert parse_tool_call(text, None)["name"] == "read_file"

    def test_no_tool_call_raises(self):
        with pytest.raises(ValueError):
            parse_tool_call("I'll just explain instead.", None)

    def test_name_whitespace_is_stripped(self):
        # GLM-4.5-Air emitted "read_file\n" and every dispatch missed until
        # tools/base.py stripped; do not reintroduce that failure here.
        text = '<atem:invoke name="read_file\n">\n</atem:invoke>'
        assert parse_tool_call(text, None)["name"] == "read_file"

    def test_zero_argument_call(self):
        out = parse_tool_call(_render_atem("list_files", {}), None)
        assert out == {"name": "list_files", "arguments": {}}

    def test_namespaced_name_is_preserved_verbatim(self):
        # The template supports ns.func names; dispatch matches exact strings,
        # so the namespace must not be silently stripped.
        out = parse_tool_call(_render_atem("fs.read_file", {}), None)
        assert out["name"] == "fs.read_file"


# Copied verbatim from the published chat_template.jinja worked example.
TEMPLATE_WORKED_EXAMPLE = (
    '<atem:function_calls>\n<atem:invoke name="example_tool_name.example_function_name">\n'
    '<atem:parameter name="example_parameter_1">value_1</atem:parameter>\n'
    '<atem:parameter name="example_parameter_2">This is the value for the second parameter\n'
    'that can span\n"multiple" lines\n</atem:parameter>\n'
    "</atem:invoke>\n</atem:function_calls>"
)


class TestAgainstRealTemplateBytes:
    def test_worked_example_parses(self):
        out = parse_tool_call(TEMPLATE_WORKED_EXAMPLE, None)
        assert out["name"] == "example_tool_name.example_function_name"
        assert out["arguments"]["example_parameter_1"] == "value_1"
        assert out["arguments"]["example_parameter_2"] == (
            "This is the value for the second parameter\n"
            'that can span\n"multiple" lines\n'
        )

    def test_sentinels_match_the_template(self):
        assert parser.tool_call_start == "<atem:function_calls>"
        assert parser.tool_call_end == "</atem:function_calls>"
        assert TEMPLATE_WORKED_EXAMPLE.startswith(parser.tool_call_start)
        assert TEMPLATE_WORKED_EXAMPLE.endswith(parser.tool_call_end)
