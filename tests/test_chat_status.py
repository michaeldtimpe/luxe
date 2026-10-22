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
    # The ceiling rides on the state (seeded at startup, refreshed per turn /
    # `/ctx` / `/backend`) — a render never asks the endpoint for it.
    state = StatusState(ctx_ceiling=sm.ctx_ceiling("chat"))
    out = _flat(fields(ChatSession(num_ctx_override=131072), sm, "", state))
    assert "128K" in out                      # override reflected as size
    # clamped to the ceiling when it exceeds it
    out2 = _flat(fields(ChatSession(num_ctx_override=999999), sm, "", state))
    assert "256K" in out2


def test_status_render_never_asks_for_the_ctx_ceiling(slots, monkeypatch):
    """Finding 9: `fields()` called `slots.ctx_ceiling`, which on a billable
    endpoint GETs `/v1/models` (30s connect timeout) — from the TUI's 10 Hz
    render. The bar now reads the cached `StatusState.ctx_ceiling`."""
    def _boom(*a, **k):
        raise AssertionError("render path asked for the ctx ceiling")

    monkeypatch.setattr(slots, "ctx_ceiling", _boom)
    out = _flat(fields(ChatSession(num_ctx_override=999999), slots, "",
                       StatusState(ctx_ceiling=131072)))
    assert "128K" in out
    # Unknown ceiling: shown unclamped rather than fetched.
    out = _flat(fields(ChatSession(num_ctx_override=65536), slots, "",
                       StatusState()))
    assert "64K" in out


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


def test_activity_line_is_one_builder_for_both_front_ends():
    """`LiveActivity` (line REPL) and the TUI's `_tick` both draw
    `activity_line`: spinner · elapsed · parts · thinking Ns · suffix."""
    line = status_mod.activity_line(
        100.0, [("read_file (2)", "bright_black"), ("", "yellow")],
        reasoning_since=104.0, suffix="esc to interrupt", now=110.0).plain
    assert " 10.0s · read_file (2) · thinking 6s · esc to interrupt" in line
    assert " ·  · " not in line                   # an empty part is skipped
    assert "thinking" not in status_mod.activity_line(100.0, now=101.0).plain


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


# --- fields() characterization ------------------------------------------------
#
# Exact-output pins for every branch of `fields()`: each segment's spans (text +
# both styles), drop priority and path flag, in order. Theme roles render as
# `p:<role>`/`r:<role>` markers so a style swap between roles is caught, and the
# clock, git, and ephemeral flag are stubbed so the bar is deterministic.

_G = ("ansibrightblack", "bright_black")   # status._GREY
_D = ("", "default")                         # status._DEFAULT


def _R(role: str) -> tuple[str, str]:
    return (f"p:{role}", f"r:{role}")


def _sp_(text: str, style: tuple[str, str]) -> tuple[str, str, str]:
    return (text, style[0], style[1])


class _RoleMap(dict):
    def get(self, key, default=None):
        return _R(key)


class _FakeTime:
    """`localtime()` → NOW, `localtime(t)` → @t; `strftime` passes it through."""

    @staticmethod
    def localtime(*a):
        return f"@{int(a[0])}" if a else "NOW"

    @staticmethod
    def strftime(fmt, t):
        assert fmt == "%H:%M"
        return t

    monotonic = staticmethod(__import__("time").monotonic)


class _Cfg:
    def __init__(self, n=1, boom=False):
        self.n, self.boom = n, boom

    def backend_entries(self):
        if self.boom:
            raise RuntimeError("no config")
        return list(range(self.n))


class _Slots:
    def __init__(self, *, backends=1, boom=False, billable=False, budget=None,
                 backend_name="local", billable_boom=False):
        self.cfg = _Cfg(backends, boom)
        if backend_name is not None:
            self.backend_name = backend_name
        self._billable, self._budget = billable, budget
        self._billable_boom = billable_boom

    def model_for(self, slot):
        return f"cfg-model-for-{slot}"

    def is_billable(self):
        if self._billable_boom:
            raise RuntimeError("catalog down")
        return self._billable

    def active_entry(self):
        from types import SimpleNamespace
        return SimpleNamespace(budget_usd=self._budget)


