"""The `luxe chat` interactive loop.

Each user turn drives exactly one `run_single` call (chat.sdd). Conversation
state lives in `ChatSession`; the loop never forks `run_agent`. Streaming/
liveness comes from the existing `on_tool_event` seam; cancellation rides the
same seam via `CancelToken` + `ChatCancelled`.
"""

from __future__ import annotations

import logging
import re
import signal
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

from rich.console import Console
from rich.live import Live
from rich.markup import escape as _escape
from rich.status import Status

from luxe.agents.single import run_single
from luxe.backend import BackendError
from luxe.chat import commands as cmd
from luxe.chat.render import (
    ARROW_PALETTE_PTK,
    CancelToken,
    ChatCancelled,
    arrow_prompt_markup,
    format_tool_call,
    format_tool_call_verbose,
    make_tool_event,
    pick_no_adjacent_repeats,
    rainbow_banner,
    raise_if_cancelled,
    render_final,
    render_footer,
)
from luxe.chat import status as status_mod
from luxe.chat.session import (
    CTX_SUGGEST_PRESSURE,
    CTX_TIERS,
    ChatSession,
    ChatTurn,
    next_tier_up,
)
from luxe.chat import mcptools
from luxe.chat.slots import SlotManager
from luxe.chat.status import StatusState
from luxe.config import PipelineConfig, RoleConfig
from luxe import spec_resolver
from luxe.chat import modelcaps
from luxe.chat import origin as origin_mod
from luxe.memory import project as project_mem
from luxe.memory import session as session_store
from luxe.state import ledger as ledger_mod

@dataclass
class TurnOutcome:
    """What the goal supervisor (B4/C1) needs to decide the next round: how much
    the turn actually did, a fingerprint of its tool calls for stuck-loop
    detection, and the latest observed test result (C1) so completion/stuck key on
    observable state, not the model's ledger discipline. `crashed` is set when
    run_single raised before returning."""
    tool_calls: int = 0
    files_changed: int = 0
    final_text: str = ""
    interrupted: bool = False
    crashed: bool = False
    fingerprint: frozenset = field(default_factory=frozenset)
    # (passed, failed, errors) from the latest test run this turn, or None.
    test_result: tuple[int, int, int] | None = None
    # Rendering inputs, populated by the UI-agnostic core so any front-end (line
    # REPL or Textual TUI) can render the footer/status from one source.
    result: object | None = None          # AgentResult | None
    slot: str = ""
    model: str = ""
    num_ctx: int = 0
    ctx_ceiling: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0


logger = logging.getLogger(__name__)

# Pytest-style summary parsing (C1). Tolerant: each count matched independently,
# singular/plural, anywhere in the output (not a fixed single-line format).
_RE_PASSED = re.compile(r"(\d+)\s+passed", re.IGNORECASE)
_RE_FAILED = re.compile(r"(\d+)\s+(?:failed|failures?)", re.IGNORECASE)
_RE_ERRORS = re.compile(r"(\d+)\s+errors?", re.IGNORECASE)
_RE_TESTCMD = re.compile(r"pytest|\bpython\b.*-m\s+pytest|\bunittest\b|\bnpm\s+test\b",
                         re.IGNORECASE)


def parse_test_result(command: str, result: str, errored: bool
                      ) -> tuple[int, int, int] | None:
    """Extract (passed, failed, errors) from a tool's output, or None if this
    wasn't a recognizable test run. A test command that crashed before emitting a
    summary (traceback / non-zero exit) records errors=1 so it counts as a
    failing, non-progress state rather than being ignored (C1 crash handling)."""
    text = result or ""
    p = _RE_PASSED.search(text)
    f = _RE_FAILED.search(text)
    e = _RE_ERRORS.search(text)
    if p or f or e:
        return (int(p.group(1)) if p else 0,
                int(f.group(1)) if f else 0,
                int(e.group(1)) if e else 0)
    looks_like_test = bool(_RE_TESTCMD.search(command or ""))
    if looks_like_test and (errored or "Traceback" in text or "Error" in text):
        return (0, 0, 1)  # ran tests, crashed before a summary → non-progress
    return None


class _ReasoningStreamer:
    """Buffers streamed model tokens and flushes COMPLETE lines via a callback
    (B2 /reasoning). Line-buffering avoids fighting the rich.Live region — each
    finished line scrolls above it exactly like a tool-call line."""

    def __init__(self, printline: Callable[[str], None]):
        self._buf = ""
        self._printline = printline

    def feed(self, delta: str) -> None:
        self._buf += delta
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._printline(line)

    def flush(self) -> None:
        if self._buf.strip():
            self._printline(self._buf)
        self._buf = ""


# Map an inferred task_type onto a model slot. Cosmetic when every slot is the
# champion; meaningful once a slot points elsewhere. `/use` overrides per turn.
_SLOT_FOR_TASK = {
    "implement": "code",
    "bugfix": "code",
    "document": "code",
    "manage": "code",
    "summarize": "plan",
    "review": "chat",
}


