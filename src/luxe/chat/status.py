"""Bottom-toolbar status bar for `luxe chat` — a port of the applicable segments
from the user's `yet-another-statusline` format (compact, Monaco-safe, ` · `
separated, model pinned last).

WHAT PORTED (applies to luxe): home-relative path · state-coloured git label
`git <branch>/<commit> +U ~M -D RN ↑a ↓b ✓` · `ctx N%` (zone-coloured) · gen rate
· `start <date time> · last <HH:MM>` session timing · `<slot>:<model>` last.
WHAT DIDN'T (Claude-Code-subscription specifics): 5h/7d plan quotas, cache-read
tokens, $ cost, thinking-effort pill. Plus luxe-specific WRITE/BASH/READ-ONLY
mode chips (chat.sdd requires write-mode visibility every turn).

LIGHTWEIGHT variant: a static bar pinned under the input line, refreshed from
`StatusState` BETWEEN turns (the tool tail-log streams above for live progress).
`fields()` is the single source of segment order/content; `toolbar()`
(prompt_toolkit) and `status_markup()` (plain-input fallback) both render from it.
Unlike YASL there is no width-responsive segment-dropping yet — prompt_toolkit
truncates the bar; the path is shown home-relative but not middle-ellipsised.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass

from luxe.chat.session import tier_label

# Colours follow the user's ACTIVE Claude statusline theme (resolved live by
# `chat/theme.py`): each span is styled by a theme ROLE (pwd/branch/commit/dirty/
# label/ctx/model/white_brt/safe/warn/alert), drawn from the terminal ANSI
# palette so luxe tracks the same iTerm2 profile as the Claude statusline.
from luxe.chat import theme as theme_mod

# A styled span: (text, ptk_style, rich_style).
Span = tuple[str, str, str]


def _sp(text: str, role: str = "") -> Span:
    """Span styled by the active theme's ROLE (see chat/theme.role_styles)."""
    if not role:
        return (text, "", "")
    ptk, rich = theme_mod.role_styles().get(role, ("", ""))
    return (text, ptk, rich)


def _sep_style() -> tuple[str, str]:
    """(ptk, rich) for the ` · ` separator — the theme's label/gray role."""
    return theme_mod.role_styles().get("label", ("", ""))


# Sparing luxe palette (ANSI-named so it tracks the terminal profile; values use
# the terminal default fg so they read on light OR dark backgrounds). Per the
# user's spec: path blue, model yellow, write-on yellow / bash-on red, everything
# else grey label + default-fg value. git keeps the theme's role colours.
_GREY = ("ansibrightblack", "bright_black")
_DEFAULT = ("", "default")           # "white" labels: terminal default fg (light-bg safe)
_CYAN = ("ansicyan", "cyan")         # path (ANSI cyan; the terminal's ANSI-blue slot renders orange)
_YELLOW = ("ansiyellow", "yellow")   # model name
_PURPLE = ("ansimagenta", "magenta")  # luxe mode / slot
_GREEN = ("ansigreen", "green")      # state ON
_RED = ("ansired", "red")            # state OFF


def _S(text: str, style: tuple[str, str]) -> Span:
    return (text, style[0], style[1])