@pytest.fixture
def pinned(monkeypatch):
    """Deterministic theme/clock/git/ephemeral; returns the git_info call log."""
    from luxe.chat import theme as theme_mod

    rm = _RoleMap()
    monkeypatch.setattr(theme_mod, "role_styles", lambda *a, **k: rm)
    monkeypatch.setattr(status_mod, "time", _FakeTime)
    monkeypatch.setattr(status_mod._ephemeral, "is_ephemeral", lambda: False)
    calls: list = []
    state = {"gi": None}

    def fake_git_info(repo, *, background=False, on_update=None):
        calls.append((repo, background, on_update))
        return state["gi"]

    monkeypatch.setattr(status_mod, "git_info", fake_git_info)
    return {"calls": calls, "git": state}


def _dump(segs):
    return [(s.priority, s.path, list(s.spans)) for s in segs]


def _tail(model="m", slot="chat"):
    """The always-present trailing segments at defaults (no backend/mode)."""
    return [
        (3, False, [_sp_("write ", _D), _sp_("off", _R("muted"))]),
        (3, False, [_sp_("bash ", _D), _sp_("off", _R("muted"))]),
        (3, False, [_sp_("web ", _D), _sp_("off", _R("muted"))]),
        (2, False, [_sp_(slot, _R("slot"))]),
        (1, False, [_sp_(model, _R("model"))]),
    ]


def _ctx(*spans):
    return (2, False, [_sp_("ctx ", _D), *spans])


_LAST = (7, False, [_sp_("last ", _G), _sp_("NOW", _D)])


def _seg(segs, label):
    hits = [d for d in _dump(segs) if d[2] and d[2][0][0] == label]
    assert len(hits) <= 1
    return hits[0] if hits else None