def _default_reader(
    console: Console,
    *,
    status_markup_fn: Callable[[], str] | None = None,
) -> Callable[[], str]:
    """Return a line reader. prompt_toolkit if available, else input(). In both
    cases the status line is printed inline just above the prompt so it scrolls
    with the conversation (Claude-CLI style) instead of being pinned to the
    terminal bottom as a floating bar."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.history import InMemoryHistory

        pt = PromptSession(history=InMemoryHistory())

        def read() -> str:
            # Status line scrolls with history (not a pinned bottom_toolbar).
            if status_markup_fn is not None:
                console.print(status_markup_fn())
            # Fresh colors each turn → the arrows shift per render. ptk needs its
            # own ansi* tokens (B4) so the arrows track the terminal palette.
            colors = pick_no_adjacent_repeats(3, palette=ARROW_PALETTE_PTK)
            message = FormattedText(
                [("", "luxe ")]
                + [(f"bold fg:{c}", "›") for c in colors]
                + [("", " ")]
            )
            return pt.prompt(message)

        return read
    except Exception:  # prompt_toolkit not installed → degrade gracefully
        def read() -> str:
            if status_markup_fn is not None:
                console.print(status_markup_fn())
            console.print(arrow_prompt_markup("luxe"), end="")
            return input()

        return read


def run_chat_repl(
    cfg: PipelineConfig,
    repo_path: str,
    languages: frozenset,
    *,
    console: Console,
    keep_loaded: bool = False,
    resume_session_id: str | None = None,
    reader: Callable[[], str] | None = None,
    infer_task_type: Callable[[str], str] | None = None,
    dev_mode: bool = False,
    start_write: bool = False,
    startup_verbose: str | None = None,
    startup_show_reasoning: bool = False,
    startup_no_terse: bool = False,
    startup_debug: bool = False,
    startup_compact: bool = False,
    theme_name: str | None = None,
    startup_ctx_tier: str | None = None,
    on_project: Callable[[str | None], dict] | None = None,
    project_kind: str = "git",
) -> None:
    from luxe.cli import _infer_task_type  # reuse the maintain heuristic

    infer = infer_task_type or _infer_task_type

    # C-T: select a curated luxe palette (auto = track terminal/YASL theme).
    if theme_name:
        from luxe.chat import theme as theme_mod
        theme_mod.set_palette(theme_name)

    slots = SlotManager(cfg, on_status=lambda m: console.print(f"[dim]· {m}[/]"))
    session = ChatSession(
        repo_path=repo_path,
        project_hash=project_mem.project_hash(repo_path) if repo_path else "",
        languages=languages,
        project_kind=project_kind,
    )
    # One cheap stat at session build: machines with the user's SSH-tunnel
    # tool get PLANEPROXY_HINT in the frame (session.py); others see nothing.
    from luxe.planeproxy import binary_present as _pp_present
    session.planeproxy_present = _pp_present()
    if dev_mode:
        session.write_enabled = True
        session.unrestricted_bash = True
    if start_write:
        # `luxe code` posture: write tools ON from turn one (its reason to
        # exist is changing code). Bash stays gated — /bash as usual.
        session.write_enabled = True

    # C3 startup verbosity flags (set before the loop so /goal users get them
    # without typing REPL commands first). --debug = verbose full + reasoning.
    if startup_debug:
        session.verbose_level = "full"
        session.show_reasoning = True
    elif startup_verbose:
        session.verbose_level = startup_verbose
    if startup_show_reasoning:
        session.show_reasoning = True
    if startup_no_terse:
        session.terse = False
    if startup_compact:
        session.compact = True
    if startup_ctx_tier and startup_ctx_tier in CTX_TIERS:
        # `--ctx <tier>` = /ctx before the first turn; the per-turn clamp to
        # the box/model ceiling applies exactly as it does for /ctx.
        session.num_ctx_override = CTX_TIERS[startup_ctx_tier]

    # Static bottom-toolbar status bar (chat.sdd lightweight variant): refreshed
    # from `status` between turns; the reader pins it under the input line.
    # Seed num_ctx from the chat slot's role so the bar shows the window size
    # (`ctx 32K`) immediately — before the first turn measures usage.
    status = StatusState(opened_at=time.time(), num_ctx=slots.role_for("chat").num_ctx)
    reader = reader or _default_reader(
        console,
        status_markup_fn=lambda: status_mod.status_markup(session, slots, repo_path, status),
    )
    meta = session_store.new_session(
        repo_path=repo_path,
        project_hash=session.project_hash,
        slot_models=slots.slot_models(),
        backend_name=slots.backend_name,
        base_url=slots.backend.base_url,
    )
    session.session_id = meta.session_id

    # Always-on per-session debug log (chat.sdd): everything luxe.* logs this
    # session lands in <session dir>/debug.log — the post-hoc answer to "what
    # happened" that the screen (especially the TUI) can't provide.
    from luxe.chat import debuglog
    dbglog = debuglog.install(session_store.session_dir(meta.session_id))
    logger.info("session %s start · repo=%s · backend=%s (%s) · slots=%s",
                meta.session_id, repo_path or "(none)", slots.backend_name,
                slots.backend.base_url, slots.slot_models())

    # Record the HEAD the resident BM25/symbol indices (built in chat_cmd just
    # before this) reflect, so /git* can warn if the repo moves mid-session.
    if repo_path:
        from luxe.gitkit.health import current_head
        session.index_head = current_head(repo_path)

    cancel = CancelToken()
    ctx = cmd.CommandContext(
        console=console,
        session=session,
        slots=slots,
        on_resume=_make_resume_hook(console, session),
        on_compare=_make_compare_hook(console, cfg, repo_path, languages, slots),
        on_compare_review=_make_compare_review_hook(console),
        on_git_analysis=_make_git_analysis_hook(console, cfg, session, cancel),
        on_project=_make_project_hook(session, on_project),
        status=status,
    )

    if resume_session_id:
        ctx.on_resume(resume_session_id)

    # The status bar (under the prompt) already shows repo path, slot/model, and
    # write/bash state — so the banner stays minimal to avoid duplicating it.
    # C3: show the build (git short-SHA[+dirty]) so a run is traceable to a commit.
    from luxe.buildinfo import build_status_hint, version_parts
    # Banner: app name (no mode — that's in the status bar) · version + clean/dirty
    # state · session · /help. Shared format with the TUI (chat.sdd).
    _sha, _dirty = version_parts()
    _state = "[yellow](dirty)[/]" if _dirty else "[dim green](clean)[/]"
    console.print(rainbow_banner("luxe")
                  + f"  [dim]· version {_sha}[/] {_state} "
                  + f"[dim]· session {meta.session_id} · /help[/]")
    # Actionable-only build hint (behind→pull, ahead→push, dirty→commit); silent
    # when clean & current.
    _hint = build_status_hint()
    if _hint:
        console.print(f"[yellow][hint][/] [dim]{_hint}[/]")
    # Where the weights actually live (local disk / network volume / remote
    # host) — stated once at startup so a networked session is never implicit.
    console.print(model_origin_notice(slots, status))

    try:
        while True:
            # /plan (B5): draft a plan, then maybe execute — runs before the goal
            # check because choosing "execute" sets goal_active for the next pass.
            if session.plan_pending:
                _run_plan(session, slots, cfg, languages, console, cancel,
                          infer, status)
                continue
            # Goal auto-runner (B4): while a goal is active, the supervisor drives
            # rounds itself instead of blocking on the prompt. Returns when the
            # goal completes, pauses, or is interrupted — then we fall back to the
            # normal interactive prompt.
            if session.goal_active:
                _run_goal_loop(session, slots, cfg, languages, console, cancel,
                               infer, status)
                continue
            try:
                line = reader()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            if cmd.is_command(line):
                res = cmd.dispatch(line, ctx)
                if res.exit:
                    break
                if not res.submit:
                    continue
                line = res.submit   # /retry: fall through and run it as a turn
            try:
                _run_turn(line, session, slots, cfg, languages, console, cancel,
                          infer, status)
            except BackendError as e:
                # Interactive turns survive a dead endpoint: report it and, when
                # the config offers another backend, point at the escape hatch
                # (e.g. "local oMLX unreachable — try /backend m5").
                console.print(f"[red]✗ {e}[/]")
                logger.error("turn BackendError: %s", e)
                session_store.append_turn(session.session_id, "error",
                                          text=str(e), model=slots.backend.model)
                # Manifest auto-degrade: a healthy endpoint whose main model
                # fails a turn (lazy load hits missing/corrupt weights HERE)
                # switches the session to the fallback, loudly.
                notice = slots.note_turn_failure()
                if notice:
                    console.print(f"[yellow]· {notice}[/]")
                else:
                    hint = slots.unreachable_hint()
                    if hint:
                        console.print(f"[yellow]· {hint}[/]")
            except Exception:
                # Parity with the TUI worker: one bad turn must not end the
                # session (an uncaught OSError from a repo walk did exactly
                # that on 2026-07-29). Report the last line, log the rest.
                exc_line = traceback.format_exc().strip().splitlines()[-1]
                console.print(f"[red]✗ turn failed: {exc_line}[/]")
                console.print("[yellow]· the session is still alive — retry, "
                              "or /quit if it repeats[/]")
                logger.error("chat turn crashed\n%s", traceback.format_exc())
                session_store.append_turn(session.session_id, "error",
                                          text=exc_line)
    finally:
        if not keep_loaded:
            # WS3: show the unload is happening BEFORE the blocking call (it can
            # take a few seconds) so quitting doesn't look like a hang.
            with console.status("[dim]unloading models…[/]", spinner="dots"):
                slots.unload_all()
            console.print("[dim]· models unloaded (use --keep-loaded to keep warm)[/]")
        if slots.stats.count:
            console.print(f"[dim]· session swaps: {slots.stats.count} "
                          f"({slots.stats.seconds:.0f}s total)[/]")
        logger.info("session %s end", session.session_id)
        debuglog.uninstall(dbglog)


@dataclass
class TurnPrep:
    """UI-agnostic per-turn state shared by the line REPL and the Textual TUI.
    `call(on_event, on_token, on_progress)` is the verbatim `run_single` closure
    (so both front-ends assemble the request identically); `note_tool` updates
    the collectors a front-end's tool callback must invoke."""
    call: Callable
    note_tool: Callable
    slot: str
    model: str
    dev_bash: bool
    # RoleConfig, not `object` — every consumer reads `.num_ctx`/`.tools` off
    # it, and the loose annotation hid 8 attribute errors behind a type that
    # promises nothing.
    role_cfg: RoleConfig
    run_id: str
    ctx_ceiling: int
    changed_files: list
    fingerprint: set
    test_result: list
    backend_name: str = ""  # multi-backend provenance stamped on the transcript
    # Completed tool calls observed via note_tool. finalize_turn's fallback for
    # an interrupted turn — the in-flight AgentResult is lost when ChatCancelled
    # unwinds, and the transcript used to claim steps=0/tool_calls=0 even when
    # tools had run (session 5bb630813c21 turn 11: one bash call, 9m40s hang,
    # nothing recorded).
    observed_calls: list = field(default_factory=list)


