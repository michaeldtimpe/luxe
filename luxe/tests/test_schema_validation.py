"""Tests for luxe.agents.base._validate_args — the lightweight
client-side JSONSchema check that runs before every tool dispatch."""

from __future__ import annotations

from harness.backends import ToolDef

from luxe_cli.agents.base import _validate_args


def _def(required=(), props=None) -> ToolDef:
    return ToolDef(
        name="test",
        description="x",
        parameters={
            "type": "object",
            "properties": props or {},
            "required": list(required),
        },
    )


def test_no_defn_no_error():
    """Unknown tools bypass validation — the dispatch layer returns an
    `unknown tool` error on its own."""
    assert _validate_args(None, {"anything": 1}) is None


def test_required_present():
    d = _def(required=["pattern"], props={"pattern": {"type": "string"}})
    assert _validate_args(d, {"pattern": "foo"}) is None


def test_required_missing_returns_error():
    d = _def(required=["pattern"], props={"pattern": {"type": "string"}})
    err = _validate_args(d, {})
    assert err is not None and "pattern" in err


def test_wrong_type_returns_error():
    d = _def(props={"limit": {"type": "integer"}})
    err = _validate_args(d, {"limit": "twenty"})
    assert err is not None and "limit" in err and "integer" in err


def test_bool_is_not_integer():
    """`bool` is a subclass of `int` in Python. Without special-casing,
    a schema asking for `integer` would accept `True`. Guard against
    that because a model sending `True` for `limit` is almost certainly
    a bug, not intent."""
    d = _def(props={"limit": {"type": "integer"}})
    assert _validate_args(d, {"limit": True}) is not None


def test_number_accepts_int_or_float():
    d = _def(props={"v": {"type": "number"}})
    assert _validate_args(d, {"v": 1}) is None
    assert _validate_args(d, {"v": 1.5}) is None
    assert _validate_args(d, {"v": "x"}) is not None


def test_missing_properties_schema_is_permissive():
    """If a property has no type constraint declared, we don't
    second-guess the model."""
    d = _def(props={"p": {}})
    assert _validate_args(d, {"p": [1, 2, 3]}) is None


def test_grep_without_pattern_is_rejected():
    """Regression: the bug that prompted this work. The model emitted
    `grep path=foo.py` with no pattern — should be rejected by schema,
    never reach the ripgrep subprocess."""
    grep_def = ToolDef(
        name="grep",
        description="ripgrep",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string"},
            },
            "required": ["pattern"],
        },
    )
    err = _validate_args(grep_def, {"path": "foo.py"})
    assert err is not None and "pattern" in err
