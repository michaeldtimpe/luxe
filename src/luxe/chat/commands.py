"""Slash-command parsing + dispatch for the chat REPL.

Commands are decoupled from the loop via a `CommandContext` carrying the
session, the slot manager, the console, and injected hooks for the heavier
features (compare, resume) that the REPL wires in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rich.console import Console

from luxe.chat.session import ChatSession
from luxe.chat.slots import SlotManager

_SLOTS = ("chat", "plan", "code")


@dataclass
class CommandResult:
    handled: bool
    exit: bool = False
    # A message the front-end should run as a turn once the command returns
    # (`/retry`). Kept as data rather than a callback so both front-ends —
    # line REPL and TUI — act on it in their own turn machinery.
    submit: str = ""


@dataclass
class CommandContext:
    console: Console
    session: ChatSession
    slots: SlotManager
    on_compare: Callable[[str], None] | None = None
    on_compare_review: Callable[[str], None] | None = None
    on_resume: Callable[[str], None] | None = None
    on_git_analysis: Callable[[str, "bool | None"], None] | None = None  # (kind, deep) -> run gitkit report
    # (path|None) -> summary dict. Re-resolves the project, rebuilds the index,
    # and moves the repo lock; provided by cli.chat_cmd (it owns the lock).
    on_project: Callable[["str | None"], dict] | None = None
    # The live status-bar snapshot, so commands that invalidate it can say so
    # (`/clear` must clear ctx%/cache — they describe a conversation that's gone).
    status: object | None = None
    # This session's debug-log handle (chat/debuglog.SessionLog). `/ephemeral`
    # needs it: the handler holds debug.log open inside the directory the
    # command is about to remove, so it must be detached first.
    session_log: object | None = None


# (command, args, description) — rendered into an auto-aligned table by _help()
# so every description starts at the same column regardless of command width.
_HELP_ROWS: list[tuple[str, str, str]] = [
    ("/help", "", "show this help"),
    ("/model", "[slot|all] [model_id]", "show slots, or repoint chat|plan|code (all = every slot)"),
    ("/backend", "[name|n]", "list configured oMLX backends, or switch to one"),
    ("/pull", "[repo|name] [--search q] [--yes]", "fetch model weights (mount → HF)"),
    ("/use", "<slot>", "pin the next turn to chat|plan|code"),
    ("/unload", "", "free oMLX RAM (unload resident weights) without quitting"),
    ("/status", "", "session summary: repo, backend, model origin, usage"),
    ("/usage", "[budget <usd>]", "session spend + cap + account credits (billable backends)"),
    ("/tools", "", "list the tools the model has THIS turn (and what's gated)"),
    ("/theme", "[preview|auto|cool|warm|mono]", "preview or switch the colour palette"),
    ("/retry", "", "re-run your last message (e.g. after /write or /ctx)"),
    ("/project", "[path]", "show, attach, or switch the project (re-indexes)"),
    ("/index", "[path]", "build the code-search index for here (or <path>)"),
    ("/doctor", "", "preflight this session: endpoint, model, index, disk, git"),
    ("/outage", "", "print the offline emergency card (OUTAGE.md)"),
    ("/net", "[host]", "layered network report: DNS/TCP/TLS/HTTP + backends"),
    ("/planeproxy", "[status|doctor]", "diagnose the planeproxy SSH tunnel (read-only)"),
    ("/claude", "[status|net|all]", "diagnose Claude Code: billing path, config, sessions"),
    ("/diff", "[--full] [--all]", "what this session changed (git-backed)"),
    ("/export", "[path]", "write the conversation to markdown"),
    ("/full", "", "re-show the last answer without the display cap"),
    ("/copy", "", "copy the last answer to the system clipboard"),
    ("/ctx", "[small|medium|large|xlarge|huge]", "show or set context window size"),
    ("/write", "", "toggle write tools (default: read-only)"),
    ("/bash", "", "toggle unrestricted shell (default: allowlisted)"),
    ("/web", "", "toggle web tools: fetch/render pages + search (default: off)"),
    ("/ephemeral", "[on|off]", "write nothing to disk: no transcript/log/notes (purges this session's)"),
    ("/verbose", "[diff|full|off]", "show full tool I/O (diffs, file contents, results)"),
    ("/reasoning", "[low|medium|high|off|default|show|hide]",
     "reasoning effort (billed per token) + live display of the model's thinking"),
    ("/debug", "", 'toggle "show everything" (verbose full + reasoning)'),
    ("/terse", "", "toggle terse model output (default ON; saves tokens)"),
    ("/compact", "", "toggle compact display (tighter on-screen output ceiling)"),
    ("/goal", "<objective> | stop", "autonomously run rounds until the objective is met"),
    ("/plan", "<objective>", "draft a plan, then choose: save / execute / both"),
    ("/attach", "<path> [...]", "attach file contents to the NEXT turn (one-shot)"),
    ("/sys", "[add <rule>|list|clear]", "manage session-scoped system constraints"),
    ("/memory", "list|add|promote|forget|edit", "manage project memory"),
    ("/init", "[--dry-run]", "draft this repo's brief into .luxe/memory.md"),
    ("/note", "", "bank session working notes into .luxe/memory.md now"),
    ("/gitaudit", "", "audit this repo: orientation + bugs/security + structural advice"),
    ("/gitchange", "", "produce an apply-ready structural change plan for this repo"),
    ("/compare", "<task>", "run two configs side-by-side"),
    ("/compare review", "[id]", "replay a stored comparison"),
    ("/resume", "[id]", "resume a prior session (or list them)"),
    ("/clear", "", "start a fresh conversation"),
    ("/quit", "", "exit (Ctrl-D also works)"),
]

# Handlers deliberately absent from `/help`: short aliases and back-compat
# names. DECLARED, not accidental — `tests/test_chat_commands.py` asserts
# `_build_handlers()` keys ≡ the `_HELP_ROWS` names ∪ this set, so a new
# command that never made it into the help table fails the suite instead of
# shipping invisible. Add a name here only when hiding it is the intent.
_HIDDEN_COMMANDS: frozenset[str] = frozenset({
    "/exit", "/q",                                    # quit aliases
    "/pp",                                            # /planeproxy
    "/cc", "/claude-code",                            # /claude spellings
    "/git-audit", "/gaudit",                          # /gitaudit spellings
    "/git-change", "/gchange",                        # /gitchange spellings
    # pre-2026-06-07 gitkit names, kept working
    "/gitsummary", "/gsum", "/gitreview", "/grev",
    "/gitrefactor", "/gref", "/gitplan", "/gplan",
})


def _degrease(line: str) -> str:
    """Strip a stray leading/trailing backtick-or-two (inline-code paste,
    e.g. `` `/ctx xlarge` `` copied from docs) plus surrounding whitespace.

    A REAL fenced code block opens with three-or-more backticks
    (```` ```python ```` or a bare ```` ``` ````) — that is prose the model
    should see verbatim, not a mis-pasted command, so 3+ leading backticks are
    left completely untouched (2026-08-24: a leading backtick ate `/ctx
    xlarge` mid-failure and the turn re-ran at the old window)."""
    s = line.strip()
    lead = len(s) - len(s.lstrip("`"))
    if lead == 0 or lead >= 3:
        return s
    s = s[lead:].lstrip()
    trail = len(s) - len(s.rstrip("`"))
    if 1 <= trail <= 2:
        s = s[: len(s) - trail].rstrip()
    return s


def is_command(line: str) -> bool:
    """A leading `/` is a command UNLESS the first token is not a known
    command AND looks like a filesystem path (a second `/`, or it exists on
    disk) — pasting `/Users/me/Downloads` used to error as an unknown
    command with no way to send it (2026-07-31). A path-free unknown token
    (`/writ`) still routes to dispatch so typos get the error, not the model."""
    s = _degrease(line)
    if not s.startswith("/"):
        return False
    head = s.split()[0].lower()
    if head in _handlers():
        return True
    import os
    if "/" in head[1:] or os.path.exists(os.path.expanduser(s.split()[0])):
        return False
    return True


_HANDLERS: "dict | None" = None


def _handlers() -> dict:
    """Command registry, built lazily (the handler fns are defined below)."""
    global _HANDLERS
    if _HANDLERS is None:
        _HANDLERS = _build_handlers()
    return _HANDLERS


def dispatch(line: str, ctx: CommandContext) -> CommandResult:
    parts = _degrease(line).split()
    cmd = parts[0].lower()
    args = parts[1:]
    fn = _handlers().get(cmd)
    if fn is None:
        # Under pressure a typo costs a round trip through /help. Name the
        # nearest command instead (difflib, stdlib) before falling back.
        import difflib

        near = difflib.get_close_matches(cmd, list(_handlers()), n=2, cutoff=0.6)
        hint = f" Did you mean {' or '.join(near)}?" if near else ""
        ctx.console.print(f"[yellow]Unknown command {cmd}.{hint} "
                          "Try /help.[/]")
        return CommandResult(handled=True)
    return fn(args, ctx)


def _build_handlers() -> dict:
    # Group modules imported HERE, not at module scope: each one imports
    # CommandContext/CommandResult/_usage back from this module, and the
    # registry is built lazily anyway (`_handlers()`), so the function-local
    # import keeps the dependency one-directional at import time.
    from luxe.chat import (
        cmd_agentic,
        cmd_diag,
        cmd_models,
        cmd_project,
        cmd_toggles,
        cmd_transcript,
    )

    return {
        "/help": _help,
        "/model": cmd_models._model,
        "/backend": cmd_models._backend,
        "/pull": cmd_models._pull,
        "/unload": cmd_models._unload,
        "/status": cmd_diag._status,
        "/usage": cmd_diag._usage_cmd,
        "/tools": cmd_diag._tools,
        "/theme": cmd_toggles._theme,
        "/retry": _retry,
        "/project": cmd_project._project,
        "/index": cmd_project._index_cmd,
        "/doctor": cmd_diag._doctor,
        "/outage": cmd_diag._outage,
        "/init": cmd_project._init,
        "/note": cmd_project._note,
        "/diff": cmd_transcript._diff,
        "/export": cmd_transcript._export,
        "/full": cmd_transcript._full,
        "/use": cmd_toggles._use,
        "/ctx": cmd_toggles._ctx,
        "/write": cmd_toggles._write,
        "/bash": cmd_toggles._bash_mode,
        "/web": cmd_toggles._web_mode,
        "/ephemeral": cmd_toggles._ephemeral,
        "/verbose": cmd_toggles._verbose,
        "/reasoning": cmd_toggles._reasoning,
        "/debug": cmd_toggles._debug,
        "/terse": cmd_toggles._terse,
        "/compact": cmd_toggles._compact_mode,
        "/goal": cmd_agentic._goal,
        "/plan": cmd_agentic._plan,
        "/attach": cmd_agentic._attach,
        "/sys": cmd_agentic._sys,
        "/memory": cmd_project._memory,
        "/gitaudit": cmd_project._gitaudit,
        "/git-audit": cmd_project._gitaudit,
        "/gaudit": cmd_project._gitaudit,
        "/gitchange": cmd_project._gitchange,
        "/git-change": cmd_project._gitchange,
        "/gchange": cmd_project._gitchange,
        # back-compat aliases → the two merged commands
        "/gitsummary": cmd_project._gitaudit, "/gsum": cmd_project._gitaudit,
        "/gitreview": cmd_project._gitaudit, "/grev": cmd_project._gitaudit,
        "/gitrefactor": cmd_project._gitaudit, "/gref": cmd_project._gitaudit,
        "/gitplan": cmd_project._gitchange, "/gplan": cmd_project._gitchange,
        "/compare": cmd_project._compare,
        "/resume": _resume,
        "/clear": _clear,
        "/copy": cmd_transcript._copy,
        "/net": cmd_diag._net,
        "/planeproxy": cmd_diag._planeproxy,
        # short alias (listed under /planeproxy in /help)
        "/pp": cmd_diag._planeproxy,
        "/claude": cmd_diag._claude,
        # short + long aliases (listed under /claude in /help)
        "/cc": cmd_diag._claude,
        "/claude-code": cmd_diag._claude,
        "/quit": _quit,
        "/exit": _quit,   # hidden alias (not listed in /help)
        "/q": _quit,      # hidden quick-exit alias
    }


def _usage(ctx: CommandContext, name: str) -> CommandResult:
    """Print a command's usage line straight out of `_HELP_ROWS`.

    The help table's `args` cell IS the usage string, so a handler that spells
    its own can drift from `/help` without anything noticing. Only for commands
    whose entire usage is that cell — the subcommand-specific lines
    (`/sys add <rule>`, `/memory promote <id>`, `/goal <objective>  ·  /goal
    stop`, …) say something the table cannot express and stay hand-written.
    """
    args = next((a for cmd, a, _ in _HELP_ROWS if cmd == name), "")
    ctx.console.print(f"[yellow]Usage: {name} {args}[/]" if args
                      else f"[yellow]Usage: {name}[/]")
    return CommandResult(handled=True)


def _help(args, ctx: CommandContext) -> CommandResult:
    from rich.markup import escape
    from rich.table import Table

    ctx.console.print("[bold]luxe chat commands[/]")
    # box=None + a sized command column → descriptions line up in one column.
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 0))
    table.add_column("command", no_wrap=True)
    table.add_column("description", overflow="fold")
    for name, cmd_args, desc in _HELP_ROWS:
        sig = f"[cyan]{name}[/]"
        if cmd_args:
            # escape literal []-placeholders so Rich doesn't eat them as markup
            sig += f" [dim]{escape(cmd_args)}[/]"
        table.add_row(sig, escape(desc))
    ctx.console.print(table)
    return CommandResult(handled=True)


def _retry(args, ctx: CommandContext) -> CommandResult:
    """Re-run the last user message — the natural follow-up to `/write`,
    `/ctx`, `/model`, or a turn that failed."""
    last = next((t.user for t in reversed(ctx.session.turns) if t.user), "")
    if not last:
        ctx.console.print("[yellow]Nothing to retry yet.[/]")
        return CommandResult(handled=True)
    preview = last if len(last) <= 60 else last[:59] + "…"
    ctx.console.print(f"[dim]· retrying:[/] {preview}")
    return CommandResult(handled=True, submit=last)


def _resume(args, ctx: CommandContext) -> CommandResult:
    if ctx.on_resume is None:
        ctx.console.print("[yellow]resume unavailable.[/]")
        return CommandResult(handled=True)
    ctx.on_resume(args[0] if args else "")
    return CommandResult(handled=True)


def _clear(args, ctx: CommandContext) -> CommandResult:
    """Start a fresh conversation: drop the turns AND the per-turn status.

    The status bar's `ctx N%` / `cache` describe the conversation that just went
    away; leaving them up made a cleared session look full (reported 2026-07-30).
    The window size and mode flags survive — those are settings, not history.
    """
    ctx.session.turns.clear()
    ctx.session.pinned_slot = None
    ctx.session.attachments.clear()
    reset_turn_status(ctx.status)
    ctx.console.print("[dim]· conversation cleared[/]")
    return CommandResult(handled=True)


def reset_turn_status(status) -> None:
    """Zero the turn-derived status fields (context pressure, resident prompt
    size, timings, step count). Keeps slot/model/num_ctx: they still apply."""
    if status is None:
        return
    for field, value in (("ctx_pressure", 0.0), ("prompt_tokens", 0),
                         ("steps", 0), ("wall_s", 0.0), ("tok_per_s", 0.0),
                         ("has_turn", False)):
        if hasattr(status, field):
            setattr(status, field, value)


def _quit(args, ctx: CommandContext) -> CommandResult:
    return CommandResult(handled=True, exit=True)


# The /attach caps moved to cmd_agentic.py with their handler; they are still
# reachable as `commands.ATTACH_MAX_*` (the tests read them there). Lazy, via
# module __getattr__, because cmd_agentic imports THIS module — a top-level
# import back would make the direction circular.
_REEXPORTED_FROM = {
    "ATTACH_MAX_FILE_BYTES": "cmd_agentic",
    "ATTACH_MAX_TOTAL_BYTES": "cmd_agentic",
}


def __getattr__(name: str):
    mod = _REEXPORTED_FROM.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f"luxe.chat.{mod}"), name)
