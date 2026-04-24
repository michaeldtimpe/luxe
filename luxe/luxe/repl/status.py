"""Banner + context view + tools list. Status-bar-ish REPL furniture."""

from __future__ import annotations

import random
import re
import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from luxe import __version__
from luxe.backend import (
    context_length,
    estimate_kv_ram_gb,
    max_context_length,
    parameter_size,
    server_process_rss_gb,
)
from luxe.registry import LuxeConfig

console = Console()


def _git_short_hash() -> str:
    """Short HEAD hash for the luxe repo. Freshly queried each call."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1.0,
            cwd=Path(__file__).resolve().parent,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return ""


# Hash at import time — fixed for the life of this Python process, so
# it reflects the code actually running. The banner compares this to
# the CURRENT HEAD and surfaces drift as a "restart pending" hint so
# freshly-pulled changes don't silently stay stuck behind an old REPL.
_GIT_HASH_AT_START = _git_short_hash()
_GIT_HASH = _GIT_HASH_AT_START  # kept for backward-compat in any ext callers

_HEAD_CACHE: tuple[float, str] | None = None


def _current_head_hash() -> str:
    """Current git HEAD, cached for 5s to avoid spamming git on every
    banner render."""
    import time as _time
    global _HEAD_CACHE
    now = _time.monotonic()
    if _HEAD_CACHE is not None and now - _HEAD_CACHE[0] < 5.0:
        return _HEAD_CACHE[1]
    h = _git_short_hash()
    _HEAD_CACHE = (now, h)
    return h


def _show_context_info(state: "ReplState", cfg: LuxeConfig) -> None:
    """Render per-agent ctx + KV RAM estimate + server process RSS, plus
    overall system memory at the bottom."""
    agents = (
        [cfg.get(state.sticky_agent)]
        if state.sticky_agent
        else [a for a in cfg.agents if a.enabled and a.name != "router"]
    )

    t = Table(show_header=True, box=None, padding=(0, 2), header_style="dim")
    t.add_column("agent")
    t.add_column("model")
    t.add_column("ctx", justify="right")
    t.add_column("max ctx", justify="right")
    t.add_column("KV est.", justify="right")
    t.add_column("server RSS", justify="right")

    seen_endpoints: dict[str, float | None] = {}
    for a in agents:
        endpoint = a.endpoint or cfg.ollama_base_url
        ctx_now = context_length(a.model, endpoint)
        ctx_max = max_context_length(a.model, endpoint)
        kv_gb = estimate_kv_ram_gb(a.model, ctx_now, endpoint)
        if endpoint not in seen_endpoints:
            seen_endpoints[endpoint] = server_process_rss_gb(endpoint)
        rss_gb = seen_endpoints[endpoint]
        t.add_row(
            a.name,
            a.model,
            f"{ctx_now:,}",
            f"{ctx_max:,}" if ctx_max else "[dim]?[/dim]",
            f"{kv_gb:.1f} GB" if kv_gb else "[dim]—[/dim]",
            f"{rss_gb:.1f} GB" if rss_gb else "[dim]—[/dim]",
        )
    console.print(t)

    try:
        import psutil
        vm = psutil.virtual_memory()
        console.print(
            f"[dim]system RAM:[/dim] {vm.used / 1024**3:.1f} / {vm.total / 1024**3:.1f} GB used "
            f"[dim]· available[/dim] {vm.available / 1024**3:.1f} GB"
        )
    except ImportError:
        pass
    console.print(
        "[dim]KV cache est. is naive fp16 × (layers × kv_heads × head_dim × ctx). "
        "Sliding-window models (Gemma 3) use materially less.[/dim]"
    )


def _handle_tools_command(args: list[str]) -> None:
    """/tools, /tools show <name>, /tools remove <name>."""
    from luxe.tool_library import TOOLS_ROOT, list_tools
    sub = args[0] if args else ""

    if sub in ("", "list"):
        entries = list_tools()
        if not entries:
            console.print(
                "[dim]no saved tools yet — agents can call[/dim] "
                "[cyan]create_tool[/cyan] [dim]to save one[/dim]"
            )
            return
        t = Table(show_header=True, box=None, padding=(0, 2), header_style="dim")
        t.add_column("name")
        t.add_column("description")
        t.add_column("tags")
        for meta in entries:
            tags = ", ".join(meta.get("tags") or []) or "[dim]—[/dim]"
            desc = (meta.get("description") or "")[:80]
            t.add_row(f"[cyan]{meta['name']}[/cyan]", desc, tags)
        console.print(t)
        console.print(f"[dim]stored at[/dim] {TOOLS_ROOT}")
        return

    if sub == "show":
        if len(args) < 2:
            console.print("[yellow]usage:[/yellow] /tools show <name>")
            return
        name = args[1]
        path = TOOLS_ROOT / f"{name}.py"
        if not path.exists():
            console.print(f"[yellow]no tool named[/yellow] {name}")
            return
        console.print(f"[dim]{path}[/dim]")
        console.print(path.read_text())
        return

    if sub == "remove":
        if len(args) < 2:
            console.print("[yellow]usage:[/yellow] /tools remove <name>")
            return
        name = args[1]
        path = TOOLS_ROOT / f"{name}.py"
        if not path.exists():
            console.print(f"[yellow]no tool named[/yellow] {name}")
            return
        path.unlink()
        console.print(f"[dim]removed[/dim] [cyan]{name}[/cyan]")
        return

    console.print("[yellow]usage:[/yellow] /tools [show <name> | remove <name>]")


def _fmt_clock(iso: str) -> str:
    """Extract HH:MM:SS from an ISO-format timestamp (what _now() emits).
    Returns "" when the input is empty or unparseable — the caller
    decides whether to render the segment at all."""
    if not iso:
        return ""
    # _now() uses datetime.now().isoformat(timespec="seconds"), e.g.
    # "2026-04-24T09:00:37" — the time portion is always characters 11:19.
    if len(iso) >= 19 and iso[10] == "T":
        return iso[11:19]
    return ""


def _fmt_wall(seconds: float | int) -> str:
    """Render a wall-time duration readably. Under a minute stays in
    seconds (no trailing zeros past one decimal for <10s). Once we
    cross 60s it switches to m/h — "2m 05s", "1h 12m"."""
    s = float(seconds or 0)
    if s < 10:
        return f"{s:.1f}s"
    if s < 60:
        return f"{s:.0f}s"
    m, rem = divmod(int(s), 60)
    if m < 60:
        return f"{m}m {rem:02d}s"
    h, rem_m = divmod(m, 60)
    return f"{h}h {rem_m:02d}m"


def _home_collapsed(p: Path) -> str:
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def _status_banner(
    state: "ReplState", cfg: LuxeConfig, border_color: str = "cyan"
) -> Panel:
    """Claude Code-style status panel.

    Row 1 is a 2-column grid (left-aligned title, right-aligned version).
    Rows 2–3 are a 4-column grid (label/value × left/right) so long paths
    or model names still keep all four columns aligned. Both grids use
    expand=True inside a Panel with a computed width so their right
    edges line up with each other, giving the visual of three rows in
    a single box. A decorative tripled right edge is painted on by
    _print_status_banner; its outer two chars pick random palette colors
    per render to match the rainbow `.:.` title markers."""
    # Running hash is fixed for this process; current HEAD may have
    # moved if the user pulled new code. Surface that drift so "your
    # fix isn't running yet" is obvious at a glance.
    running = _GIT_HASH_AT_START or f"v{__version__}"
    head = _current_head_hash()
    if head and _GIT_HASH_AT_START and head != _GIT_HASH_AT_START:
        version = f"{running} [yellow](↻ {head} — restart)[/yellow]"
    else:
        version = running

    if state.sticky_agent:
        mode = f"{state.sticky_agent}"
        agent_cfg = cfg.get(state.sticky_agent)
        model = state.pending_model or agent_cfg.model
        endpoint = agent_cfg.endpoint or cfg.ollama_base_url
    else:
        mode = "router"
        router_cfg = cfg.get("router")
        model = state.pending_model or router_cfg.model
        endpoint = cfg.ollama_base_url

    params = state.param_override or parameter_size(model, endpoint)
    # "7.6B" reads as "7.68" in some fonts — add a space before B/M.
    params = re.sub(r"(\d)([BM])\b", r"\1 \2", params)
    folder = _home_collapsed(Path.cwd())

    # `.:.` markers randomize per render, same palette + no-adjacent
    # rule as the prompt arrows — keeps the banner lively without
    # looking monochrome.
    title = _random_rainbow_title()

    # Single 3-col grid so every row shares the same right-label column.
    # That guarantees `version:`, `params:`, `mode:` colons line up
    # vertically — a 2-grid / 4-col layout computed widths independently
    # and drifted the colons by 2–4 chars per row.
    #
    # Left column renders differently per row:
    #   Row 1 — the rainbow title (no label/value gutter).
    #   Rows 2–3 — "label:<pad>value" pre-formatted so the labels
    #   align on `:` and the values start at a consistent column.
    left_lbl_w = max(len("model:"), len("folder:"))  # 7

    def _left_label_value(label: str, value: str) -> str:
        # Pad the label to `left_lbl_w`, then one separator space, then
        # value. Padding is applied to the plain label text (not the
        # Rich markup) so widths stay accurate.
        pad = " " * (left_lbl_w - len(label))
        return f"[dim]{label}[/dim]{pad} {value}"

    grid = Table.grid(expand=False, padding=(0, 1))
    grid.add_column(justify="left",  no_wrap=True)  # title / label:value
    grid.add_column(justify="right", no_wrap=True)  # right label (colon-aligned)
    grid.add_column(justify="left",  no_wrap=True)  # right value
    grid.add_row(title,
                 "[dim]version:[/dim]", version)
    grid.add_row(_left_label_value("model:",  model),
                 "[dim]params:[/dim]",  params)
    grid.add_row(_left_label_value("folder:", folder),
                 "[dim]mode:[/dim]",    mode)

    return Panel(grid, border_style=border_color, expand=False)


def _hex_to_truecolor(hex_color: str) -> str:
    """#rrggbb → `\x1b[38;2;R;G;Bm` truecolor SGR. Used to paint the
    banner's doubled right edge without routing back through Rich."""
    h = hex_color.lstrip("#")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f"\x1b[38;2;{r};{g};{b}m"