class TestFieldsCharacterization:
    def test_minimal_bar_exact(self, pinned):
        segs = fields(ChatSession(), _Slots(), "", StatusState(model="m"))
        assert _dump(segs) == [_ctx(_sp_("default", _G)), _LAST, *_tail()]
        # no repo, no on_git_update → foreground git lookup
        assert pinned["calls"] == [("", False, None)]

    def test_everything_on_exact(self, pinned, monkeypatch):
        from luxe.chat.session import FRONTIER_MAX_TOKENS, FRONTIER_STEPS

        monkeypatch.setattr(status_mod._ephemeral, "is_ephemeral", lambda: True)
        pinned["git"]["gi"] = status_mod.GitInfo(
            branch="main", commit="abc123def", untracked=1, modified=2, deleted=3,
            renamed=4, ahead=5, behind=6, has_upstream=True)
        session = ChatSession(
            write_enabled=True, unrestricted_bash=True, web_enabled=True,
            num_ctx_override=131072, max_steps_override=FRONTIER_STEPS,
            max_tokens_override=FRONTIER_MAX_TOKENS, loop_guard=False,
            verbose_level="full", show_reasoning=True, compact=True, terse=False,
            session_cost_usd=0.9)
        state = StatusState(
            slot="code", model="org/" + "X" * 30, has_turn=True, ctx_pressure=0.42,
            num_ctx=32768, ctx_ceiling=65536, prompt_tokens=92378, cache_hit=0.98,
            prefill_tps=3400.0, decode_tps=45.0, opened_at=1_000_000.0,
            model_origin="network")
        upd = lambda: None  # noqa: E731
        segs = fields(session, _Slots(backends=2, billable=True, budget=1.0,
                                      backend_name="m5"),
                      "/r", state, on_git_update=upd)
        assert pinned["calls"] == [("/r", True, upd)]
        on = _R("success")
        assert _dump(segs) == [
            (2, True, [_sp_("/r", _R("pwd"))]),
            (1, False, [_sp_("git", _R("alert")), _sp_(" ", ("", "")),
                        _sp_("main", _R("branch")), _sp_("/abc123def", _R("commit")),
                        _sp_(" +1", _R("dirty")), _sp_(" ~2", _R("dirty")),
                        _sp_(" -3", _R("dirty")), _sp_(" R4", _R("dirty")),
                        _sp_(" ↑5", _R("warn")), _sp_(" ↓6", _R("alert"))]),
            _ctx(_sp_("42% ", _D), _sp_("64K", _G)),
            (8, False, [_sp_("cache ", _G), _sp_("98% ", _D), _sp_("92k", _G)]),
            (6, False, [_sp_("pp ", _G), _sp_("3.4k", _D), _sp_(" ", _G),
                        _sp_("tg ", _G), _sp_("45", _D), _sp_(" t/s", _G)]),
            (5, False, [_sp_("$0.900/$1.00", _R("warn"))]),
            (7, False, [_sp_("start ", _G), _sp_("@1000000", _D)]),
            _LAST,
            (3, False, [_sp_("write ", _D), _sp_("on", on)]),
            (3, False, [_sp_("bash ", _D), _sp_("on", on)]),
            (3, False, [_sp_("web ", _D), _sp_("on", on)]),
            (1, False, [_sp_("eph ", _D), _sp_("on", _R("warning"))]),
            (3, False, [_sp_("frontier ", _D),
                        _sp_("steps 500 tok 16k loopguard off", _R("warning"))]),
            (5, False, [_sp_("backend ", _G), _sp_("m5", _D)]),
            (4, False, [_sp_("mode ", _D),
                        _sp_("verbose:full reason compact terse:off", _R("info"))]),
            (2, False, [_sp_("code", _R("slot"))]),
            (1, False, [_sp_("☁ ", _R("warn")), _sp_("X" * 21 + "…", _R("model"))]),
        ]

    # --- path / git ---------------------------------------------------------

    def test_background_git_explicit_overrides_callback(self, pinned):
        upd = lambda: None  # noqa: E731
        fields(ChatSession(), _Slots(), "/r", StatusState(), on_git_update=upd,
               background_git=False)
        fields(ChatSession(), _Slots(), "/r", StatusState(), background_git=True)
        # a foreground lookup never carries the callback (nothing to repaint)
        assert pinned["calls"] == [("/r", False, None), ("/r", True, None)]

    def test_no_project_chip_only_when_detached_from_any_project(self, pinned):
        segs = fields(ChatSession(project_kind="none"), _Slots(), "",
                      StatusState(model="m"))
        assert _dump(segs)[0] == (2, False, [_sp_("no project", _G)])
        segs = fields(ChatSession(project_kind="dir"), _Slots(), "",
                      StatusState(model="m"))
        assert _seg(segs, "no project") is None

    def test_git_without_branch_is_no_segment(self, pinned):
        pinned["git"]["gi"] = status_mod.GitInfo(branch="", commit="abc")
        segs = fields(ChatSession(project_kind="none"), _Slots(), "/r",
                      StatusState(model="m"))
        assert [d[2][0][0] for d in _dump(segs)[:2]] == ["/r", "no project"]

    def test_clean_tracked_git_gets_a_check(self, pinned):
        pinned["git"]["gi"] = status_mod.GitInfo(branch="main", has_upstream=True)
        segs = fields(ChatSession(), _Slots(), "/r", StatusState(model="m"))
        assert _dump(segs)[1] == (1, False, [
            _sp_("git", _R("safe")), _sp_(" ", ("", "")), _sp_("main", _R("branch")),
            _sp_(" ✓", _R("safe"))])

    def test_pending_git_without_upstream(self, pinned):
        pinned["git"]["gi"] = status_mod.GitInfo(branch="b", commit="c", ahead=1)
        segs = fields(ChatSession(), _Slots(), "/r", StatusState(model="m"))
        assert _dump(segs)[1] == (1, False, [
            _sp_("git", _R("warn")), _sp_(" ", ("", "")), _sp_("b", _R("branch")),
            _sp_("/c", _R("commit")), _sp_(" ↑1", _R("warn"))])

    # --- ctx / cache / rates ------------------------------------------------

    @pytest.mark.parametrize("override,ceiling,num_ctx,has_turn,want", [
        (None, 0, 0, False, [_sp_("default", _G)]),
        (None, 0, 0, True, [_sp_("7% ", _D), _sp_("default", _G)]),
        (None, 0, 1000, False, [_sp_("1000", _G)]),
        (None, 0, 2 * 1024 * 1024, False, [_sp_("2.0M", _G)]),
        (65536, 0, 8192, False, [_sp_("64K", _G)]),          # unclamped
        (999999, 131072, 8192, False, [_sp_("128K", _G)]),   # clamped
        (16384, 131072, 8192, True, [_sp_("7% ", _D), _sp_("16K", _G)]),
    ])
    def test_ctx_segment(self, pinned, override, ceiling, num_ctx, has_turn, want):
        segs = fields(ChatSession(num_ctx_override=override), _Slots(), "",
                      StatusState(num_ctx=num_ctx, ctx_ceiling=ceiling,
                                  has_turn=has_turn, ctx_pressure=0.07))
        assert _dump(segs)[0] == _ctx(*want)

    @pytest.mark.parametrize("tokens,hit,want", [
        (999, None, [_sp_("cache ", _G), _sp_("999", _D)]),
        (9999, None, [_sp_("cache ", _G), _sp_("10.0k", _D)]),
        (2_500_000, 0.0, [_sp_("cache ", _G), _sp_("0% ", _D), _sp_("2.5m", _G)]),
    ])
    def test_cache_segment(self, pinned, tokens, hit, want):
        segs = fields(ChatSession(), _Slots(), "",
                      StatusState(has_turn=True, prompt_tokens=tokens, cache_hit=hit))
        assert _seg(segs, "cache ") == (8, False, want)

    def test_cache_and_rates_absent_before_first_turn(self, pinned):
        segs = fields(ChatSession(), _Slots(), "",
                      StatusState(prompt_tokens=5, cache_hit=0.5, prefill_tps=9.0,
                                  decode_tps=9.0, model="m"))
        assert _dump(segs) == [_ctx(_sp_("default", _G)), _LAST, *_tail()]

    @pytest.mark.parametrize("pp,tg,want", [
        (None, None, None),
        (0.0, 0.0, None),
        (12.0, None, [_sp_("pp ", _G), _sp_("12", _D), _sp_(" t/s", _G)]),
        (None, 1500.0, [_sp_("tg ", _G), _sp_("1.5k", _D), _sp_(" t/s", _G)]),
    ])
    def test_rate_segment(self, pinned, pp, tg, want):
        segs = fields(ChatSession(), _Slots(), "",
                      StatusState(has_turn=True, prefill_tps=pp, decode_tps=tg))
        rate = [d for d in _dump(segs) if d[0] == 6]
        assert rate == ([] if want is None else [(6, False, want)])

    # --- cost ---------------------------------------------------------------

    @pytest.mark.parametrize("slots_kw,sess_cost,state_cost,want", [
        ({}, 0.0, 0.0, None),                                     # local, nothing
        ({"billable": True}, 0.0, 0.0, [_sp_("$0.0000", _D)]),    # billable, uncapped
        ({}, 0.0, 0.25, [_sp_("$0.250", _D)]),                    # state fallback
        ({}, 2.5, 0.25, [_sp_("$2.50", _D)]),                     # session wins
        ({"billable": True, "budget": 10.0}, 7.99, 0.0,
         [_sp_("$7.99/$10.00", _D)]),                             # under warn line
        ({"billable": True, "budget": 10.0}, 8.0, 0.0,
         [_sp_("$8.00/$10.00", _R("warn"))]),                     # at warn line
        ({"billable_boom": True}, 1.0, 0.0, None),                # guarded
    ])
    def test_cost_segment(self, pinned, slots_kw, sess_cost, state_cost, want):
        segs = fields(ChatSession(session_cost_usd=sess_cost), _Slots(**slots_kw),
                      "", StatusState(session_cost_usd=state_cost, model="m"))
        got = [d for d in _dump(segs) if d[0] == 5]
        assert got == ([] if want is None else [(5, False, want)])

    def test_cost_sits_between_rates_and_start(self, pinned):
        segs = fields(ChatSession(session_cost_usd=0.5), _Slots(), "",
                      StatusState(has_turn=True, decode_tps=5.0, opened_at=60.0))
        labels = [d[2][0][0] for d in _dump(segs)]
        assert labels[:6] == ["ctx ", "cache ", "tg ", "$0.500", "start ", "last "]

    # --- toggles / chips ----------------------------------------------------

    def test_toggles_are_independent(self, pinned):
        segs = fields(ChatSession(write_enabled=False, unrestricted_bash=True,
                                  web_enabled=False), _Slots(), "", StatusState())
        assert _seg(segs, "write ")[2][1] == _sp_("off", _R("muted"))
        assert _seg(segs, "bash ")[2][1] == _sp_("on", _R("success"))
        assert _seg(segs, "web ")[2][1] == _sp_("off", _R("muted"))

    def test_session_without_web_attr_reads_off(self, pinned):
        from types import SimpleNamespace
        s = SimpleNamespace(num_ctx_override=None, write_enabled=False,
                            unrestricted_bash=False)
        segs = fields(s, _Slots(), "", StatusState(model="m"))
        assert _dump(segs) == [_ctx(_sp_("default", _G)), _LAST, *_tail()]

    @pytest.mark.parametrize("kw,label,text", [
        ({"max_steps_override": 40}, "limits ", "steps 40"),
        ({"max_tokens_override": 8192}, "limits ", "tok 8.2k"),
        ({"loop_guard": False}, "limits ", "loopguard off"),
        ({"max_steps_override": 40, "max_tokens_override": 300},
         "limits ", "steps 40 tok 300"),
        ({"max_steps_override": 500, "loop_guard": False},
         "limits ", "steps 500 loopguard off"),
    ])
    def test_limits_chip(self, pinned, kw, label, text):
        segs = fields(ChatSession(**kw), _Slots(), "", StatusState())
        assert _seg(segs, label) == (3, False, [_sp_(label, _D),
                                                _sp_(text, _R("warning"))])
        assert _seg(segs, "frontier ") is None

    @pytest.mark.parametrize("kw,text", [
        ({"verbose_level": "diff"}, "verbose:diff"),
        ({"verbose_level": ""}, None),
        ({"show_reasoning": True}, "reason"),
        ({"compact": True}, "compact"),
        ({"terse": False}, "terse:off"),
    ])
    def test_mode_chip(self, pinned, kw, text):
        segs = fields(ChatSession(**kw), _Slots(), "", StatusState())
        want = None if text is None else (4, False, [_sp_("mode ", _D),
                                                     _sp_(text, _R("info"))])
        assert _seg(segs, "mode ") == want

    @pytest.mark.parametrize("slots_kw,want", [
        ({"backends": 1}, None),
        ({"backends": 3, "backend_name": "m5"}, "m5"),
        ({"backends": 2, "backend_name": None}, "?"),
        ({"backends": 2, "boom": True}, None),
    ])
    def test_backend_chip(self, pinned, slots_kw, want):
        segs = fields(ChatSession(), _Slots(**slots_kw), "", StatusState())
        got = _seg(segs, "backend ")
        assert got == (None if want is None else
                       (5, False, [_sp_("backend ", _G), _sp_(want, _D)]))

    def test_chip_order_after_toggles(self, pinned, monkeypatch):
        monkeypatch.setattr(status_mod._ephemeral, "is_ephemeral", lambda: True)
        segs = fields(ChatSession(loop_guard=False, compact=True),
                      _Slots(backends=2), "", StatusState(model="m"))
        labels = [d[2][0][0] for d in _dump(segs)]
        assert labels[2:] == ["write ", "bash ", "web ", "eph ", "limits ",
                              "backend ", "mode ", "chat", "m"]

    # --- model --------------------------------------------------------------

    @pytest.mark.parametrize("model,origin,want", [
        ("", "unknown", [_sp_("cfg-model-for-chat", _R("model"))]),
        ("a/b/Short", "local", [_sp_("Short", _R("model"))]),
        ("Y" * 22, "remote", [_sp_("⇅ ", _R("warn")), _sp_("Y" * 22, _R("model"))]),
        ("Y" * 23, "bogus", [_sp_("Y" * 21 + "…", _R("model"))]),
    ])
    def test_model_segment(self, pinned, model, origin, want):
        # slot "code": the no-model fallback asks for the CHAT slot's model
        segs = fields(ChatSession(), _Slots(), "",
                      StatusState(slot="code", model=model, model_origin=origin))
        assert _dump(segs)[-2:] == [(2, False, [_sp_("code", _R("slot"))]),
                                    (1, False, want)]
