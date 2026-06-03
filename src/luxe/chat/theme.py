"""Resolve the user's ACTIVE Claude statusline theme so luxe's chat status bar
follows it live, instead of a static port.

How: read the theme name (`CLAUDE_STATUSLINE_THEME` env → `~/.claude/statusline-theme`
→ `claude-dark`), then import the user's yet-another-statusline `themes` module
via the `~/.claude/statusline_command.py` symlink and call its `resolve(name)`.
Each YASL role is an ANSI escape string (`\\033[38;5;Nm` / `\\033[38;2;r;g;bm` /
`\\033[39m`); we convert it to a (prompt_toolkit, Rich) style pair. ANSI indices
0-15 become NAMED colours (so they track the terminal/iTerm2 profile, exactly as
llmtop intends); 16-255 become the fixed xterm value; rgb/default pass through.

Decoupled + safe: if the repo/symlink/import is unavailable, fall back to the
built-in llmtop-equivalent ANSI map. This module reads only the theme *name* file
and imports the user's (own, pure-dataclass) themes module for COLOURS — it is
NOT the memory subsystem and does not inject `~/.claude` content into context
(luxe.sdd/chat.sdd memory prohibition is about context/memory, not UI theming).
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path

# luxe status-bar roles → built-in fallback styles (llmtop's ANSI map):
# (prompt_toolkit style, Rich markup tag). Role names match YASL Theme attrs.
_FALLBACK: dict[str, tuple[str, str]] = {
    "pwd":       ("ansicyan", "cyan"),
    "branch":    ("ansigreen", "green"),
    "commit":    ("ansibrightblack", "bright_black"),
    "label":     ("ansibrightblack", "bright_black"),
    "ctx":       ("ansicyan", "cyan"),
    "dirty":     ("ansired", "red"),
    "model":     ("ansimagenta", "magenta"),
    "white_brt": ("", "default"),
    "safe":      ("ansigreen", "green"),
    "warn":      ("ansiyellow", "yellow"),
    "alert":     ("ansired", "red"),
    # luxe-only semantic roles (B4). Fallbacks are ANSI-named so they track the
    # terminal/iTerm profile even without a YASL theme; `_ALIAS` below points each
    # at a real YASL role so a custom theme drives them too. `slot` deliberately
    # does NOT alias `model` (magenta) — that's the purple dominance we're killing.
    "accent":    ("ansicyan", "cyan"),
    "success":   ("ansigreen", "green"),
    "error":     ("ansired", "red"),
    "info":      ("ansiblue", "blue"),
    "slot":      ("ansiblue", "blue"),
    "muted":     ("ansibrightblack", "bright_black"),
    "diff_add":  ("ansigreen", "green"),
    "diff_del":  ("ansired", "red"),
    "diff_hunk": ("ansicyan", "cyan"),
}
_ROLES = tuple(_FALLBACK)

# Original YASL Theme attrs (the only roles queried against the user's theme).
_YASL_ROLES = (
    "pwd", "branch", "commit", "label", "ctx", "dirty",
    "model", "white_brt", "safe", "warn", "alert",
)

# luxe-only roles inherit a real YASL role so a custom theme drives them. Roles
# absent here (info, slot) keep their ANSI-named fallback by design.
_ALIAS: dict[str, str] = {
    "accent":    "ctx",
    "success":   "safe",
    "error":     "alert",
    "muted":     "commit",
    "diff_add":  "safe",
    "diff_del":  "alert",
    "diff_hunk": "ctx",
}

# ANSI 0-15 → named styles (tracked by the terminal profile, NOT fixed hex).
_ANSI16_PTK = [
    "ansiblack", "ansired", "ansigreen", "ansiyellow", "ansiblue", "ansimagenta",
    "ansicyan", "ansigray", "ansibrightblack", "ansibrightred", "ansibrightgreen",
    "ansibrightyellow", "ansibrightblue", "ansibrightmagenta", "ansibrightcyan",
    "ansiwhite",
]
_ANSI16_RICH = [
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "bright_black", "bright_red", "bright_green", "bright_yellow", "bright_blue",
    "bright_magenta", "bright_cyan", "bright_white",
]


def _xterm_rgb(n: int) -> tuple[int, int, int]:
    """xterm 256-palette index (16-255) → RGB. (0-15 use named colours instead.)"""
    if n <= 231:
        n -= 16
        r, g, b = n // 36, (n // 6) % 6, n % 6
        f = lambda c: 0 if c == 0 else 55 + 40 * c
        return f(r), f(g), f(b)
    g = 8 + 10 * (n - 232)
    return g, g, g


def escape_to_styles(s: str) -> tuple[str, str]:
    """Convert a YASL role ANSI escape string to (ptk_style, rich_style)."""
    m = re.search(r"\x1b\[38;5;(\d+)m", s)
    if m:
        n = int(m.group(1))
        if n < 16:
            return _ANSI16_PTK[n], _ANSI16_RICH[n]
        r, g, b = _xterm_rgb(n)
        return f"#{r:02x}{g:02x}{b:02x}", f"color({n})"
    m = re.search(r"\x1b\[38;2;(\d+);(\d+);(\d+)m", s)
    if m:
        hexv = "#%02x%02x%02x" % tuple(int(x) for x in m.groups())
        return hexv, hexv
    if "\x1b[39m" in s:  # terminal default fg
        return "", "default"
    return "", ""


def resolve_theme_name() -> str:
    """Active theme name: env > ~/.claude/statusline-theme > 'claude-dark'."""
    env = os.environ.get("CLAUDE_STATUSLINE_THEME")
    if env and env.strip():
        return env.strip()
    try:
        name = (Path.home() / ".claude" / "statusline-theme").read_text().strip()
        return name or "claude-dark"
    except OSError:
        return "claude-dark"


def _load_yasl_theme(name: str):
    """Import the user's yet-another-statusline themes module (located via the
    ~/.claude/statusline_command.py symlink) and return its resolved Theme, or
    None if unavailable."""
    cmd = Path.home() / ".claude" / "statusline_command.py"
    try:
        pkg_parent = cmd.resolve(strict=True).parent  # <repo>/claude
    except OSError:
        return None
    if not (pkg_parent / "statusline" / "themes.py").is_file():
        return None
    sp = str(pkg_parent)
    added = sp not in sys.path
    if added:
        sys.path.insert(0, sp)
    try:
        mod = importlib.import_module("statusline.themes")
        return mod.resolve(name)
    except Exception:
        return None
    finally:
        if added:
            try:
                sys.path.remove(sp)
            except ValueError:
                pass


# Curated luxe palettes (C-T). Each value is a hex used for BOTH prompt_toolkit
# and Rich (both accept `#rrggbb`). Only the visually-salient roles are set;
# anything omitted inherits the ANSI fallback. These exist because the default
# "auto" mode tracks the terminal's ANSI slots — which, on a standard-index YASL
# theme (e.g. llmtop), equal luxe's fallback, so nothing changes. A curated
# palette bypasses ANSI to give luxe its own look regardless of terminal profile.
def _hexmap(d: dict[str, str]) -> dict[str, tuple[str, str]]:
    return {role: (h, h) for role, h in d.items()}


_PALETTES: dict[str, dict[str, tuple[str, str]]] = {
    # cool: teal / mint / slate — calm, low-saturation.
    "cool": _hexmap({
        "accent": "#5fd7d7", "pwd": "#5fd7d7", "ctx": "#5fd7d7", "diff_hunk": "#5fafd7",
        "success": "#87d7af", "safe": "#87d7af", "branch": "#87d7af", "diff_add": "#87d7af",
        "error": "#ff8787", "alert": "#ff8787", "diff_del": "#ff8787", "dirty": "#ff8787",
        "warn": "#ffd787", "info": "#5f87d7", "slot": "#8787d7", "model": "#5fafd7",
        "muted": "#767676", "commit": "#767676", "label": "#767676",
    }),
    # warm: amber / coral / sand.
    "warm": _hexmap({
        "accent": "#ffaf5f", "pwd": "#ffaf5f", "ctx": "#ffaf5f", "diff_hunk": "#d7af5f",
        "success": "#afd75f", "safe": "#afd75f", "branch": "#afd75f", "diff_add": "#afd75f",
        "error": "#ff5f5f", "alert": "#ff5f5f", "diff_del": "#ff8787", "dirty": "#ff8787",
        "warn": "#ffd75f", "info": "#d7af87", "slot": "#d78787", "model": "#ffaf87",
        "muted": "#8a8a8a", "commit": "#8a8a8a", "label": "#8a8a8a",
    }),
    # mono: high-contrast greys with a single cyan accent.
    "mono": _hexmap({
        "accent": "#00d7ff", "pwd": "#00d7ff", "ctx": "#00d7ff", "diff_hunk": "#00d7ff",
        "success": "#d0d0d0", "safe": "#d0d0d0", "branch": "#d0d0d0", "diff_add": "#87d787",
        "error": "#ff5f5f", "alert": "#ff5f5f", "diff_del": "#ff5f5f", "dirty": "#ff5f5f",
        "warn": "#d7d700", "info": "#afafaf", "slot": "#afafaf", "model": "#ffffff",
        "muted": "#767676", "commit": "#767676", "label": "#767676",
    }),
}

_active_palette: str | None = None  # set by set_palette(); None = "auto"


def set_palette(name: str | None) -> None:
    """Select a curated luxe palette by name ('auto'/'' = track terminal theme).
    Unknown names degrade to auto. Clears the resolved-style cache."""
    global _active_palette
    n = (name or "").strip().lower()
    _active_palette = None if n in ("", "auto") else n
    reset_cache()


def list_palettes() -> list[str]:
    return ["auto", *_PALETTES]


_cache: dict[str, tuple[str, str]] | None = None


def role_styles(*, force: bool = False) -> dict[str, tuple[str, str]]:
    """luxe role -> (ptk_style, rich_style) for the ACTIVE theme. Cached (theme
    doesn't change mid-session); `force=True` re-reads. A curated palette (C-T)
    overrides; otherwise tracks the YASL/terminal theme with per-role fallback."""
    global _cache
    if _cache is not None and not force:
        return _cache
    styles = dict(_FALLBACK)
    # luxe-only roles inherit their aliased base role first (so even a curated
    # palette that omits a role still gets a sensible value).
    for new_role, src in _ALIAS.items():
        if src in styles:
            styles[new_role] = styles[src]

    palette = _PALETTES.get(_active_palette) if _active_palette else None
    if palette is not None:
        styles.update(palette)  # curated colors win; no YASL/ANSI tracking
        _cache = styles
        return styles

    # auto: track the user's YASL theme (falling back to ANSI-named roles).
    theme = _load_yasl_theme(resolve_theme_name())
    if theme is not None:
        for role in _YASL_ROLES:
            val = getattr(theme, role, None)
            if isinstance(val, str) and val:
                styles[role] = escape_to_styles(val)
        for new_role, src in _ALIAS.items():
            themed = getattr(theme, new_role, None)
            if isinstance(themed, str) and themed:
                styles[new_role] = escape_to_styles(themed)
            elif src in styles:
                styles[new_role] = styles[src]
    _cache = styles
    return styles


def reset_cache() -> None:
    global _cache
    _cache = None


def styles_for(role: str) -> tuple[str, str]:
    """(ptk_style, rich_style) for a luxe role under the active theme."""
    return role_styles().get(role, ("", ""))


def rich(role: str) -> str:
    """Rich style string for a luxe role ('' = terminal default)."""
    return styles_for(role)[1]


def m(role: str, text: str) -> str:
    """Wrap `text` in the role's Rich style, or return it bare when the role
    resolves to the terminal default (avoids emitting an empty `[]` tag)."""
    style = rich(role)
    return f"[{style}]{text}[/]" if style else text
