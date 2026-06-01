"""Tests for chat cancellation — the tool-event seam raises ChatCancelled when
the CancelToken is set, and is a clean no-op otherwise."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from luxe.chat.render import CancelToken, ChatCancelled, format_tool_call, make_tool_event
from luxe.tools.base import ToolCall


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False)


def _tc(**kw) -> ToolCall:
    base = dict(id="1", name="read_file", arguments={"path": "a.py"}, bytes_out=10)
    base.update(kw)
    return ToolCall(**base)


def test_no_cancel_is_noop():
    cancel = CancelToken()
    on_event = make_tool_event(_console(), cancel)
    on_event(_tc())  # must not raise


def test_cancel_requested_raises_at_tool_boundary():
    cancel = CancelToken(requested=True)
    on_event = make_tool_event(_console(), cancel)
    with pytest.raises(ChatCancelled):
        on_event(_tc())


def test_chat_cancelled_is_keyboardinterrupt():
    # So the loop's `except Exception` guards never swallow it.
    assert issubclass(ChatCancelled, KeyboardInterrupt)
    assert not issubclass(ChatCancelled, Exception)


def test_cancel_token_reset():
    cancel = CancelToken(requested=True)
    cancel.reset()
    assert cancel.requested is False


def test_format_tool_call_variants():
    assert "read_file" in format_tool_call(_tc())
    assert "cached" in format_tool_call(_tc(cached=True))
    assert "duplicate" in format_tool_call(_tc(duplicate=True))
    assert "✗" in format_tool_call(_tc(error="boom"))
