"""Tests for the user-writable tool library.

Covers the AST safety check, save→load round-trip, and keyword-based
matching. Uses a tmp_path fixture so tests don't touch ~/.luxe/tools/.
"""

from __future__ import annotations

import pytest

from luxe import tool_library as tl


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path, monkeypatch):
    """Redirect TOOLS_ROOT at the module level for the duration of each test."""
    monkeypatch.setattr(tl, "TOOLS_ROOT", tmp_path / "tools")
    yield


# ── Safety ────────────────────────────────────────────────────────────


def test_is_safe_code_accepts_plain_compute():
    ok, _ = tl.is_safe_code("def compute(x, y):\n    return x + y\n")
    assert ok


def test_is_safe_code_requires_compute_fn():
    ok, msg = tl.is_safe_code("def other():\n    return 1\n")
    assert not ok
    assert "compute" in msg


def test_is_safe_code_rejects_import():
    ok, msg = tl.is_safe_code("import os\ndef compute(): return 1\n")
    assert not ok
    assert "Import" in msg


def test_is_safe_code_rejects_exec_call():
    ok, _ = tl.is_safe_code('def compute(): return exec("1+1")')
    assert not ok


def test_is_safe_code_rejects_dunder_access():
    ok, _ = tl.is_safe_code("def compute(x): return x.__class__\n")
    assert not ok


def test_is_safe_code_rejects_syntax_error():
    ok, _ = tl.is_safe_code("def compute(: 1\n")
    assert not ok


# ── Save / load round-trip ────────────────────────────────────────────


def _make_params() -> dict:
    return {
        "type": "object",
        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        "required": ["a", "b"],
    }


def test_save_and_load_round_trip():
    ok, path = tl.save_tool(
        name="sum2",
        description="Add two numbers.",
        parameters=_make_params(),
        python_code="def compute(a, b):\n    return {'sum': a + b}\n",
        tags=["math", "add"],
    )
    assert ok, path
    entries = tl.list_tools()
    assert len(entries) == 1
    meta = entries[0]
    assert meta["name"] == "sum2"
    fn = tl.load_callable(meta)
    assert fn is not None
    assert fn(a=2, b=3) == {"sum": 5}


def test_save_tool_rejects_bad_name():
    ok, msg = tl.save_tool(
        name="Invalid Name!",
        description="x",
        parameters={},
        python_code="def compute(): return 1",
    )
    assert not ok
    assert "snake_case" in msg


def test_save_tool_rejects_unsafe_code():
    ok, msg = tl.save_tool(
        name="bad_tool",
        description="x",
        parameters={},
        python_code="import os\ndef compute(): return 1",
    )
    assert not ok
    assert "Import" in msg


def test_create_tool_fn_error_path():
    result, err = tl.create_tool_fn({
        "name": "bad",
        "description": "x",
        "parameters": {},
        "python_code": "import os\ndef compute(): return 1",
    })
    assert result is None
    assert err and "Import" in err


# ── Matching ──────────────────────────────────────────────────────────


def test_match_tools_by_tag_and_name():
    tl.save_tool(
        name="ev_charging_time",
        description="Estimate EV charge time.",
        parameters=_make_params(),
        python_code="def compute(a, b):\n    return a\n",
        tags=["ev", "charging"],
    )
    tl.save_tool(
        name="tip_split",
        description="Split a bill with tip.",
        parameters=_make_params(),
        python_code="def compute(a, b):\n    return a\n",
        tags=["money", "split"],
    )

    # EV-flavored task should find the EV tool, not the tip one.
    matched = tl.match_tools("plan an EV trip with charging stops")
    names = [m["name"] for m in matched]
    assert "ev_charging_time" in names
    assert "tip_split" not in names


def test_match_tools_empty_text():
    assert tl.match_tools("") == []
    assert tl.match_tools("no keywords here") == []


def test_tool_def_from_meta_shape():
    tl.save_tool(
        name="area_rect",
        description="Area of a rectangle.",
        parameters=_make_params(),
        python_code="def compute(a, b):\n    return a * b\n",
        tags=["geometry"],
    )
    meta = tl.list_tools()[0]
    td = tl.tool_def_from_meta(meta)
    assert td.name == "area_rect"
    assert "Area" in td.description
    assert td.parameters["type"] == "object"