# Tools that are useless without a resident index (chat/project.py "none" mode).
_INDEX_TOOLS = {"bm25_search": "search", "find_symbol": "symbols"}


def index_tools_available() -> dict[str, bool]:
    """Which index-backed tools have their index resident right now."""
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod

    return {"bm25_search": search_mod.get_index() is not None,
            "find_symbol": symbols_mod.get_index() is not None}


def _drop_unavailable_index_tools(role_cfg):
    """Strip `bm25_search` / `find_symbol` when their index isn't built."""
    available = index_tools_available()
    tools = [t for t in (role_cfg.tools or [])
             if t not in _INDEX_TOOLS or available.get(t, False)]
    if len(tools) == len(role_cfg.tools or []):
        return role_cfg
    return role_cfg.model_copy(update={"tools": tools})


def _make_project_hook(session, on_project):
    """Wrap cli's attach hook so the SESSION follows the new project too.

    cli owns the lock and the indexes; the session owns repo_path / project_kind
    / index_head, which drive the prompt frame, the git segment, and `/diff`.
    Returns None when the front-end wasn't given a hook (tests, embedders)."""
    if on_project is None:
        return None

    def _hook(target: str | None) -> dict:
        summary = on_project(target)
        session.repo_path = summary["root"]
        session.project_kind = summary["kind"]
        try:
            from luxe.gitkit.health import current_head
            session.index_head = current_head(summary["root"]) or ""
        except Exception:
            session.index_head = ""
        return summary

    return _hook


def model_origin_notice(slots, status=None) -> str:
    """Resolve where the chat slot's model actually lives, record it on the
    status bar, and return the one-line startup notice (Rich markup).

    Called once per front-end at startup — this is the ONLY place that pays for
    the `/v1/models/status` lookup; every later read hits the per-endpoint
    cache. Local weights are announced too, not just remote ones: the point is
    that you can always tell, not that you get warned when it's bad.
    """
    model = slots.model_for("chat")
    try:
        org = origin_mod.origin_for(slots.backend, model)
    except Exception:
        org = origin_mod.ModelOrigin(kind="unknown", model_id=model)
    if status is not None:
        status.model_origin = org.kind
    colour = "yellow" if org.is_over_the_network else "dim"
    return (f"[dim]· model[/] {model} [dim]—[/] "
            f"[{colour}]{org.glyph} {org.describe()}[/]")


