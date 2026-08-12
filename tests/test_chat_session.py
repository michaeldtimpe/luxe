"""Tests for ChatSession context assembly + precedence ordering."""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.chat import session as session_mod
from luxe.chat.session import (
    CTX_TIER_MIN_RAM_GB,
    CTX_TIERS,
    ChatSession,
    ChatTurn,
    ctx_tier_ram_warning,
    host_ram_gb,
    next_tier_up,
    tier_label,
)
from luxe.memory import project as project_mem


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


def test_first_turn_no_memory_write_mode_is_empty_context(repo: Path):
    # Write mode ON + first turn + no memory → legacy byte-identical empty block.
    # `terse` is default-on (B2) and injects a <response_style> block, so the
    # empty-context invariant holds only with terse off — and, since 2026-08-05,
    # only outside a git project (write mode in a git repo injects
    # GIT_WORKFLOW_HINT, the one state where the model can actually run git).
    s = ChatSession(repo_path=str(repo), write_enabled=True, terse=False,
                    project_kind="dir")
    ctx, version = s.build_extra_context("what does foo do?")
    assert ctx == ""  # Goal carries the message; nothing else to disambiguate
    assert version == "trunc-v1"


def test_terse_default_injects_response_style(repo: Path):
    # B2: terse defaults on and injects a single high-precedence style block.
    s = ChatSession(repo_path=str(repo), write_enabled=True)
    assert s.terse is True
    ctx, _ = s.build_extra_context("what does foo do?")
    assert "<response_style>" in ctx
    assert "<current_request>" in ctx  # echo restored once anything precedes it


def test_read_only_injects_session_mode_hint_even_on_first_turn(repo: Path):
    # Default (read-only) first turn now carries the /write hint so the model
    # never reports luxe can't create/edit files.
    s = ChatSession(repo_path=str(repo))
    assert s.write_enabled is False
    ctx, _ = s.build_extra_context("scaffold a new project")
    assert "<session_mode>" in ctx
    assert "/write" in ctx
    assert "<current_request>" in ctx  # echo restored once anything precedes it


def test_write_mode_drops_the_read_only_hint(repo: Path):
    # Write mode replaces the read-only hint; in a NON-git project no other
    # mode hint applies, so the block disappears entirely.
    s = ChatSession(repo_path=str(repo), write_enabled=True, project_kind="dir")
    s.add_turn(ChatTurn(user="hi", assistant="hello"))
    ctx, _ = s.build_extra_context("now what?")
    assert "<session_mode>" not in ctx


def test_write_mode_in_a_git_repo_injects_the_git_workflow_hint(repo: Path):
    from luxe.agents.prompts import GIT_WORKFLOW_HINT, READ_ONLY_CHAT_HINT

    s = ChatSession(repo_path=str(repo), write_enabled=True)  # kind defaults git
    ctx, _ = s.build_extra_context("commit my changes")
    assert GIT_WORKFLOW_HINT in ctx
    assert READ_ONLY_CHAT_HINT not in ctx
    assert ctx.count("<session_mode>") == 1


def test_read_only_git_repo_gets_no_git_workflow_hint(repo: Path):
    # Read-only sessions cannot run git (bash is withheld) — the workflow
    # hint would be noise, and the read-only hint already owns the framing.
    from luxe.agents.prompts import GIT_WORKFLOW_HINT

    s = ChatSession(repo_path=str(repo))  # write off, kind defaults git
    ctx, _ = s.build_extra_context("commit my changes")
    assert GIT_WORKFLOW_HINT not in ctx


def test_session_mode_hint_is_lowest_precedence(repo: Path):
    (repo / ".luxe").mkdir()
    project_mem.repo_memory_file(repo).write_text("Pref: concise.\n")
    s = ChatSession(repo_path=str(repo))  # read-only
    s.add_turn(ChatTurn(user="earlier", assistant="answer"))
    ctx, _ = s.build_extra_context("current ask")
    i_mode = ctx.index("<session_mode>")
    i_mem = ctx.index("<project_memory>")
    i_hist = ctx.index("<conversation_history>")
    i_cur = ctx.index("<current_request>")
    assert i_mode < i_mem < i_hist < i_cur


def test_memory_only_injects_project_block_and_echo(repo: Path):
    (repo / ".luxe").mkdir()
    project_mem.repo_memory_file(repo).write_text("Use ruff.\n")
    s = ChatSession(repo_path=str(repo))
    ctx, _ = s.build_extra_context("add a test")
    assert "<project_memory>" in ctx
    assert "Use ruff." in ctx
    assert "<current_request>" in ctx
    assert "add a test" in ctx
    assert "<conversation_history>" not in ctx


def test_history_injects_conversation_block_and_echo(repo: Path):
    s = ChatSession(repo_path=str(repo))
    s.add_turn(ChatTurn(user="hi", assistant="hello"))
    ctx, _ = s.build_extra_context("now what?")
    assert "<conversation_history>" in ctx
    assert "[user] hi" in ctx
    assert "<current_request>" in ctx
    assert "now what?" in ctx


