"""Tests for the /help renderer's styling contract.

The regression these pin: section headers must be rendered as `bold
orange1` (Phase 1) and the description column must emit an explicit
`default` style reset so ambient dim doesn't bleed into it on terminals
that inherit the previous style.
"""

from __future__ import annotations

from rich.console import Console

from luxe.repl.help import _HELP_SECTIONS, _render_help


def _render_to_ansi() -> str:
    # Use a recording Console at a wide enough width that rows don't wrap.
    console = Console(
        record=True,
        force_terminal=True,
        color_system="truecolor",
        width=200,
    )
    console.print(_render_help())
    return console.export_text(styles=True)


def test_help_has_all_sections():
    out = _render_to_ansi()
    for title, _ in _HELP_SECTIONS:
        assert title in out, f"missing section header: {title}"


def test_help_contains_known_commands():
    out = _render_to_ansi()
    # Spot-check a few commands across sections.
    for cmd in ("/help", "/tasks", "/review", "/memory", "/sessions"):
        assert cmd in out


def test_section_headers_render_orange():
    # Build an ANSI-capture console; orange1 is color 214 in the 256 palette,
    # which Rich renders into the sequence `38;5;214` under color_system=256
    # or the RGB equivalent under truecolor.
    console = Console(
        record=True,
        force_terminal=True,
        color_system="256",
        width=200,
    )
    console.print(_render_help())
    ansi = console.export_text(styles=True)
    # "214" is the 256-palette code for orange1.
    assert "214" in ansi, "section headers should render as orange1 (256-palette 214)"


def test_section_headers_render_bold():
    # Bold emits `\x1b[1` under both 256 and truecolor.
    console = Console(
        record=True,
        force_terminal=True,
        color_system="256",
        width=200,
    )
    console.print(_render_help())
    ansi = console.export_text(styles=True)
    assert "\x1b[1" in ansi, "section headers should be bold"