def prepare_turn(message, session, slots, cfg, languages, infer,
                 *, plan_mode: bool = False, cancel=None,
                 on_tool_start=None) -> TurnPrep:
    """UI-agnostic turn setup: slot routing, role/tool/ledger wiring, `/ctx`
    clamp, `extra_context`, user-turn persistence, and the `run_single` closure.
    Shared by both front-ends; the benchmark path is untouched (these tools/args
    are chat-only). Rendering is the caller's job (see `TurnPrep`).

    `cancel` (CancelToken) makes the chat bash subprocess killable mid-flight;
    `on_tool_start(command)` fires at bash DISPATCH so the front-end can show
    the currently running command (completion-time events can't — a hung
    command was invisible). Both optional; None keeps prior behaviour."""
    from luxe.mcp.server import make_read_only_role
    from luxe.state.ledger import make_update_ledger_tool

    task_type = infer(message)
    pinned = session.pinned_slot
    slot = pinned or _SLOT_FOR_TASK.get(task_type, "chat")
    session.pinned_slot = None

    model = slots.model_for(slot)
    dev_bash = session.write_enabled and session.unrestricted_bash and not plan_mode

    backend = slots.backend_for(slot)
    slot_cfg = cfg.slot_config(slot)
    base_role = cfg.role(slot_cfg.role)
    # /plan (B5) forces a read-only drafting turn regardless of write mode.
    write_on = session.write_enabled and not plan_mode
    role_cfg = base_role if write_on else make_read_only_role(base_role)
    # No index (a session started outside a project) → withhold the tools that
    # need one, rather than offering them and answering "index not built" to
    # every call. Derived from what's actually resident, so `/index` mid-session
    # turns them back on with no other bookkeeping.
    role_cfg = _drop_unavailable_index_tools(role_cfg)
    # A model whose chat template can't render tool calls gets NO tool surface:
    # oMLX silently drops the tools array for it, so offering them would produce
    # an agent that never calls a tool and never says why (chat/modelcaps.py).
    caps = modelcaps.for_model(backend, model)
    session.tools_withheld = not caps.usable
    if not caps.usable:
        role_cfg = role_cfg.model_copy(update={"tools": []})

    # EVERY freeform interactive turn gets the conversational persona — chat is
    # a conversation, not a batch job. Keying this on `slot == "chat"` (the
    # 2026-06-30 version) was the "chats become coding sessions" bug: the slot
    # is picked by `_infer_task_type`, a keyword heuristic built for `luxe
    # maintain` goals, so ordinary messages containing "add"/"change"/
    # "explain"/"fix"/… routed to the code/plan slots and inherited the
    # baseline repo-maintenance persona (+ the config's task overlay) — repo
    # orientation loops and "final reports" for plain questions. Slot routing
    # still picks the MODEL (fan-out configs); the persona now follows the
    # turn KIND instead. The task personas remain for the explicit task modes:
    # /plan drafting (plan_mode), autonomous /goal rounds (goal_active), and a
    # user-pinned `/use <slot>` turn. The conversational prompt still does
    # real work — it reads/edits via tools when the message calls for it.
    # Prompt ids resolve in the registry (chat.sdd).
    if pinned is None and not plan_mode and not session.goal_active:
        role_cfg = role_cfg.model_copy(update={
            "system_prompt_id": "chat_conversational",
            "task_prompt_id": "chat_conversational",
            "task_overlay_id": "",
        })

    # Chat bash (chat.sdd), swapped for THIS run only via run_single's extra-tool
    # seam — benchmark/maintain never pass these, so their bash is untouched.
    # update_ledger (B0/B5) is always exposed so the model can maintain its
    # working state across rounds; it only mutates the per-session ledger file.
    _led_def, _led_fn = make_update_ledger_tool(session.session_id)
    extra_tool_defs = [_led_def]
    extra_tool_fns = {"update_ledger": _led_fn}
    # net_probe (2026-07-31): the bounded connectivity ladder, read-only and
    # in-process — the model reaches for THIS instead of hand-rolled curl
    # commands with no timeout (session 5bb630813c21). Rides every chat turn
    # like the ledger tool; benchmark/maintain never pass extra tools.
    from luxe.netdiag import make_net_probe_tool
    _net_def, _net_fn = make_net_probe_tool()
    extra_tool_defs.append(_net_def)
    extra_tool_fns["net_probe"] = _net_fn
    # planeproxy_diag (2026-08-02): bounded, read-only diagnosis of the user's
    # SSH-tunnel tool via its own `status --json`/`doctor --json` — the model
    # reaches for THIS instead of ssh -v / ps / log spelunking. Never runs
    # up/down (mutations are commands with consent flows, chat.sdd). Same
    # always-on seam as net_probe; benchmark/maintain never pass extra tools.
    from luxe.planeproxy import make_planeproxy_tool
    _pp_def, _pp_fn = make_planeproxy_tool()
    extra_tool_defs.append(_pp_def)
    extra_tool_fns["planeproxy_diag"] = _pp_fn
    # Browser tools (2026-08-02, restored from ff6eab7): read-only headless-
    # Chrome navigate + read for JS-rendered pages, host-allowlisted and
    # deny-by-default. Deps ([browser] extra) are lazy — absent deps yield a
    # clear tool error, never an import failure here.
    from luxe.browser import make_browser_tools
    for _b_def, _b_fn in make_browser_tools():
        extra_tool_defs.append(_b_def)
        extra_tool_fns[_b_def.name] = _b_fn
    if write_on:
        from luxe.tools.shell import (
            make_bash_fn,
            restricted_bash_def,
            unrestricted_bash_def,
        )
        if session.unrestricted_bash:
            extra_tool_defs.append(unrestricted_bash_def())
            extra_tool_fns["bash"] = make_bash_fn(
                unrestricted=True, cancel=cancel, on_start=on_tool_start)
        else:
            extra_tool_defs.append(restricted_bash_def())
            extra_tool_fns["bash"] = make_bash_fn(
                restricted_hint=True, cancel=cancel, on_start=on_tool_start)
        # Chat-only: prose files (.txt/.md/…) skip the placeholder honesty
        # guard — "save these notes with '# TODO: implement X'" is content the
        # user dictated, not a code stub (the guard's target). Code extensions
        # stay guarded; benchmark/maintain keep the default TOOL_FNS
        # (tools.sdd). Same per-turn seam as the bash swap above.
        from luxe.tools.fs import make_prose_aware_write_fns
        extra_tool_fns.update(make_prose_aware_write_fns())

    # MCP tools (cli `--mcp`, chat-only): inspection tools ride every turn;
    # tools matching the server's `gate_tools` (mutating remote operations)
    # follow the same /write gate as the native mutation surface. Withheld
    # entirely for a model whose template can't call tools.
    mcp_surface = mcptools.active()
    if mcp_surface is not None and caps.usable:
        extra_tool_defs.extend(mcp_surface.always_defs)
        if write_on:
            extra_tool_defs.extend(mcp_surface.gated_defs)
        extra_tool_fns.update(mcp_surface.fns)

    # `/ctx` size override (chat-only) — clamp to the effective ceiling
    # (role's box ceiling ∧ the manifest's per-model cap for the model this
    # turn actually runs) so a tier request can never exceed what this
    # box/model pair can hold — including after an auto-degrade.
    ctx_ceiling = slots.ctx_ceiling(slot)
    if session.num_ctx_override:
        effective_ctx = min(session.num_ctx_override, ctx_ceiling)
        if effective_ctx != role_cfg.num_ctx:
            role_cfg = role_cfg.model_copy(update={"num_ctx": effective_ctx})

    extra_context, fold_version = session.build_extra_context(message)

    turn_idx = len(session.turns)
    run_id = f"{session.session_id}-{turn_idx}"
    session_store.append_turn(session.session_id, "user", text=message, slot=slot)
    if extra_context:
        session_store.append_fold(session.session_id, turn_idx, fold_version, extra_context)

    # Per-turn collectors feeding the ledger (B0/B5) and the goal supervisor (B4):
    # files actually written/edited, and a fingerprint of tool calls (name + the
    # salient arg) for stuck-loop detection.
    changed_files: list[str] = []
    fingerprint: set = set()
    test_result: list = [None]  # latest (passed, failed, errors) seen this turn (C1)
    observed_calls: list = []   # completed calls; interrupt-stats fallback

    def _note_tool(tc) -> None:
        observed_calls.append(tc.name)
        args = getattr(tc, "arguments", {}) or {}
        prim = (args.get("path") or args.get("query")
                or args.get("command") or args.get("pattern"))
        fingerprint.add((tc.name, str(prim) if prim is not None else ""))
        if (tc.name in ("write_file", "edit_file")
                and not getattr(tc, "error", None)
                and not getattr(tc, "duplicate", False)):
            p = args.get("path")
            if p:
                changed_files.append(str(p))
        # C1 observable telemetry: capture the latest test result from any tool's
        # output (pytest usually runs via bash).
        tr = parse_test_result(str(args.get("command", "")),
                               getattr(tc, "result", "") or "",
                               bool(getattr(tc, "error", None)))
        if tr is not None:
            test_result[0] = tr

    def _call(on_event, on_token=None, on_progress=None):
        return run_single(
            backend, role_cfg, goal=message, task_type=task_type,
            languages=languages, extra_tool_defs=extra_tool_defs,
            extra_tool_fns=extra_tool_fns, on_tool_event=on_event,
            on_token=on_token, on_progress=on_progress, run_id=run_id, phase="chat",
            extra_context=extra_context,
        )

    return TurnPrep(
        call=_call, note_tool=_note_tool, slot=slot, model=model,
        dev_bash=dev_bash, role_cfg=role_cfg, run_id=run_id,
        ctx_ceiling=ctx_ceiling, changed_files=changed_files,
        fingerprint=fingerprint, test_result=test_result,
        backend_name=getattr(slots, "backend_name", ""),
        observed_calls=observed_calls,
    )


