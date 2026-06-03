"""Tests for chat output rendering — the WS4 verbosity ladder + Markdown-safe
truncation primitive shared by chat (render_final) and the gitkit preview."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from luxe.chat.render import render_final, truncate_for_display


def test_truncate_fits_returns_unchanged():
    assert truncate_for_display("a\nb\nc", max_lines=50) == ("a\nb\nc", 0)


def test_truncate_caps_lines_and_reports_hidden():
    text = "\n".join(str(i) for i in range(20))
    shown, hidden = truncate_for_display(text, max_lines=5)
    assert hidden == 15
    assert shown.split("\n")[:5] == ["0", "1", "2", "3", "4"]


def test_truncate_closes_dangling_code_fence():
    # Cutting inside an open ``` fence must append a closing fence so the rest of
    # the terminal isn't swallowed by an unterminated code block.
    text = "intro\n```py\ncode\nmore\nmore2\nmore3"
    shown, hidden = truncate_for_display(text, max_lines=3)
    assert hidden > 0
    assert shown.rstrip().endswith("```")
    assert shown.count("```") % 2 == 0


def test_truncate_respects_max_chars():
    text = "\n".join("x" * 10 for _ in range(20))
    shown, hidden = truncate_for_display(text, max_lines=100, max_chars=25)
    assert hidden > 0


@pytest.mark.parametrize("mode,expect_hint", [
    ("full", False),
    ("truncated", True),
    ("compact", True),
])
def test_render_final_modes(mode, expect_hint):
    text = "\n".join(f"line {i}" for i in range(80))
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    render_final(console, text, mode=mode)
    assert ("for full" in out.getvalue()) == expect_hint


def test_render_final_empty():
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    render_final(console, "", mode="truncated")
    assert "no response text" in out.getvalue()
