"""Tests for chat render extras ported from the retired luxe_cli REPL:
the rainbow banner / color-shifting arrows, the tok/s footer metric, and the
start/end timestamp + elapsed line."""

from __future__ import annotations

import io
import random

from rich.console import Console

from luxe.agents.loop import AgentResult
from luxe.chat.render import (
    PROMPT_ARROW_PALETTE,
    arrow_prompt_markup,
    pick_no_adjacent_repeats,
    rainbow_banner,
    render_footer,
)


def _console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=200), buf


def test_pick_no_adjacent_repeats_never_repeats_neighbors():
    rng = random.Random(0)
    for n in range(2, 30):
        picks = pick_no_adjacent_repeats(n, rng=rng)
        assert len(picks) == n
        assert all(a != b for a, b in zip(picks, picks[1:]))
        assert all(c in PROMPT_ARROW_PALETTE for c in picks)


def test_rainbow_banner_keeps_label_and_uses_palette():
    out = rainbow_banner("luxe chat", rng=random.Random(1))
    assert "[bold white]luxe chat[/]" in out
    assert out.count(".") == 4 and out.count(":") == 2  # `.:.` on each side
    assert any(f"[{c}]" in out for c in PROMPT_ARROW_PALETTE)


def test_arrow_prompt_markup_has_three_colored_arrows():
    out = arrow_prompt_markup("luxe", rng=random.Random(2))
    assert out.startswith("luxe ")
    assert out.count("›") == 3
    assert out.count("[bold ") == 3


def _result(**kw) -> AgentResult:
    base = dict(
        steps=2, tool_calls_total=3, wall_s=4.0,
        prompt_tokens=1000, completion_tokens=200, peak_context_pressure=0.42,
    )
    base.update(kw)
    return AgentResult(**base)


def test_footer_reports_tok_per_s():
    console, buf = _console()
    render_footer(console, slot="chat", model="m", write_enabled=False, result=_result())
    out = buf.getvalue()
    assert "50 tok/s" in out  # 200 completion / 4.0s
    assert "4.0s" in out


def test_footer_tok_per_s_zero_wall_is_safe():
    console, buf = _console()
    render_footer(console, slot="chat", model="m", write_enabled=False,
                  result=_result(wall_s=0.0))
    assert "0 tok/s" in buf.getvalue()


def test_footer_timestamp_line_when_bookends_given():
    console, buf = _console()
    render_footer(
        console, slot="chat", model="m", write_enabled=False, result=_result(),
        started_at=1_000_000.0, ended_at=1_000_012.5,
    )
    out = buf.getvalue()
    assert "started " in out and "ended " in out
    assert "elapsed 12.5s" in out


def test_footer_no_timestamp_line_without_bookends():
    console, buf = _console()
    render_footer(console, slot="chat", model="m", write_enabled=False, result=_result())
    assert "elapsed" not in buf.getvalue()