def finalize_turn(session, prep: TurnPrep, result, *, interrupted: bool,
                  message: str, started_at: float, ended_at: float,
                  partial_text: str = "") -> TurnOutcome:
    """UI-agnostic post-turn bookkeeping: record changed files, persist the
    assistant turn, update in-memory history, and build the `TurnOutcome` (incl.
    rendering inputs) the front-end renders from. Files are recorded even on an
    interrupted turn — those writes already happened on disk.

    On interrupt the in-flight AgentResult is lost (ChatCancelled unwinds
    run_single), so the transcript record falls back to what the front-end
    observed: `prep.observed_calls` for the tool-call count and `partial_text`
    (the streamed prose so far) for the body — "partial turn saved" used to
    save an empty record (steps=0/tool_calls=0/len=0) no matter what ran."""
    if prep.changed_files:
        ledger_mod.record_files(session.session_id, prep.changed_files)
        # The contract scan is cached per repo root for the session (it walks
        # the whole root — 19s when that root is $HOME). A turn that wrote a
        # `.sdd` invalidates it so the next turn sees the new contract.
        if any(str(p).endswith(".sdd") for p in prep.changed_files):
            spec_resolver.invalidate_scan_cache(session.repo_path or None)

    assistant_text = (result.final_text if result else "") or ""
    if not assistant_text and interrupted:
        assistant_text = partial_text or ""
    observed = len(prep.observed_calls)
    tool_calls_total = result.tool_calls_total if result else observed
    session_store.append_turn(
        session.session_id, "assistant",
        text=assistant_text, run_id=prep.run_id, interrupted=interrupted,
        steps=(result.steps if result else 0),
        tool_calls=tool_calls_total,
        backend=prep.backend_name,
    )
    # Server-truth turn record for post-hoc ctx forensics — the status bar's
    # number is otherwise unlogged (the TUI swallows stdout; debug.log is the
    # only surface that survives the session).
    if result is not None:
        num_ctx = prep.role_cfg.num_ctx
        last_pt = getattr(result, "last_prompt_tokens", 0) or 0
        ctx_srv = last_pt / num_ctx if last_pt and num_ctx else 0.0
        logger.debug(
            "turn done run_id=%s steps=%d tool_calls=%d prompt_tokens=%d "
            "last_prompt_tokens=%d num_ctx=%d ctx_server=%.1f%% ctx_est=%.1f%% "
            "peak_est=%.1f%%",
            prep.run_id, result.steps, result.tool_calls_total,
            result.prompt_tokens, last_pt, num_ctx,
            ctx_srv * 100, result.final_context_pressure * 100,
            result.peak_context_pressure * 100)
    else:
        logger.debug("turn interrupted run_id=%s observed_tool_calls=%d "
                     "partial_chars=%d", prep.run_id, observed,
                     len(assistant_text))
    session_store.touch(session.session_id)
    session.add_turn(ChatTurn(
        user=message, assistant=assistant_text, slot=prep.slot,
        model=prep.model, run_id=prep.run_id,
    ))
    return TurnOutcome(
        tool_calls=tool_calls_total,
        files_changed=len(set(prep.changed_files)),
        final_text=assistant_text,
        interrupted=interrupted,
        fingerprint=frozenset(prep.fingerprint),
        test_result=prep.test_result[0],
        result=result,
        slot=prep.slot,
        model=prep.model,
        num_ctx=prep.role_cfg.num_ctx,
        ctx_ceiling=prep.ctx_ceiling,
        started_at=started_at,
        ended_at=ended_at,
    )


