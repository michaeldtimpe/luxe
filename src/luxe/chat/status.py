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

# Semantic colour -> (prompt_toolkit style, Rich markup tag).
_C: dict[str, tuple[str, str]] = {
    "red": ("#ff6b6b", "red"),
    "yellow": ("#ffcc55", "yellow"),
    "green": ("#66e066", "green"),
    "orange": ("#ffa040", "dark_orange3"),
    "cyan": ("#66d9ff", "cyan"),
    "blue": ("#88aaff", "blue"),
    "dim": ("#888888", "dim"),
    "white": ("#ffffff", "white"),
    "": ("", ""),
}

# A styled span: (text, ptk_style, rich_style).
Span = tuple[str, str, str]


def _sp(text: str, colour: str = "") -> Span:
    p, r = _C.get(colour, ("", ""))
    return (text, p, r)


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
    label_clr = {"drift": "red", "pending": "yellow", "clean": "green"}[gi.state]
    spans: list[Span] = [_sp("git", label_clr), _sp(" ", ""), _sp(gi.branch, "cyan")]
    if gi.commit:
        spans.append(_sp(f"/{gi.commit}", "dim"))
    if gi.untracked:
        spans.append(_sp(f" +{gi.untracked}", "yellow"))
    if gi.modified:
        spans.append(_sp(f" ~{gi.modified}", "yellow"))
    if gi.deleted:
        spans.append(_sp(f" -{gi.deleted}", "yellow"))
    if gi.renamed:
        spans.append(_sp(f" R{gi.renamed}", "yellow"))
    if gi.ahead:
        spans.append(_sp(f" ↑{gi.ahead}", "yellow"))
    if gi.behind:
        spans.append(_sp(f" ↓{gi.behind}", "red"))
    if gi.clean and gi.has_upstream and not gi.detached:
        spans.append(_sp(" ✓", "green"))
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
    steps: int = 0
    has_turn: bool = False
    opened_at: float = 0.0  # session start (epoch); 0 = unknown


def _short_model(model: str) -> str:
    name = (model or "?").split("/")[-1]
    return name if len(name) <= 22 else name[:21] + "…"


def _ctx_zone_colour(pressure: float) -> str:
    if pressure < 0.50:
        return "green"
    if pressure < 0.80:
        return "yellow"
    if pressure < 0.95:
        return "orange"
    return "red"


def fields(session, slots, repo: str, state: StatusState) -> list[Segment]:
    """Ordered status segments. THE place to change the bar's format. Order
    mirrors yet-another-statusline (path · git · ctx · rate · timing · model-last)
    with luxe mode chips pinned first. `priority` drives responsive drop order
    (higher = dropped first); git/ctx/model are protected (priority 1)."""
    segs: list[Segment] = []

    # mode chips (luxe-specific; chat.sdd requires write-mode visible) ----
    if session.write_enabled:
        chip: list[Span] = [(" WRITE ", "bg:#8a6d00 #ffffff bold", "black on yellow")]
        if session.unrestricted_bash:
            chip += [(" ", "", ""), (" BASH ", "bg:#8a1c1c #ffffff bold", "white on red")]
        segs.append(Segment(chip, priority=1))
    else:
        segs.append(Segment([(" READ-ONLY ", "bg:#1f5c2f #ffffff bold", "white on green")],
                            priority=1))

    # home-relative path (elastic: middle-ellipsised before any segment drops)
    if repo:
        home = os.path.expanduser("~")
        shown = "~" + repo[len(home):] if home and repo.startswith(home) else repo
        segs.append(Segment([_sp(shown, "blue")], priority=2, path=True))

    # git (protected) -----------------------------------------------------
    git_seg = _git_segment(repo)
    if git_seg:
        segs.append(Segment(git_seg, priority=1))

    # context occupancy (protected) --------------------------------------
    ctx_spans: list[Span] = [_sp("ctx ", "dim")]
    if state.has_turn:
        ctx_spans.append(_sp(f"{state.ctx_pressure:.0%}", _ctx_zone_colour(state.ctx_pressure)))
    tier = tier_label(session.num_ctx_override) if session.num_ctx_override else "default"
    ctx_spans.append(_sp(f" {tier}", "dim"))
    segs.append(Segment(ctx_spans, priority=2))

    # generation rate + wall (dropped first on overflow) -----------------
    if state.has_turn:
        segs.append(Segment([_sp(f"{state.tok_per_s:.0f}tok/s {state.wall_s:.1f}s", "dim")],
                            priority=9))

    # session timing: start <date time> · last <HH:MM> (droppable) -------
    if state.opened_at:
        started = time.strftime("%d-%b-%y %H:%M", time.localtime(state.opened_at)).lower()
        now = time.strftime("%H:%M", time.localtime())
        segs.append(Segment([_sp("start ", "dim"), _sp(started, "white"),
                             _sp(" · last ", "dim"), _sp(now, "white")], priority=7))

    # model pinned last (protected) --------------------------------------
    model = state.model or slots.model_for("chat")
    segs.append(Segment([_sp(f"{state.slot}:{_short_model(model)}", "cyan")], priority=1))

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
    parts: list[tuple[str, str]] = []
    for i, seg in enumerate(fit(fields(session, slots, repo, state), w)):
        if i:
            parts.append(("#666666", " · "))
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
    return "[dim]·[/] " + " [dim]·[/] ".join(out_segs)


def to_rich_text(segments: list[Segment]):
    """Render fitted segments as a Rich Text (for the live in-turn status bar)."""
    from rich.text import Text

    t = Text()
    for i, seg in enumerate(segments):
        if i:
            t.append(" · ", style="grey42")
        for text, _ptk, rich in seg.spans:
            t.append(text, style=(rich or None))
    return t


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class LiveActivity:
    """Rich renderable for the in-turn live bar: spinner + ticking elapsed +
    tool count + current tool, followed by the fitted status segments. Re-renders
    under rich.Live auto-refresh so the clock/spinner animate during generation."""

    def __init__(self, session, slots, repo: str, state: StatusState, started_at: float):
        self.session = session
        self.slots = slots
        self.repo = repo
        self.state = state
        self.started_at = started_at
        self.tools = 0
        self.last_tool = ""

    def note(self, tc) -> None:
        self.tools += 1
        self.last_tool = getattr(tc, "name", "") or ""

    def __rich__(self):
        from rich.text import Text

        elapsed = max(0.0, time.time() - self.started_at)
        frame = _SPINNER[int(elapsed * 10) % len(_SPINNER)]
        head = Text()
        head.append(f"{frame} ", style="cyan")
        head.append(f"{elapsed:5.1f}s ", style="bold")
        head.append(f"·{self.tools} tools", style="dim")
        if self.last_tool:
            head.append(f" · {self.last_tool}", style="yellow")
        head.append("  ", style="")
        segs = fit(fields(self.session, self.slots, self.repo, self.state), _term_width())
        head.append_text(to_rich_text(segs))
        return head
