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
        def __init__(self, base_url="", model=""):
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
    assert "write off" in out and "bash off" in out


def test_mode_on_when_enabled(slots):
    out = _flat(fields(ChatSession(write_enabled=True, unrestricted_bash=True),
                       slots, "", StatusState()))
    assert "write on" in out and "bash on" in out


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
        < pos("write ") < pos("bash ") < pos("chat") < pos("Champ-9000")


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


def test_ctx_tier_shown_when_overridden(slots):
    out = _flat(fields(ChatSession(num_ctx_override=131072), slots, "", StatusState()))
    assert "xlarge" in out


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
