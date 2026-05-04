"""BFCL function-spec → luxe ToolDef converter.

PRELIMINARY (2026-05-03). BFCL function specs share OpenAPI/JSON-Schema
shape with luxe's ToolDef.parameters, but with category-specific quirks
(BFCL has its own type system in some categories — `tuple`, `dict`,
`any` vs JSON-Schema's `array`/`object`/no-type). This module normalizes
common cases.

Reference: https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard
(verify against the actual repo schema once `pip install bfcl_eval` is approved).

The converter does NOT execute. It builds a ToolDef + a stub executor
that returns a synthetic mock response — BFCL grades the agent on
*whether the tool was called correctly*, not on what the underlying
behavior would have done.
"""

from __future__ import annotations

from typing import Any

from luxe.tools.base import ToolDef, ToolFn


# BFCL parameter "type" values that map to JSON-Schema equivalents. Some
# categories use these directly; others use JSON-Schema. Both are tolerated.
_BFCL_TYPE_TO_JSONSCHEMA = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "float": "number",
    "boolean": "boolean",
    "array": "array",
    "list": "array",
    "tuple": "array",
    "dict": "object",
    "object": "object",
    "any": "string",  # most permissive fallback for the chat template
}


def normalize_parameters(bfcl_params: dict[str, Any]) -> dict[str, Any]:
    """Convert BFCL parameters dict to JSON-Schema. Tolerates already-
    JSON-Schema input (passthrough). Recursively normalizes nested
    `properties` and `items`."""
    if not isinstance(bfcl_params, dict):
        return {"type": "object", "properties": {}, "required": []}

    out: dict[str, Any] = {}
    for k, v in bfcl_params.items():
        if k == "type" and isinstance(v, str):
            out[k] = _BFCL_TYPE_TO_JSONSCHEMA.get(v.lower(), v)
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: normalize_parameters(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = normalize_parameters(v)
        else:
            out[k] = v
    return out


def bfcl_func_spec_to_tool_def(spec: dict[str, Any]) -> ToolDef:
    """Build a luxe ToolDef from a BFCL function spec.

    Expected spec shape (BFCL v3):
        {"name": "func_name",
         "description": "...",
         "parameters": {...}}    # JSON-Schema or BFCL-typed object schema
    """
    return ToolDef(
        name=str(spec.get("name", "unnamed_function")),
        description=str(spec.get("description", "")),
        parameters=normalize_parameters(spec.get("parameters", {})),
    )


def make_stub_executor(spec: dict[str, Any]) -> ToolFn:
    """Return a stub executor matching luxe's ToolFn signature.

    BFCL grades the agent on the SHAPE of the tool call (function name +
    arguments), not on the result. The stub returns a synthetic
    placeholder so the agent loop can continue if the agent issues
    follow-up calls (e.g., multi_turn category).
    """
    name = spec.get("name", "unnamed_function")

    def _stub(args: dict[str, Any]) -> tuple[str, str | None]:
        # Echo back the arguments so multi_turn agents can chain on the
        # result; BFCL's multi_turn ground truths assume mock output that
        # echoes inputs by default.
        return f"[stub:{name}] called with args={args}", None

    return _stub