def _human(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k" if n < 10_000 else f"{n / 1000:.0f}k"
    return f"{n / 1_000_000:.1f}m"


def _ctx_size(n: int) -> str:
    """Context window size in the K/M convention used for context windows
    (binary, K-tokens): 131072 → '128K', 1048576 → '1.0M', 8192 → '8K'."""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return str(n)


@dataclass
class Segment:
    """A status-bar segment: styled spans + responsive-fit metadata."""
    spans: list[Span]
    priority: int = 5    # higher → dropped sooner when the bar overflows
    path: bool = False   # the elastic path segment (middle-ellipsised, not dropped)


def _seg_text(seg: Segment) -> str:
    return "".join(t for t, _p, _r in seg.spans)


# ---------------------------------------------------------------- git ------

_GIT_TTL = 5.0
_git_cache: dict[str, tuple[float, "GitInfo | None"]] = {}


@dataclass
class GitInfo:
    branch: str = ""
    commit: str = ""
    untracked: int = 0
    modified: int = 0
    deleted: int = 0
    renamed: int = 0
    ahead: int = 0
    behind: int = 0
    has_upstream: bool = False
    detached: bool = False

    @property
    def clean(self) -> bool:
        return not (self.untracked or self.modified or self.deleted
                    or self.renamed or self.ahead or self.behind)

    @property
    def state(self) -> str:
        if self.detached or self.behind:
            return "drift"      # red
        if self.untracked or self.modified or self.deleted or self.renamed or self.ahead:
            return "pending"    # yellow
        return "clean"          # green


def _run_git(repo: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", repo, *args],
                              capture_output=True, text=True, timeout=2)
    except Exception:
        return None


def git_info(repo: str) -> GitInfo | None:
    """Rich git state for `repo`, or None if not a git repo. TTL-cached so a
    per-keystroke toolbar redraw doesn't spawn git each time. Mirrors the
    yet-another-statusline porcelain=v1 -b parse."""
    if not repo:
        return None
    now = time.monotonic()
    hit = _git_cache.get(repo)
    if hit and (now - hit[0]) < _GIT_TTL:
        return hit[1]

    res: GitInfo | None = None
    head = _run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if head is not None and head.returncode == 0:
        branch = head.stdout.strip()
        detached = branch == "HEAD"
        gi = GitInfo(branch=branch, detached=detached)
        sha = _run_git(repo, "rev-parse", "--short=9", "HEAD")
        if sha is not None and sha.returncode == 0:
            gi.commit = sha.stdout.strip()
        st = _run_git(repo, "status", "--porcelain=v1", "-b", "-z",
                      "--untracked-files=normal")
        if st is not None and st.returncode == 0:
            _parse_porcelain(st.stdout, gi)
        res = gi

    _git_cache[repo] = (now, res)
    return res


def _parse_porcelain(out: str, gi: GitInfo) -> None:
    entries = [e for e in out.split("\0") if e]
    i = 0
    if entries and entries[0].startswith("##"):
        header = entries[0]
        gi.has_upstream = "..." in header
        m = re.search(r"ahead (\d+)", header)
        gi.ahead = int(m.group(1)) if m else 0
        m = re.search(r"behind (\d+)", header)
        gi.behind = int(m.group(1)) if m else 0
        i = 1
    while i < len(entries):
        entry = entries[i]
        if len(entry) < 2:
            i += 1
            continue
        x, y = entry[0], entry[1]
        if x == "R" or y == "R":
            gi.renamed += 1
            i += 2  # rename consumes the original-name field
            continue
        if x == "?" and y == "?":
            gi.untracked += 1
        elif x == "A" or y == "A":
            gi.untracked += 1
        elif x == "D" or y == "D":
            gi.deleted += 1
        elif x == "M" or y == "M":
            gi.modified += 1
        i += 1


def _git_segment(repo: str) -> list[Span] | None:
    gi = git_info(repo)
    if gi is None or not gi.branch:
        return None
    label_role = {"drift": "alert", "pending": "warn", "clean": "safe"}[gi.state]
    # git label=state, branch, commit=gray, dirty markers +~-R, ahead=warn,
    # behind=alert, ✓=safe — each via the active theme's role.
    spans: list[Span] = [_sp("git", label_role), _sp(" ", ""), _sp(gi.branch, "branch")]
    if gi.commit:
        spans.append(_sp(f"/{gi.commit}", "commit"))
    if gi.untracked:
        spans.append(_sp(f" +{gi.untracked}", "dirty"))
    if gi.modified:
        spans.append(_sp(f" ~{gi.modified}", "dirty"))
    if gi.deleted:
        spans.append(_sp(f" -{gi.deleted}", "dirty"))
    if gi.renamed:
        spans.append(_sp(f" R{gi.renamed}", "dirty"))
    if gi.ahead:
        spans.append(_sp(f" ↑{gi.ahead}", "warn"))
    if gi.behind:
        spans.append(_sp(f" ↓{gi.behind}", "alert"))
    if gi.clean and gi.has_upstream and not gi.detached:
        spans.append(_sp(" ✓", "safe"))
    return spans


# -------------------------------------------------------------- state ------


@dataclass
class StatusState:
    """Mutable snapshot the REPL updates after each turn; toolbar reads it."""
    slot: str = "chat"
    model: str = ""
    wall_s: float = 0.0
    tok_per_s: float = 0.0
    ctx_pressure: float = 0.0
    num_ctx: int = 0        # effective context window of the last turn
    prompt_tokens: int = 0  # resident prompt size (shown as "cache")
    steps: int = 0
    has_turn: bool = False
    opened_at: float = 0.0  # session start (epoch); 0 = unknown


def _short_model(model: str) -> str:
    name = (model or "?").split("/")[-1]
    return name if len(name) <= 22 else name[:21] + "…"


def fields(session, slots, repo: str, state: StatusState) -> list[Segment]:
    """Ordered status segments. THE place to change the bar's format. Order (user
    spec): path · git · ctx · cache · start · last · write · bash · model.
    `priority` drives responsive drop order (higher = dropped first); path/git/
    ctx/model are protected (1-2). git keeps the active theme's role colours; the
    rest use the sparing luxe palette (path blue, model yellow, grey labels,
    default-fg values)."""
    segs: list[Segment] = []

    # home-relative path (blue; elastic: ellipsised before any segment drops)
    if repo:
        home = os.path.expanduser("~")
        shown = "~" + repo[len(home):] if home and repo.startswith(home) else repo
        segs.append(Segment([_S(shown, _CYAN)], priority=2, path=True))

    # git (theme-coloured) — slots in after path when inside a repo
    git_seg = _git_segment(repo)
    if git_seg:
        segs.append(Segment(git_seg, priority=1))

    # ctx: `ctx N% <window-size>` (label = default fg; % used; size in the K/M
    # context convention). Before the first turn, show the configured tier.
    ctx_spans: list[Span] = [_S("ctx ", _DEFAULT)]
    if state.num_ctx:
        # window size is known from config immediately; the % appears once a
        # turn has measured usage.
        if state.has_turn:
            ctx_spans.append(_S(f"{state.ctx_pressure:.0%} ", _DEFAULT))
        ctx_spans.append(_S(_ctx_size(state.num_ctx), _GREY))
    else:
        tier = tier_label(session.num_ctx_override) if session.num_ctx_override else "default"
        ctx_spans.append(_S(tier, _GREY))
    segs.append(Segment(ctx_spans, priority=2))

    # cache: resident prompt size (luxe has no cross-turn prompt cache — each turn
    # is a fresh run_single; this is the prompt/KV size processed last turn)
    if state.has_turn:
        segs.append(Segment([_S("cache ", _GREY), _S(_human(state.prompt_tokens), _DEFAULT)],
                            priority=8))

    # start / last (separate segments, droppable) ------------------------
    if state.opened_at:
        started = time.strftime("%H:%M", time.localtime(state.opened_at))
        segs.append(Segment([_S("start ", _GREY), _S(started, _DEFAULT)], priority=7))
    now = time.strftime("%H:%M", time.localtime())
    segs.append(Segment([_S("last ", _GREY), _S(now, _DEFAULT)], priority=7))

    # write on/off · bash on/off — label default fg, state ON=green / OFF=red
    segs.append(Segment([_S("write ", _DEFAULT),
                         _S("on" if session.write_enabled else "off",
                            _GREEN if session.write_enabled else _RED)], priority=3))
    segs.append(Segment([_S("bash ", _DEFAULT),
                         _S("on" if session.unrestricted_bash else "off",
                            _GREEN if session.unrestricted_bash else _RED)], priority=3))

    # luxe mode (slot) as its own segment in purple, then the model name (yellow)
    segs.append(Segment([_S(state.slot, _PURPLE)], priority=2))
    model = state.model or slots.model_for("chat")
    segs.append(Segment([_S(_short_model(model), _YELLOW)], priority=1))

    return segs


# ------------------------------------------------------- responsive fit ----

_SEP_LEN = 3  # len(" · ")


def _middle_ellipsis(s: str, maxlen: int) -> str:
    if maxlen <= 1 or len(s) <= maxlen:
        return s if len(s) <= maxlen else s[: max(0, maxlen - 1)] + "…"
    keep = maxlen - 1
    left = keep // 2
    right = keep - left
    return s[:left] + "…" + (s[-right:] if right else "")


def fit(segments: list[Segment], width: int) -> list[Segment]:
    """Drop lowest-value segments (highest priority first) until the bar fits
    `width`; then middle-ellipsis the path. git/ctx/model (priority 1–2) survive;
    the path shrinks rather than dropping. Mirrors yet-another-statusline."""
    if width <= 0:
        return segments

    def total(segs: list[Segment]) -> int:
        if not segs:
            return 0
        return sum(len(_seg_text(s)) for s in segs) + _SEP_LEN * (len(segs) - 1)

    segs = list(segments)
    while total(segs) > width:
        droppable = [s for s in segs if s.priority >= 4 and not s.path]
        if not droppable:
            break
        segs.remove(max(droppable, key=lambda s: s.priority))

    if total(segs) > width:
        path_seg = next((s for s in segs if s.path), None)
        if path_seg and path_seg.spans:
            over = total(segs) - width
            text = _seg_text(path_seg)
            new = _middle_ellipsis(text, max(8, len(text) - over))
            t, p, r = path_seg.spans[0]
            path_seg.spans = [(new, p, r)]
    return segs


def _term_width(default: int = 100) -> int:
    import shutil
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


# ------------------------------------------------------------- render ------


def toolbar(session, slots, repo: str, state: StatusState, width: int | None = None):
    """prompt_toolkit bottom_toolbar value (FormattedText), fitted to width."""
    from prompt_toolkit.formatted_text import FormattedText

    w = width if width is not None else _term_width() - 1
    sep_ptk = _sep_style()[0]
    parts: list[tuple[str, str]] = []
    for i, seg in enumerate(fit(fields(session, slots, repo, state), w)):
        if i:
            parts.append((sep_ptk, " · "))
        parts += [(style, text) for (text, style, _rich) in seg.spans]
    return FormattedText(parts)


def status_markup(session, slots, repo: str, state: StatusState,
                  width: int | None = None) -> str:
    """Rich-markup one-liner for the plain-input fallback (no prompt_toolkit)."""
    w = width if width is not None else _term_width()
    out_segs: list[str] = []
    for seg in fit(fields(session, slots, repo, state), w):
        chunk = "".join(f"[{rich}]{text}[/]" if rich else text
                        for (text, _ptk, rich) in seg.spans)
        out_segs.append(chunk)
    sep_rich = _sep_style()[1] or "default"
    sep = f"[{sep_rich}]·[/]"
    return f"{sep} " + f" {sep} ".join(out_segs)


def to_rich_text(segments: list[Segment]):
    """Render fitted segments as a Rich Text (for the live in-turn status bar)."""
    from rich.text import Text

    t = Text()
    sep_rich = _sep_style()[1] or None
    for i, seg in enumerate(segments):
        if i:
            t.append(" · ", style=sep_rich)
        for text, _ptk, rich in seg.spans:
            t.append(text, style=(rich or None))
    return t


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class LiveActivity:
    """Rich renderable for the in-turn live view — THREE stacked lines so a long
    generation isn't opaque:
      1. a live preview of the text the model is currently streaming (so you can
         see it "thinking"/writing during the gaps between tool calls);
      2. the activity line (spinner · elapsed · tool count · current tool);
      3. the status bar.
    Re-renders under rich.Live auto-refresh so the clock/spinner/preview animate.
    Tool-call lines scroll above this region via `live.console.print`."""

    def __init__(self, session, slots, repo: str, state: StatusState, started_at: float):
        self.session = session
        self.slots = slots
        self.repo = repo
        self.state = state
        self.started_at = started_at
        self.tools = 0
        self.last_tool = ""
        self._stream = ""  # text streamed since the last tool dispatch

    def note(self, tc) -> None:
        self.tools += 1
        self.last_tool = getattr(tc, "name", "") or ""
        self._stream = ""  # a tool call ends the current generation chunk

    def on_token(self, delta: str) -> None:
        # Keep a bounded rolling buffer of the live generation.
        self._stream = (self._stream + delta)[-2000:]

    def __rich__(self):
        from rich.console import Group
        from rich.text import Text

        width = _term_width()
        elapsed = max(0.0, time.time() - self.started_at)
        frame = _SPINNER[int(elapsed * 10) % len(_SPINNER)]

        lines = []
        # 1. live streaming preview (last non-blank line, truncated to width)
        tail = next((ln for ln in reversed(self._stream.splitlines()) if ln.strip()), "")
        if tail:
            if len(tail) > width - 2:
                tail = "…" + tail[-(width - 3):]
            lines.append(Text(tail, style="bright_black"))
        # 2. activity line
        act = Text()
        act.append(f"{frame} ", style="cyan")
        act.append(f"{elapsed:5.1f}s ", style="bold")
        act.append(f"·{self.tools} tools", style="bright_black")
        if self.last_tool:
            act.append(f" · {self.last_tool}", style="yellow")
        lines.append(act)
        # 3. status bar
        lines.append(to_rich_text(fit(fields(self.session, self.slots, self.repo,
                                             self.state), width)))
        return Group(*lines)