def _run_turn(
    message: str,
    session: ChatSession,
    slots: SlotManager,
    cfg: PipelineConfig,
    languages: frozenset,
    console: Console,
    cancel: CancelToken,
    infer: Callable[[str], str],
    status: StatusState | None = None,
    plan_mode: bool = False,
) -> TurnOutcome:
    # Dispatch-time visibility: print the bash command as it STARTS (dim `$`
    # line) so a hung command is identifiable on screen; the usual tool line
    # still summarizes it on completion. `cancel` makes that subprocess
    # killable mid-flight (esc/Ctrl-C lands in ~0.2s, not at the timeout).
    def _on_tool_start(command: str) -> None:
        if command:
            console.print(f"[dim]$ {_escape(command)}[/]", highlight=False)

    prep = prepare_turn(message, session, slots, cfg, languages, infer,
                        plan_mode=plan_mode, cancel=cancel,
                        on_tool_start=_on_tool_start)
    role_cfg = prep.role_cfg
    bash_note = " · [red]bash:unrestricted[/]" if prep.dev_bash else ""
    console.print(f"[dim]slot: {prep.slot} · model: {prep.model}{bash_note}[/]")

    cancel.reset()
    prev_handler = None
    try:
        prev_handler = signal.getsignal(signal.SIGINT)

        def _on_sigint(signum, frame):
            cancel.requested = True

        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        prev_handler = None  # not in main thread (e.g. tests)

    interrupted = False
    result = None
    started_at = time.time()
    stream_parts: list[str] = []  # streamed prose; persisted if interrupted

    try:
        if console.is_terminal:
            # Live layout (chat.sdd): tool lines scroll above a status bar that
            # ticks live during the turn (spinner/elapsed/tool count). transient
            # clears the bar when the turn ends; the footer then prints below.
            live_state = StatusState(
                slot=prep.slot, model=prep.model,
                opened_at=(status.opened_at if status else 0.0),
                num_ctx=role_cfg.num_ctx,  # show ctx size during the turn
                ctx_pressure=(status.ctx_pressure if status else 0.0),
                has_turn=(status.has_turn if status else False),  # last-known %
            )
            activity = status_mod.LiveActivity(
                session, slots, session.repo_path, live_state, started_at)
            with Live(activity, console=console, refresh_per_second=10,
                      transient=True) as live:
                reasoner = _ReasoningStreamer(
                    lambda ln: live.console.print(f"[dim]{_escape(ln)}[/]"))

                def _on_event(tc):
                    if session.verbose_level in ("diff", "full"):
                        live.console.print(
                            format_tool_call_verbose(tc, session.verbose_level))
                    else:
                        # highlight=False: keep markup, stop the ReprHighlighter
                        # repainting the tool name magenta over the theme (iter-6).
                        live.console.print(format_tool_call(tc), highlight=False)
                    activity.note(tc)
                    prep.note_tool(tc)
                    raise_if_cancelled(cancel)

                def _on_token(delta):
                    # B1: cancel lands mid-generation (cadence-bound, not instant).
                    raise_if_cancelled(cancel)
                    stream_parts.append(delta)
                    activity.on_token(delta)
                    if session.show_reasoning:
                        reasoner.feed(delta)

                def _on_progress(pressure):
                    # C2: live ctx% during the turn — same instantaneous metric
                    # the [token-progress] line prints, so they agree.
                    live_state.ctx_pressure = pressure
                    live_state.has_turn = True

                result = prep.call(_on_event, _on_token, _on_progress)
                if session.show_reasoning:
                    reasoner.flush()
        else:
            reasoner = _ReasoningStreamer(
                lambda ln: console.print(f"[dim]{_escape(ln)}[/]"))
            base_event = make_tool_event(console, cancel, session.verbose_level)

            def _on_event(tc):
                prep.note_tool(tc)
                base_event(tc)

            def _on_token(delta):
                raise_if_cancelled(cancel)
                stream_parts.append(delta)
                if session.show_reasoning:
                    reasoner.feed(delta)

            with Status("[dim]generating…[/]", console=console, spinner="dots"):
                result = prep.call(_on_event, _on_token)
            if session.show_reasoning:
                reasoner.flush()
    except (ChatCancelled, KeyboardInterrupt):
        interrupted = True
        console.print("[yellow]· interrupted — partial turn saved[/]")
    finally:
        ended_at = time.time()
        if prev_handler is not None:
            try:
                signal.signal(signal.SIGINT, prev_handler)
            except (ValueError, OSError):
                pass

    # UI-agnostic bookkeeping (records changed files even on interrupt, persists
    # the assistant turn, builds the outcome the renderer reads from).
    outcome = finalize_turn(session, prep, result, interrupted=interrupted,
                            message=message, started_at=started_at,
                            ended_at=ended_at,
                            partial_text="".join(stream_parts))

    if not interrupted and result is not None:
        # WS4 output ladder: full when /verbose full (or /debug), else compact
        # if /compact, else the default truncated preview.
        out_mode = ("full" if session.verbose_level == "full"
                    else "compact" if session.compact else "truncated")
        render_final(console, outcome.final_text, mode=out_mode)
        render_footer(
            console,
            slot=prep.slot,
            model=prep.model,
            write_enabled=session.write_enabled,
            result=result,
            swap_count=slots.stats.count,
            swap_seconds=slots.stats.seconds,
            started_at=started_at,
            ended_at=ended_at,
            num_ctx=prep.role_cfg.num_ctx,
        )
        if status is not None:
            status.slot = prep.slot
            status.model = prep.model
            status.model_origin = origin_mod.cached_origin_for(
                slots.backend, prep.model).kind
            status.wall_s = result.wall_s
            status.tok_per_s = (result.completion_tokens / result.wall_s
                                if result.wall_s > 0 else 0.0)
            # Bar shows the server-truth context fill when the turn reported
            # usage (the chars/4 estimate misses tool schemas and read a flat
            # 7% at a real ~12% — 2026-07-30); estimate is the fallback.
            if result.last_prompt_tokens and role_cfg.num_ctx:
                status.ctx_pressure = (result.last_prompt_tokens
                                       / role_cfg.num_ctx)
            else:
                status.ctx_pressure = result.final_context_pressure
            status.num_ctx = role_cfg.num_ctx
            status.prompt_tokens = result.prompt_tokens
            status.steps = result.steps
            status.has_turn = True
        # Auto-suggest a larger window (never resizes silently — chat.sdd).
        if result.peak_context_pressure >= CTX_SUGGEST_PRESSURE:
            nxt = next_tier_up(role_cfg.num_ctx, prep.ctx_ceiling)
            if nxt:
                console.print(
                    f"[dim]· context pressure {result.peak_context_pressure:.0%} — "
                    f"`/ctx {nxt[0]}` (num_ctx {nxt[1]}) gives more headroom[/]"
                )

        # Working-state view (B2): show the ledger after the footer so the
        # operator sees decided/done/remaining at a glance.
        if session.verbose_level in ("diff", "full"):
            console.print(ledger_mod.render_rich(ledger_mod.load(session.session_id)))

    return outcome


# -- goal auto-runner (B1/B4, C1) -------------------------------------------

_GOAL_DONE_ROUNDS = 2       # consecutive corroborated settled rounds → done
_GOAL_STUCK_SETTLED = 3     # settled rounds with no NEW completed items → stuck (idle)
_GOAL_STUCK_THRASH = 3      # test-running rounds with no failure improvement → stuck
_GOAL_STUCK_FP_ROUNDS = 3   # identical tool fingerprint, no edits → faster trip
_GOAL_MAX_CRASHES = 3       # consecutive crashes → pause for a human
_GOAL_LOW_CTX = 32768       # ≤ this is below the practical minimum for builds
_GOAL_SENTINEL = "LUXE_GOAL_DONE"

_GOAL_SUFFIX = (
    "\n\n(Autonomous goal mode. Cadence: work → tool output → update_ledger → done. "
    "Record finished work in `completed` via update_ledger as you go; do not narrate "
    "or re-summarize. When the objective is FULLY complete and verified (e.g. the "
    f"test suite passes), reply with ONLY a line containing {_GOAL_SENTINEL}.)"
)


@dataclass
class GoalDecision:
    """Result of evaluating one goal round (pure, unit-testable)."""
    verdict: str  # "continue" | "done" | "stuck"
    reason: str   # short machine reason for the verdict
    done_streak: int
    settled_no_progress: int
    completed_ever_grew: bool
    thrash_count: int
    best_failures: int | None
    best_total: int | None


