"""Tests for chat/theme.py — resolving the user's active Claude statusline theme
and converting its ANSI-escape role colours to (prompt_toolkit, Rich) styles."""

from __future__ import annotations

from luxe.chat import theme


def test_escape_ansi_0_15_becomes_named_palette():
    # ANSI 0-15 → named colours so they track the terminal/iTerm2 profile.
    assert theme.escape_to_styles("\x1b[38;5;6m") == ("ansicyan", "cyan")
    assert theme.escape_to_styles("\x1b[38;5;8m") == ("ansibrightblack", "bright_black")
    assert theme.escape_to_styles("\x1b[38;5;2m") == ("ansigreen", "green")


def test_escape_extended_256_becomes_fixed():
    ptk, rich = theme.escape_to_styles("\x1b[38;5;114m")
    assert ptk.startswith("#") and rich == "color(114)"


def test_escape_rgb_and_default():
    assert theme.escape_to_styles("\x1b[38;2;180;100;50m") == ("#b46432", "#b46432")
    assert theme.escape_to_styles("\x1b[39m") == ("", "default")


def test_resolve_theme_name_env_wins(monkeypatch):
    monkeypatch.setenv("CLAUDE_STATUSLINE_THEME", "catppuccin-mocha")
    assert theme.resolve_theme_name() == "catppuccin-mocha"


def test_resolve_theme_name_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_STATUSLINE_THEME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "statusline-theme").write_text("llmtop\n")
    assert theme.resolve_theme_name() == "llmtop"


def test_resolve_theme_name_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_STATUSLINE_THEME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no .claude/statusline-theme
    assert theme.resolve_theme_name() == "claude-dark"


def test_role_styles_falls_back_without_yasl(monkeypatch):
    monkeypatch.setattr(theme, "_load_yasl_theme", lambda name: None)
    theme.reset_cache()
    try:
        rs = theme.role_styles(force=True)
        assert rs["pwd"] == ("ansicyan", "cyan")
        assert rs["alert"] == ("ansired", "red")
        assert rs["white_brt"] == ("", "default")
    finally:
        theme.reset_cache()


def test_role_styles_reads_resolved_theme(monkeypatch):
    class FakeTheme:
        pwd = "\x1b[38;5;6m"
        branch = "\x1b[38;5;2m"
        commit = "\x1b[38;5;8m"
        label = "\x1b[38;5;8m"
        ctx = "\x1b[38;5;6m"
        dirty = "\x1b[38;5;1m"
        model = "\x1b[38;5;5m"
        white_brt = "\x1b[39m"
        safe = "\x1b[38;5;2m"
        warn = "\x1b[38;5;3m"
        alert = "\x1b[38;5;1m"

    monkeypatch.setattr(theme, "_load_yasl_theme", lambda name: FakeTheme())
    theme.reset_cache()
    try:
        rs = theme.role_styles(force=True)
        assert rs["pwd"] == ("ansicyan", "cyan")
        assert rs["model"] == ("ansimagenta", "magenta")
        assert rs["dirty"] == ("ansired", "red")
        assert rs["white_brt"] == ("", "default")
    finally:
        theme.reset_cache()
