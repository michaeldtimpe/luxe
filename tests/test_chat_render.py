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


# --- leaked-reasoning hygiene ------------------------------------------------


def test_strip_leaked_reasoning_drops_headless_think_block():
    """The GLM leak shape: no opening tag, reasoning + `</think>` + answer
    all in content (server splitter needs the opening tag to separate)."""
    from luxe.chat.render import strip_leaked_reasoning

    leaked = "Now I have all the evidence. Step 1...\n</think>\n## Report\nAnswer."
    assert strip_leaked_reasoning(leaked) == "## Report\nAnswer."


def test_strip_leaked_reasoning_drops_a_full_think_block():
    from luxe.chat.render import strip_leaked_reasoning

    leaked = "<think>chain of thought</think>The answer is 42."
    assert strip_leaked_reasoning(leaked) == "The answer is 42."


def test_strip_leaked_reasoning_is_a_noop_without_the_marker():
    from luxe.chat.render import strip_leaked_reasoning

    for text in ("plain reply", "", "code: `<thinking>` isn't the marker"):
        assert strip_leaked_reasoning(text) == text


class TestComposeAnswer:
    """The visible reply is every step's prose, not just the last step's.

    `AgentResult.final_text` is the LAST step's text. A model that speaks
    before it acts leaves the opening of its answer on an intermediate step,
    and chat rendered the tail alone — replies literally started mid-thought
    (session 0eb5998d8825 turn -7: 3 steps, 2 tool calls, the lead-in stranded
    in step 1). The join is CHAT-ONLY: `final_text` is untouched.
    """

    def test_step_prose_comes_before_the_final_answer(self):
        from luxe.chat.render import compose_answer
        r = AgentResult(step_texts=["Let me check the config."],
                        final_text="It sets the strict flag.")
        assert compose_answer(r) == (
            "Let me check the config.\n\nIt sets the strict flag.")

    def test_multiple_steps_keep_the_order_the_model_produced(self):
        from luxe.chat.render import compose_answer
        r = AgentResult(step_texts=["First I'll look.", "Now the tests."],
                        final_text="All green.")
        assert compose_answer(r) == (
            "First I'll look.\n\nNow the tests.\n\nAll green.")

    def test_a_turn_with_no_step_prose_is_unchanged(self):
        """The overwhelmingly common case must be byte-identical."""
        from luxe.chat.render import compose_answer
        assert compose_answer(AgentResult(final_text="just this")) == "just this"

    def test_a_recap_is_not_shown_twice(self):
        """Models recap themselves; the final answer often repeats the lead-in."""
        from luxe.chat.render import compose_answer
        r = AgentResult(step_texts=["Checking the config."],
                        final_text="Checking the config. It sets the flag.")
        assert compose_answer(r) == "Checking the config. It sets the flag."

    def test_identical_steps_collapse(self):
        from luxe.chat.render import compose_answer
        r = AgentResult(step_texts=["thinking", "thinking"], final_text="done")
        assert compose_answer(r) == "thinking\n\ndone"

    def test_blank_steps_are_dropped(self):
        from luxe.chat.render import compose_answer
        r = AgentResult(step_texts=["", "   \n"], final_text="answer")
        assert compose_answer(r) == "answer"

    def test_step_prose_survives_an_empty_final_answer(self):
        """The turn -7 shape at its worst: everything the model said lived in
        intermediate steps and the last step was silent."""
        from luxe.chat.render import compose_answer
        r = AgentResult(step_texts=["Here is what I found."], final_text="")
        assert compose_answer(r) == "Here is what I found."

    def test_no_result_is_the_empty_string(self):
        from luxe.chat.render import compose_answer
        assert compose_answer(None) == ""

    def test_final_text_itself_is_never_mutated(self):
        """No benchmark grader may see the join."""
        from luxe.chat.render import compose_answer
        r = AgentResult(step_texts=["lead-in"], final_text="answer")
        compose_answer(r)
        assert r.final_text == "answer"
        assert r.step_texts == ["lead-in"]
