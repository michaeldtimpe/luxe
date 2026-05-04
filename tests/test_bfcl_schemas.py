"""Tests for benchmarks/bfcl/schemas.py — BFCL function-spec converter.

PRELIMINARY (2026-05-03). Validates the schema normalization + stub
executor signatures. Real-dataset integration deferred until BFCL
package install is approved.
"""

from __future__ import annotations

from benchmarks.bfcl.schemas import (
    bfcl_func_spec_to_tool_def,
    make_stub_executor,
    normalize_parameters,
)


def test_normalize_passes_jsonschema_through():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    assert normalize_parameters(schema) == schema


def test_normalize_maps_bfcl_types():
    schema = {"type": "dict", "properties": {"items": {"type": "list", "items": {"type": "any"}}}}
    out = normalize_parameters(schema)
    assert out["type"] == "object"
    assert out["properties"]["items"]["type"] == "array"
    assert out["properties"]["items"]["items"]["type"] == "string"  # `any` → string fallback


def test_normalize_handles_tuple_as_array():
    schema = {"type": "tuple", "items": {"type": "integer"}}
    assert normalize_parameters(schema)["type"] == "array"


def test_normalize_preserves_unknown_keys():
    schema = {"type": "string", "enum": ["a", "b"], "minLength": 1}
    out = normalize_parameters(schema)
    assert out["enum"] == ["a", "b"]
    assert out["minLength"] == 1


def test_normalize_tolerates_non_dict_input():
    assert normalize_parameters([]) == {"type": "object", "properties": {}, "required": []}
    assert normalize_parameters("nope") == {"type": "object", "properties": {}, "required": []}


def test_to_tool_def_basic():
    spec = {"name": "add", "description": "add two numbers",
            "parameters": {"type": "object",
                           "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                           "required": ["a", "b"]}}
    td = bfcl_func_spec_to_tool_def(spec)
    assert td.name == "add"
    assert td.description == "add two numbers"
    assert td.parameters["type"] == "object"
    assert "a" in td.parameters["properties"]


def test_to_tool_def_normalizes_bfcl_types():
    spec = {"name": "f", "description": "",
            "parameters": {"type": "dict", "properties": {"xs": {"type": "list", "items": {"type": "float"}}}}}
    td = bfcl_func_spec_to_tool_def(spec)
    assert td.parameters["type"] == "object"
    assert td.parameters["properties"]["xs"]["type"] == "array"
    assert td.parameters["properties"]["xs"]["items"]["type"] == "number"


def test_stub_executor_returns_tuple():
    spec = {"name": "lookup", "parameters": {}}
    exec_fn = make_stub_executor(spec)
    result, err = exec_fn({"key": "x"})
    assert err is None
    assert "stub:lookup" in result
    assert "key" in result


def test_stub_executor_handles_empty_args():
    exec_fn = make_stub_executor({"name": "noargs"})
    result, err = exec_fn({})
    assert err is None
    assert "stub:noargs" in result