def _print_status_banner(state: "ReplState", cfg: LuxeConfig) -> None:
    """Render the Panel and tack on a decorative tripled right edge.

    Panel's Box char-set is fixed at 1 char per position, so a tripled
    right border can't be expressed through Rich's usual APIs. We pick
    three palette colors per render — one per vertical column — and:
      - feed column 0 into the Panel's `border_style` so the whole box
        (all four edges) flips to that color this render,
      - emit the captured panel lines as-is,
      - append two more border chars per row, painting each column
        uniformly in colors 1 and 2.
    Result: ╮╮╮ / │││ / ╯╯╯ where each column reads as a single solid
    stripe and the three stripes rotate colors every refresh (like the
    `.:.` title markers, but column-wise instead of char-wise)."""
    from io import StringIO
    from rich.console import Console as _Cons

    # Three distinct colors with no adjacent repeats → neighboring
    # stripes always read as different hues.
    cols = _pick_no_adjacent_repeats(3)
    panel = _status_banner(state, cfg, border_color=cols[0])

    buf = StringIO()
    capture = _Cons(
        file=buf,
        force_terminal=True,
        color_system=(console.color_system or "truecolor"),
        width=console.width,
    )
    capture.print(panel)
    lines = buf.getvalue().rstrip("\n").split("\n")

    RESET = "\x1b[0m"
    c_mid = _hex_to_truecolor(cols[1])
    c_out = _hex_to_truecolor(cols[2])

    decorated: list[str] = []
    for i, ln in enumerate(lines):
        if i == 0:
            extra = "╮"
        elif i == len(lines) - 1:
            extra = "╯"
        else:
            extra = "│"
        decorated.append(f"{ln}{c_mid}{extra}{RESET}{c_out}{extra}{RESET}")
    import sys
    sys.stdout.write("\n".join(decorated) + "\n")
    sys.stdout.flush()


_PROMPT_ARROW_PALETTE = [
    "#ff5c5c",  # red
    "#ffa040",  # orange
    "#ffdd33",  # yellow
    "#66d9ff",  # light blue
    "#66e066",  # green
    "#c38bff",  # violet
]


def _pick_no_adjacent_repeats(n: int) -> list[str]:
    """Pick `n` colors from the palette with no two neighbors equal.
    Shared between the prompt arrows and the banner's .:. markers."""
    picks: list[str] = []
    for _ in range(n):
        pool = [c for c in _PROMPT_ARROW_PALETTE if not picks or c != picks[-1]]
        picks.append(random.choice(pool))
    return picks


def _random_rainbow_title() -> str:
    """Render `.:. luxe .:.` with each of the 6 punctuation chars picking
    an independent palette color (no adjacent duplicates). `luxe` stays
    white."""
    colors = _pick_no_adjacent_repeats(6)
    marker = ('.', ':', '.')
    left = "".join(f"[{colors[i]}]{c}[/]" for i, c in enumerate(marker))
    right = "".join(f"[{colors[i + 3]}]{c}[/]" for i, c in enumerate(marker))
    return f"{left} [bold white]luxe[/] {right}"