def evaluate_goal_round(
    *, settled: bool, sentinel: bool, completed_count: int, new_completed: bool,
    test_result: tuple[int, int, int] | None,
    done_streak: int, settled_no_progress: int, completed_ever_grew: bool,
    thrash_count: int, best_failures: int | None, best_total: int | None,
    done_rounds: int = _GOAL_DONE_ROUNDS, stuck_settled: int = _GOAL_STUCK_SETTLED,
    stuck_thrash: int = _GOAL_STUCK_THRASH,
) -> GoalDecision:
    """Pure per-round decision for the goal supervisor (C1/D1/D6). Keys on
    OBSERVABLE state, not model bookkeeping:

    DONE when a settled round either (a) shows GREEN tests (observable truth), or
    (b) carries the sentinel + corroborating ledger `completed` (model self-report),
    for 2 consecutive rounds. (a) lets a finished run complete even if the model
    never logged completed / signaled done (the iter-5 run-2/3 false-STUCK).

    STUCK via two independent guards: idle (settled rounds with no new completed) and
    THRASH (rounds that ran tests without reducing failures and added no completed —
    catches the edit→test→same-failures loop the idle counter misses). Completion is
    evaluated first and resets both counters.
    """
    completed_ever_grew = completed_ever_grew or new_completed
    passed = failed = errors = 0
    ran_tests = test_result is not None
    if ran_tests:
        passed, failed, errors = test_result
    failures = failed + errors
    total = passed + failed + errors
    tests_green = ran_tests and failures == 0 and passed > 0

    def _mk(verdict: str, reason: str) -> GoalDecision:
        return GoalDecision(verdict, reason, done_streak, settled_no_progress,
                            completed_ever_grew, thrash_count, best_failures, best_total)

    # --- Completion (observable green OR corroborated sentinel) ---------------
    sentinel_ok = sentinel and completed_count > 0 and completed_ever_grew
    if settled and (tests_green or sentinel_ok):
        done_streak += 1
        settled_no_progress = 0
        thrash_count = 0
        reason = "tests green" if tests_green else "signaled done"
        return _mk("done" if done_streak >= done_rounds else "continue", reason)
    done_streak = 0

    # --- Progress accounting for the thrash guard (D6) ------------------------
    # A round makes progress if failures dropped below the best seen, a broader
    # suite appeared (new baseline), or a new completed item landed.
    new_baseline = ran_tests and (best_total is None or total > best_total)
    improved = ran_tests and (best_failures is None or failures < best_failures or new_baseline)
    if ran_tests:
        if best_total is None or total > best_total:
            best_total = total
        if best_failures is None or failures < best_failures or new_baseline:
            best_failures = failures
    if improved or new_completed:
        thrash_count = 0
    elif ran_tests and failures > 0:
        # Ran tests, no improvement, still failing → thrashing (whether it edited
        # or idled). No-test rounds don't count, so staged work isn't punished.
        thrash_count += 1
    if thrash_count >= stuck_thrash:
        return _mk("stuck", f"thrashing on {failures} failing test(s)")

    # --- Idle STUCK (settled rounds with no new completed work) ---------------
    settled_no_progress = settled_no_progress + 1 if (settled and not new_completed) else 0
    if settled_no_progress >= stuck_settled:
        return _mk("stuck", "no new completed work")
    return _mk("continue", "")


def _run_goal_loop(
    session: ChatSession,
    slots: SlotManager,
    cfg: PipelineConfig,
    languages: frozenset,
    console: Console,
    cancel: CancelToken,
    infer: Callable[[str], str],
    status: StatusState | None,
    run_turn: Callable | None = None,
) -> None:
    """Supervisor: auto-issue rounds until the objective is reached, the budget
    is hit, the agent gets stuck, or too many crashes pile up. Survives a crashed
    round (the turn is already persisted; the ledger + history rehydrate state).

    Completion is LEDGER-AWARE (B1): the iteration-3 data showed `LUXE_GOAL_DONE`
    is self-reported and noisy (a failed 32K run emitted it 20× with an empty
    ledger), while completed-item richness cleanly separated success from failure.
    So a "settled" round (no file edits — re-running tests no longer blocks
    completion) is only treated as DONE when the ledger corroborates (completed
    non-empty AND in_progress cleared), for 2 consecutive rounds. Settled rounds
    that record NO new completed work accrue toward an honest STUCK exit instead.
    """
    run_turn = run_turn or _run_turn  # front-end's turn renderer (line / TUI)
    done_streak = 0           # consecutive corroborated settled rounds
    settled_no_progress = 0   # consecutive settled rounds with no NEW completed
    completed_ever_grew = False
    thrash_count = 0          # test rounds with no failure improvement (D6)
    best_failures: int | None = None
    best_total: int | None = None
    last_test: tuple[int, int, int] | None = None  # for the honest STUCK message
    recent_fps: list[frozenset] = []
    prev_completed = len(ledger_mod.load(session.session_id).completed)

    console.print(
        f"[bold cyan]· goal started[/] [dim]{_escape(session.goal)}[/]\n"
        f"[dim]  up to {session.goal_max_rounds} rounds · Ctrl-C or /goal stop to halt[/]")
    eff_ctx = session.num_ctx_override or slots.role_for("chat").num_ctx
    if eff_ctx and eff_ctx <= _GOAL_LOW_CTX:
        console.print(
            f"[yellow]· note: {eff_ctx // 1024}K context is below the practical "
            f"minimum for build tasks — `/ctx large` reduces stuck/incomplete rounds.[/]")

    while session.goal_active:
        if session.goal_round >= session.goal_max_rounds:
            console.print(f"[yellow]· goal budget reached "
                          f"({session.goal_max_rounds} rounds) — pausing for a human.[/]")
            session.goal_active = False
            break

        session.goal_round += 1
        rnd = session.goal_round
        base = session.goal if rnd == 1 else "continue work"
        message = base + _GOAL_SUFFIX
        console.print(f"\n[bold]· [goal round {rnd}/{session.goal_max_rounds}][/] "
                      f"[dim]{_escape(base)}[/]")

        try:
            outcome = run_turn(message, session, slots, cfg, languages,
                               console, cancel, infer, status)
        except (ChatCancelled, KeyboardInterrupt):
            console.print("[yellow]· goal halted by interrupt.[/]")
            session.goal_active = False
            break
        except Exception as e:  # crash: bounded retry on CONSECUTIVE failures
            session.consecutive_crashes += 1
            console.print(f"[red]· goal round crashed ({type(e).__name__}: {e}) — "
                          f"consecutive {session.consecutive_crashes}/"
                          f"{_GOAL_MAX_CRASHES}[/]")
            if session.consecutive_crashes >= _GOAL_MAX_CRASHES:
                console.print("[yellow]· too many consecutive crashes — "
                              "pausing goal for a human.[/]")
                session.goal_active = False
            continue
        session.consecutive_crashes = 0

        if outcome.interrupted:
            console.print("[yellow]· goal halted by interrupt.[/]")
            session.goal_active = False
            break

        # Read the (pruned) ledger to judge progress this round.
        led = ledger_mod.prune(session.session_id)
        ncomp = len(led.completed)
        new_completed = ncomp > prev_completed
        prev_completed = ncomp
        settled = outcome.files_changed == 0
        sentinel = _GOAL_SENTINEL in (outcome.final_text or "")
        if outcome.test_result is not None:
            last_test = outcome.test_result

        # Observable-signal decision (C1/D6): completion evaluated BEFORE stuck; a
        # corroborated round resets the stuck/thrash counters. Keys on green tests
        # or sentinel+ledger, never plan bookkeeping alone.
        decision = evaluate_goal_round(
            settled=settled, sentinel=sentinel, completed_count=ncomp,
            new_completed=new_completed, test_result=outcome.test_result,
            done_streak=done_streak, settled_no_progress=settled_no_progress,
            completed_ever_grew=completed_ever_grew, thrash_count=thrash_count,
            best_failures=best_failures, best_total=best_total)
        done_streak = decision.done_streak
        settled_no_progress = decision.settled_no_progress
        completed_ever_grew = decision.completed_ever_grew
        thrash_count = decision.thrash_count
        best_failures = decision.best_failures
        best_total = decision.best_total

        if decision.verdict == "done":
            ledger_mod.clear_in_progress(session.session_id)  # cosmetic provenance
            console.print(f"[green]· goal complete ({decision.reason}; completed={ncomp}) "
                          f"after {rnd} round(s).[/]")
            session.goal_active = False
            break

        # Fast trip: identical non-empty tool fingerprint repeating with no edits.
        if outcome.fingerprint:
            recent_fps.append(outcome.fingerprint)
            recent_fps[:] = recent_fps[-_GOAL_STUCK_FP_ROUNDS:]
            if (len(recent_fps) == _GOAL_STUCK_FP_ROUNDS
                    and len(set(recent_fps)) == 1 and settled):
                console.print(
                    f"[yellow]· goal appears stuck — same {len(outcome.fingerprint)} "
                    f"call(s) for {_GOAL_STUCK_FP_ROUNDS} rounds, no edits. "
                    f"Pausing for a human.[/]")
                session.goal_active = False
                break
        else:
            recent_fps.clear()

        if decision.verdict == "stuck":
            # Honest, observable status — distinguishes "nothing happened" from
            # "substantial work, convergence failed" (C1).
            built = len(led.files)
            if last_test is not None:
                p, f, e = last_test
                tests = f"last tests: {p} passed, {f} failed, {e} error(s)"
            else:
                tests = "no test run observed"
            console.print(
                f"[yellow]· goal STUCK ({decision.reason}) after {rnd} round(s) — "
                f"built {built} file(s); {tests}; completed={ncomp}. "
                f"Pausing for a human.[/]")
            session.goal_active = False
            break

    session.goal_round = 0  # ready for a fresh /goal


