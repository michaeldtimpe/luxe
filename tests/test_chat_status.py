"""Tests for the chat bottom-toolbar status bar — a port of the applicable
yet-another-statusline segments (git-aware, ctx, rate, timing, model-last)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.chat import status as status_mod
from luxe.chat.session import ChatSession
from luxe.chat.slots import SlotManager
from luxe.chat.status import StatusState, fields, git_info, status_markup
from luxe.config import PipelineConfig, RoleConfig


@pytest.fixture
def slots(monkeypatch):
    from luxe.chat import slots as slots_module

    class FakeBackend:
        def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
            self.model = model

        def unload_all_loaded(self, *, except_for=None):
            return {}

    monkeypatch.setattr(slots_module, "Backend", FakeBackend)
    cfg = PipelineConfig(models={"monolith": "Qwen3.6-35B-A3B-6bit"},
                         roles={"monolith": RoleConfig(model_key="monolith")})
    return SlotManager(cfg)


def _flat(segs) -> str:
    """Flatten list[Segment] to plain text for assertions."""
    return " · ".join("".join(t for t, _p, _r in seg.spans) for seg in segs)


def test_mode_shows_on_off_explicitly(slots):
    out = _flat(fields(ChatSession(), slots, "", StatusState()))
    assert "write off" in out and "bash off" in out and "web off" in out


def test_mode_on_when_enabled(slots):
    out = _flat(fields(ChatSession(write_enabled=True, unrestricted_bash=True,
                                   web_enabled=True),
                       slots, "", StatusState()))
    assert "write on" in out and "bash on" in out and "web on" in out


def test_session_mode_chip_omitted_when_default(slots):
    out = _flat(fields(ChatSession(), slots, "", StatusState()))
    assert "mode " not in out


def test_session_mode_chip_shows_active_flags(slots):
    s = ChatSession(verbose_level="diff", compact=True, show_reasoning=True,
                    terse=False)
    out = _flat(fields(s, slots, "", StatusState()))
    assert "mode " in out
    for bit in ("verbose:diff", "reason", "compact", "terse:off"):
        assert bit in out


def test_segment_order_matches_spec(slots):
    # path · ctx · cache · start · last · write · bash · slot · model
    st = StatusState(slot="chat", model="Champ-9000", ctx_pressure=0.1,
                     num_ctx=32768, prompt_tokens=9000, has_turn=True,
                     opened_at=1_000_000.0)
    labels = [_flat([seg]) for seg in fields(ChatSession(), slots, "/r", st)]

    def pos(token): return next(i for i, l in enumerate(labels) if token in l)
    assert pos("/r") < pos("ctx ") < pos("cache ") < pos("start ") < pos("last ") \
        < pos("write ") < pos("bash ") < pos("web ") < pos("chat") \
        < pos("Champ-9000")


def test_ctx_shows_percent_and_window_size(slots):
    st = StatusState(ctx_pressure=0.42, num_ctx=131072, has_turn=True)
    out = _flat(fields(ChatSession(), slots, "", st))
    assert "ctx 42%" in out and "128K" in out  # 131072 → 128K (K-token convention)


def test_ctx_shows_size_before_first_turn(slots):
    # Window size is known from config immediately — no "default", no % yet.
    st = StatusState(num_ctx=32768, has_turn=False)
    out = _flat(fields(ChatSession(), slots, "", st))
    assert "ctx 32K" in out and "%" not in out and "default" not in out


def test_slot_is_its_own_segment(slots):
    segs = fields(ChatSession(), slots, "", StatusState(slot="chat", model="Champ-9000"))
    seg_texts = [_flat([s]) for s in segs]
    assert "chat" in seg_texts and "Champ-9000" in seg_texts  # separate segments


def test_cache_shows_resident_prompt_size(slots):
    st = StatusState(prompt_tokens=92378, has_turn=True)
    out = _flat(fields(ChatSession(), slots, "", st))
    assert "cache " in out and "92k" in out


def test_colours_resolve_through_active_theme_fallback(slots, monkeypatch):
    # With no YASL theme available, the fallback (llmtop-equivalent ANSI) is used:
    # path=cyan, model=magenta, values=terminal default, all ANSI-named (no hex).
    from luxe.chat import theme as theme_mod
    monkeypatch.setattr(theme_mod, "_load_yasl_theme", lambda name: None)
    theme_mod.reset_cache()
    try:
        rs = theme_mod.role_styles(force=True)
        assert rs["pwd"][0] == "ansicyan" and rs["model"][0] == "ansimagenta"
        assert rs["white_brt"][1] == "default"
        assert all(not p.startswith("#") for p, _r in rs.values())
    finally:
        theme_mod.reset_cache()


def test_model_pinned_last(slots):
    segs = fields(ChatSession(), slots, "", StatusState(model="Qwen3.6-35B-A3B-6bit"))
    last = "".join(t for t, _p, _r in segs[-1].spans)
    assert last == "Qwen3.6-35B-A3B-6bit"  # model name alone, pinned last


def test_ctx_override_reflected_immediately(monkeypatch):
    # A /ctx override is shown as the effective window size right away (before any
    # turn), clamped to the slot's ceiling.
    from luxe.chat import slots as slots_module
    from luxe.config import PipelineConfig, RoleConfig

    class FakeBackend:
        def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
            self.model = model

        def unload_all_loaded(self, *, except_for=None):
            return {}

    monkeypatch.setattr(slots_module, "Backend", FakeBackend)
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith", num_ctx=32768,
                                      num_ctx_max=262144)},
    )
    sm = slots_module.SlotManager(cfg)
    out = _flat(fields(ChatSession(num_ctx_override=131072), sm, "", StatusState()))
    assert "128K" in out                      # override reflected as size
    # clamped to the ceiling when it exceeds it
    out2 = _flat(fields(ChatSession(num_ctx_override=999999), sm, "", StatusState()))
    assert "256K" in out2


def test_rate_not_in_status_bar(slots):
    # Per the user's spec the bar omits gen rate (it lives in the post-turn
    # footer). cache replaces it in the segment list.
    out = _flat(fields(ChatSession(), slots, "",
                       StatusState(tok_per_s=50.0, has_turn=True)))
    assert "tok/s" not in out


def test_timing_segments_when_opened(slots):
    out = _flat(fields(ChatSession(), slots, "", StatusState(opened_at=1_000_000.0)))
    assert "start " in out and "last " in out


def test_git_info_none_when_not_a_repo(tmp_path: Path):
    status_mod._git_cache.clear()
    assert git_info(str(tmp_path)) is None


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    env = {"HOME": str(tmp_path), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def g(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, env=env)

    g("init", "-b", "main")
    (repo / "a.txt").write_text("hi")
    g("add", "a.txt")
    g("commit", "-m", "init")
    return repo


def test_git_info_clean_then_dirty(tmp_path: Path):
    repo = _git_repo(tmp_path)
    status_mod._git_cache.clear()
    gi = git_info(str(repo))
    assert gi is not None and gi.branch == "main" and gi.clean and gi.state == "clean"

    (repo / "b.txt").write_text("new")        # untracked
    (repo / "a.txt").write_text("changed")    # modified
    status_mod._git_cache.clear()
    gi2 = git_info(str(repo))
    assert gi2.untracked == 1 and gi2.modified == 1
    assert gi2.state == "pending" and not gi2.clean


def test_git_segment_markers_render(tmp_path: Path, slots):
    repo = _git_repo(tmp_path)
    (repo / "b.txt").write_text("x")
    status_mod._git_cache.clear()
    out = _flat(fields(ChatSession(), slots, str(repo), StatusState()))
    assert "git" in out and "main" in out and "+1" in out


def test_git_segment_via_markup_monkeypatched(slots, monkeypatch):
    monkeypatch.setattr(status_mod, "git_info", lambda repo: status_mod.GitInfo(
        branch="feature/x", commit="abc123def", modified=2, ahead=1, has_upstream=True))
    out = status_markup(ChatSession(), slots, "/some/repo", StatusState())
    assert "feature/x" in out and "~2" in out and "↑1" in out


def _bar_len(segs) -> int:
    return sum(len("".join(t for t, _p, _r in s.spans)) for s in segs) + 3 * (len(segs) - 1)


def test_fit_drops_low_priority_first(slots):
    s = ChatSession(num_ctx_override=131072)
    st = StatusState(slot="chat", model="Qwen3.6-35B-A3B-6bit",
                     ctx_pressure=0.1, num_ctx=131072, prompt_tokens=9000,
                     has_turn=True, opened_at=1_000_000.0)
    full = status_mod.fields(s, slots, "/Users/x/Downloads/luxe", st)
    fitted = status_mod.fit(full, 55)
    txt = " · ".join("".join(t for t, _p, _r in seg.spans) for seg in fitted)
    # cache (priority 8) and start/last (7) drop before the protected ctx/model.
    assert "cache " not in txt and "start " not in txt
    assert "Qwen3.6-35B-A3B-6bit" in txt  # model protected, pinned last
    assert _bar_len(fitted) < _bar_len(full)  # fit actually shrank the bar


def test_fit_middle_ellipsis_path_when_still_over(slots):
    deep = "/Users/x/" + "/".join(f"segment{i}" for i in range(20))
    s = ChatSession()
    full = status_mod.fields(s, slots, deep, StatusState())
    fitted = status_mod.fit(full, 40)
    path_seg = next(seg for seg in fitted if seg.path)
    path_text = "".join(t for t, _p, _r in path_seg.spans)
    # Path is middle-ellipsised and much shorter than the original (the bar can't
    # go below the protected segments' minimum, which is expected best-effort).
    assert "…" in path_text and len(path_text) < len(deep)


def test_fit_keeps_everything_when_wide(slots):
    s = ChatSession()
    full = status_mod.fields(s, slots, "/r", StatusState())
    assert len(status_mod.fit(full, 500)) == len(full)


def test_live_activity_renders_spinner_and_elapsed(slots):
    from rich.console import Console
    import io
    act = status_mod.LiveActivity(ChatSession(write_enabled=True), slots, "",
                                  StatusState(slot="chat", model="m"), started_at=0.0)
    act.note(type("TC", (), {"name": "bash"})())
    out = io.StringIO()
    Console(file=out, width=200).print(act.__rich__())
    text = out.getvalue()
    assert "tools" in text and "bash" in text


# --- aborted-turn context reporting + the /ctx suggestion gate --------------
#
# Both land here because both are pure DISPLAY decisions about the two context
# numbers a finished turn carries. Driver:
# `acceptance/chat_bigread_2026_08_24/EVIDENCE.md`, findings 4 and 5 — a turn
# in session 168f1825a1fd that opened a 257,988-byte file and a 23,775-byte one
# in ONE step, died, and then rendered `ctx: 2% of 128K` directly above
# `context pressure 103%` followed by an offer of a bigger window.

from dataclasses import dataclass, field as _field

from luxe.chat.session import (
    CTX_SUGGEST_PRESSURE,
    aborted_ctx_line,
    ctx_suggestion,
    largest_tool_result_tokens,
    single_result_dominated,
)


@dataclass
class _Call:
    bytes_out: int = 0


@dataclass
class _Result:
    """The reporting surface of AgentResult that these two read."""
    aborted: bool = False
    abort_reason: str = ""
    last_prompt_tokens: int = 0
    peak_context_pressure: float = 0.0
    tool_calls: list = _field(default_factory=list)


K128 = 131072
K32 = 32768


class TestAbortedTurnContextLine:

    def test_both_numbers_carry_their_qualifier(self):
        """The exact shape of the 2026-08-24 footer: 3,148 accepted tokens of a
        128K window beside a 103% peak. Neither number was wrong; printed bare
        they read as a contradiction."""
        line = aborted_ctx_line(
            _Result(aborted=True, last_prompt_tokens=3148,
                    peak_context_pressure=1.03), K128)
        assert "last accepted 3.1k/128K" in line
        assert "attempted" in line and "estimated" in line
        assert "103%" in line
        # `peak x num_ctx` is the CALIBRATED token estimate of the step that
        # failed, not a raw chars/4 count: pressure is measured against
        # `calibrated_ctx_limit`, so the product lands back in server-truth
        # tokens. In session 168f1825a1fd that was 71,616 est x 1.88 ~ 135k
        # against a 128K window — which is precisely why it read 103%.
        assert "135.0k est" in line
        # Neither figure may appear naked — each is owned by its qualifier.
        assert line.index("last accepted") < line.index("3.1k")
        assert line.index("attempted") < line.index("103%")

    def test_a_turn_that_did_not_abort_gets_no_line(self):
        assert aborted_ctx_line(
            _Result(last_prompt_tokens=3148, peak_context_pressure=1.025),
            K128) is None

    def test_it_degrades_when_a_number_is_missing(self):
        only_peak = aborted_ctx_line(
            _Result(aborted=True, peak_context_pressure=0.9), K128)
        assert "last accepted" not in only_peak and "attempted" in only_peak
        assert aborted_ctx_line(_Result(aborted=True), K128) is None

    def test_no_window_means_no_fabricated_denominator(self):
        line = aborted_ctx_line(
            _Result(aborted=True, last_prompt_tokens=3148,
                    peak_context_pressure=1.025), 0)
        assert "/0K" not in line and "0 est" not in line
        assert "last accepted 3.1k" in line


class TestCtxSuggestionGate:
    """A display gate ONLY: `CTX_SUGGEST_PRESSURE` keeps its value, no
    compaction threshold moves, nothing dispatched changes."""

    def test_cumulative_growth_still_gets_the_suggestion(self):
        """The unchanged case — many modest results, real pressure, a window
        that is genuinely too small."""
        r = _Result(peak_context_pressure=0.9,
                    tool_calls=[_Call(bytes_out=4000) for _ in range(20)])
        assert ctx_suggestion(r, K32, K128) == ("large", 65536)

    def test_below_the_threshold_is_silent(self):
        assert CTX_SUGGEST_PRESSURE == 0.85          # unchanged by this work
        r = _Result(peak_context_pressure=0.84)
        assert ctx_suggestion(r, K32, K128) is None

    def test_one_oversized_tool_result_suppresses_it(self):
        """`self.md` at 257,988 B on a 128K window: ~64k tokens in ONE result,
        49% of the window. The next tier up buys that read more room to
        overflow — it is a read-budget problem, not a window problem."""
        r = _Result(aborted=True,
                    abort_reason="Backend error: OpenRouter stream failed",
                    peak_context_pressure=1.025,
                    tool_calls=[_Call(bytes_out=385),
                                _Call(bytes_out=257_988),
                                _Call(bytes_out=23_775)])
        assert single_result_dominated(r, K128) is True
        assert largest_tool_result_tokens(r) == 257_988 // 4
        assert ctx_suggestion(r, K128, 262144) is None

    def test_it_suppresses_on_a_non_window_abort_even_without_a_big_read(self):
        r = _Result(aborted=True, abort_reason="Max steps reached (12)",
                    peak_context_pressure=0.95)
        assert ctx_suggestion(r, K32, K128) is None

    def test_an_abort_the_server_blamed_on_the_window_still_suggests(self):
        """The counter-case, so the gate cannot swallow the one abort a bigger
        window really does fix."""
        r = _Result(aborted=True, peak_context_pressure=0.99,
                    abort_reason="Backend error: oMLX returned 400: Prompt too "
                                 "long: 45264 tokens exceeds max context "
                                 "window of 32768")
        assert ctx_suggestion(r, K32, K128) == ("large", 65536)

    def test_no_tier_left_is_silent(self):
        r = _Result(peak_context_pressure=0.95)
        assert ctx_suggestion(r, K128, K128) is None

    def test_a_tier_this_host_cannot_hold_is_not_recommended(self, monkeypatch):
        """`/ctx huge` typed by hand warns and proceeds — an explicit request is
        an instruction. An unprompted suggestion luxe already knows will run the
        box out of memory mid-session is just bad advice."""
        import luxe.chat.session as sess

        monkeypatch.setattr(sess, "host_ram_gb", lambda: 64.0)
        r = _Result(peak_context_pressure=0.95)
        assert ctx_suggestion(r, K128, 262144) is None
        # …and the same host reaches `huge` for weights it is not holding.
        assert ctx_suggestion(r, K128, 262144,
                              local_weights=False) == ("huge", 262144)