def test_precedence_order_memory_then_history_then_current(repo: Path):
    (repo / ".luxe").mkdir()
    project_mem.repo_memory_file(repo).write_text("Pref: concise.\n")
    s = ChatSession(repo_path=str(repo))
    s.add_turn(ChatTurn(user="earlier question", assistant="earlier answer"))
    ctx, _ = s.build_extra_context("current ask")
    i_mem = ctx.index("<project_memory>")
    i_hist = ctx.index("<conversation_history>")
    i_cur = ctx.index("<current_request>")
    assert i_mem < i_hist < i_cur  # documented precedence ordering
    # current request is the LAST-seen content
    assert ctx.rstrip().endswith("</current_request>")


def test_ctx_tier_label_exact_and_custom():
    assert tier_label(CTX_TIERS["medium"]) == "medium"
    assert tier_label(32768) == "medium"
    assert tier_label(40000) == "custom(40000)"


def test_next_tier_up_respects_ceiling():
    # From medium with a 128K ceiling → large is the next step up.
    assert next_tier_up(32768, 131072) == ("large", 65536)
    # From medium with only an 8K ceiling → nothing fits.
    assert next_tier_up(32768, 8192) is None
    # Already at the top tier.
    assert next_tier_up(131072, 131072) is None


class TestCtxTierRamWarning:
    """`/ctx huge` is inside `num_ctx_max` and past what most boxes hold.

    `num_ctx_max: 262144` is the MODEL's native limit (Qwen3.6's
    `max_position_embeddings`), so the existing clamp never fires for `huge` —
    it is the HOST that can't hold it. The KV cache is ~80 KiB/token here (40
    layers x 2 KV heads x 256 head_dim x 2 for K+V x 2 bytes, turboquant KV
    off), so a filled 256K window is ~20 GiB on top of 21-28 GB of weights:
    fine on a 128 GB box, past the 36 GB GPU cap on a 64 GB one. Measured
    2026-08-11."""

    def test_huge_warns_on_a_small_box(self, monkeypatch):
        monkeypatch.setattr(session_mod, "host_ram_gb", lambda: 64.0)
        warning = ctx_tier_ram_warning("huge")
        assert warning is not None
        assert "96+ GB" in warning
        assert "64 GB" in warning

    def test_huge_is_silent_on_a_big_box(self, monkeypatch):
        monkeypatch.setattr(session_mod, "host_ram_gb", lambda: 128.0)
        assert ctx_tier_ram_warning("huge") is None

    @pytest.mark.parametrize("tier", ["small", "medium", "large", "xlarge"])
    def test_the_other_tiers_never_warn(self, tier, monkeypatch):
        """Only `huge` carries a floor; xlarge is BFCL-proven on a 64 GB box."""
        monkeypatch.setattr(session_mod, "host_ram_gb", lambda: 8.0)
        assert ctx_tier_ram_warning(tier) is None

    def test_unknown_ram_does_not_warn(self, monkeypatch):
        """None means "couldn't tell", not "too small" — a warning nobody can
        act on is worse than none."""
        monkeypatch.setattr(session_mod, "host_ram_gb", lambda: None)
        assert ctx_tier_ram_warning("huge") is None

    def test_the_warning_names_the_cache_cost(self, monkeypatch):
        """It has to say WHY, or the number reads as arbitrary."""
        monkeypatch.setattr(session_mod, "host_ram_gb", lambda: 64.0)
        warning = ctx_tier_ram_warning("huge")
        assert "80 KiB/token" in warning
        assert "20 GiB" in warning

    def test_it_says_the_failure_is_deferred(self, monkeypatch):
        """The window is a ceiling and MLX grows the KV cache lazily, so
        selecting the tier succeeds and the box dies later. Say so."""
        monkeypatch.setattr(session_mod, "host_ram_gb", lambda: 64.0)
        assert "mid-session" in ctx_tier_ram_warning("huge")

    def test_every_floor_names_a_real_tier(self):
        assert set(CTX_TIER_MIN_RAM_GB) <= set(CTX_TIERS)

    def test_host_ram_is_a_positive_number_or_none(self):
        got = host_ram_gb()
        assert got is None or got > 0


def test_num_ctx_override_defaults_off():
    assert ChatSession().num_ctx_override is None


def test_unpromoted_facts_do_not_leak_into_context(repo: Path):
    project_mem.add_fact(repo, "secret auto fact", confidence="auto")
    # terse off + non-git kind so the only thing that could appear is the
    # (forbidden) fact — see the empty-context test above.
    s = ChatSession(repo_path=str(repo), write_enabled=True, terse=False,
                    project_kind="dir")
    ctx, _ = s.build_extra_context("hello")
    assert "secret auto fact" not in ctx
    assert ctx == ""  # nothing injected