# -- /plan mode (B5) --------------------------------------------------------


def _write_plan_file(session: ChatSession, plan_text: str):
    """Write the drafted plan, never clobbering an existing plan.md."""
    from pathlib import Path

    base = Path(session.repo_path or ".")
    target = base / "plan.md"
    if target.exists():
        plans_dir = base / ".luxe" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        sid = (session.session_id or "plan")[:8]
        target = plans_dir / f"plan-{sid}-{len(session.turns)}.md"
    target.write_text(plan_text)
    return target


def _run_plan(
    session: ChatSession,
    slots: SlotManager,
    cfg: PipelineConfig,
    languages: frozenset,
    console: Console,
    cancel: CancelToken,
    infer: Callable[[str], str],
    status: StatusState | None,
    run_turn: Callable | None = None,
    reader: Callable[[str], str] | None = None,
) -> None:
    """Draft a plan read-only, then ask: save / execute / both / discard (B5).

    `run_turn`/`reader` are injected by the TUI (renders into the RichLog; asks
    via a modal); both default to the line-REPL behaviour."""
    from pathlib import Path

    from luxe.agents.prompts import PLAN_HINT

    run_turn = run_turn or _run_turn
    objective = (session.plan_pending or "").strip()
    session.plan_pending = None
    if not objective:
        return

    console.print(f"[bold cyan]· planning[/] [dim]{_escape(objective)}[/]")
    message = f"{objective}\n\n{PLAN_HINT}"
    try:
        outcome = run_turn(message, session, slots, cfg, languages,
                           console, cancel, infer, status, plan_mode=True)
    except (ChatCancelled, KeyboardInterrupt):
        console.print("[yellow]· planning interrupted.[/]")
        return

    plan_text = (outcome.final_text or "").strip()
    if not plan_text:
        console.print("[yellow]· no plan was produced.[/]")
        return
    session.plan_text = plan_text

    # Interactive choice. plan.md-exists changes only the save destination/label.
    exists = (Path(session.repo_path or ".") / "plan.md").exists()
    save_label = ("save to alternate path (existing plan.md found)" if exists
                  else "save to plan.md")
    console.print(f"\n[bold]Plan ready.[/]  [cyan]s[/]={save_label} · "
                  f"[cyan]e[/]xecute · [cyan]b[/]oth · [cyan]d[/]iscard")
    try:
        if reader is not None:
            raw = (reader("choose [s/e/b/d]: ") or "s").strip().lower()
            choice = raw[:1] if raw[:1] in ("s", "e", "b", "d") else "s"
        else:
            from rich.prompt import Prompt
            choice = Prompt.ask("choose", choices=["s", "e", "b", "d"],
                                default="s").lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[yellow]· plan discarded.[/]")
        return

    if choice in ("s", "b"):
        path = _write_plan_file(session, plan_text)
        console.print(f"[green]✓[/] plan written to [cyan]{path}[/]")

    if choice in ("e", "b"):
        if not session.write_enabled:
            session.write_enabled = True
            console.print("[yellow]· enabling write mode to execute the plan "
                          "(/write to toggle).[/]")
        # Seed the runner: ledger goal + the plan rides in extra_context as
        # provenance so the agent keeps following what it just drafted.
        ledger_mod.apply_update(session.session_id,
                                {"goal": objective, "decided": ["Plan drafted via /plan"]})
        session.goal = objective
        session.goal_round = 0
        session.consecutive_crashes = 0
        session.goal_active = True  # main loop picks this up next iteration
    elif choice == "d":
        console.print("[dim]· plan discarded.[/]")


# -- hooks (resume now; compare wired in the compare phase) -----------------


def _make_resume_hook(console: Console, session: ChatSession):
    def _resume(session_id: str) -> None:
        from luxe.chat.resume import list_resumable, resume_into

        if not session_id:
            list_resumable(console)
            return
        resume_into(session_id, session, console)

    return _resume


def _make_compare_hook(console, cfg, repo_path, languages, slots):
    def _compare(task: str) -> None:
        try:
            from luxe.compare.run_pair import interactive_compare
        except Exception:
            console.print("[yellow]compare module unavailable.[/]")
            return
        interactive_compare(task, cfg, repo_path, languages, console=console)

    return _compare


def _make_compare_review_hook(console):
    def _review(compare_id: str) -> None:
        try:
            from luxe.compare.store import review as review_compare
        except Exception:
            console.print("[yellow]compare review unavailable.[/]")
            return
        review_compare(compare_id, console=console)

    return _review


def _make_git_analysis_hook(console, cfg, session: ChatSession, cancel=None):
    """Hook for /gitaudit (and /gitchange) — a read-only gitkit
    report. Targets the SESSION repo, reusing its resident indices (warns if
    HEAD moved); if the session dir isn't a git repo, the runner prompts to
    clone a URL into a local copy and analyzes that, restoring session state."""
    def _git(kind: str, deep: bool | None = None) -> None:
        try:
            from luxe.gitkit import run_git_report
        except Exception:
            console.print("[yellow]gitkit module unavailable.[/]")
            return
        run_git_report(
            kind, cfg=cfg, repo_path=session.repo_path,
            console=console, save=True, expected_head=session.index_head,
            verbose=(session.verbose_level == "full"), cancel=cancel, deep=deep,
        )

    return _git
