"""Interactive REPL. Reads prompts, routes, prints responses, logs sessions."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from luxe_cli import prefs, router, runner
from luxe_cli.backend import (
    context_length,
    parameter_size,
    prewarm as _prewarm,
)
from luxe_cli.registry import LuxeConfig
from luxe_cli.router import RouterDecision
from luxe_cli.session import Session

console = Console()


from luxe_cli.repl.aliases import (
    BUILTIN_CMDS_NAMES,
    _apply_pins,
    _edit_memory,
    _expand_alias,
    _handle_alias,
    _print_history,
    _print_last_events,
    _print_sessions,
    _render_images,
)
from luxe_cli.repl.help import BUILTIN_CMDS, _HELP_SECTIONS, _render_help  # noqa: F401
from luxe_cli.repl.models import (
    _print_variants,
    _pull_with_progress,
    _prompt_assign_to_agent,
)
from luxe_cli.repl.prompt import (
    _ask_styled,
    _make_prompt_session,
    _prompt_message,
)
from luxe_cli.repl.review import _start_review
from luxe_cli.repl.status import (
    _GIT_HASH,
    _GIT_HASH_AT_START,
    _current_head_hash,
    _fmt_wall,
    _git_short_hash,
    _handle_tools_command,
    _print_status_banner,
    _show_context_info,
)
from luxe_cli.repl.tasks import (
    _print_launch_hints,
    _tasks_abort,
    _tasks_analyze,
    _tasks_list_recent,
    _tasks_log,
    _tasks_resume,
    _tasks_run_background,
    _tasks_run_sync,
    _tasks_save,
    _tasks_status,
    _tasks_tail,
    _tasks_watch,
)


@dataclass
class ReplState:
    sess: Session
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_wall_s: float = 0.0
    turns: int = 0
    last_prompt: str = ""
    last_agent: str = ""
    pending_model: str | None = None  # consumed on next dispatch
    pins: list[str] = field(default_factory=list)
    last_ctx_used: int = 0
    last_model: str = ""
    last_endpoint: str = ""
    sticky_agent: str = ""  # non-empty → skip router, send plain prompts here
    param_override: str | None = None  # user-forced param string for banner display

    def reset_for_new_session(self, sess: Session) -> None:
        self.sess = sess
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_wall_s = 0.0
        self.turns = 0
        self.last_prompt = ""
        self.last_agent = ""
        self.pending_model = None
        self.pins.clear()
        self.last_ctx_used = 0
        self.last_model = ""
        self.last_endpoint = ""
        self.sticky_agent = ""
        self.param_override = None


def _check_provider_health(cfg: LuxeConfig) -> dict:
    """Ping every distinct base_url any enabled agent might use.

    Returns {"reachable": [(url, kind), ...], "unreachable": [...]}
    so callers can decide whether to bail (nothing reachable) or warn
    (some up, some down).
    """
    from luxe_cli.backend import _kind_for_url
    from luxe_cli.providers import get_provider

    seen: dict[str, str] = {}
    for agent in cfg.agents:
        if not agent.enabled:
            continue
        url = cfg.resolve_endpoint(agent)
        seen.setdefault(url, _kind_for_url(url))

    reachable: list[tuple[str, str]] = []
    unreachable: list[tuple[str, str]] = []
    for url, kind in seen.items():
        if get_provider(kind, url).ping():
            reachable.append((url, kind))
        else:
            unreachable.append((url, kind))
    return {"reachable": reachable, "unreachable": unreachable}


def start(cfg: LuxeConfig, session: Session | None = None) -> None:
    health = _check_provider_health(cfg)
    if not health["reachable"]:
        # Every provider any enabled agent could use is down. Bail.
        console.print(
            "[red]No backend provider is reachable.[/red] "
            "Start one of:"
        )
        for url, kind in health["unreachable"]:
            console.print(f"  [red]✗[/red] [cyan]{kind}[/cyan] at {url}")
        return
    for url, kind in health["unreachable"]:
        # Some providers are down but at least one works — warn and proceed.
        console.print(
            f"[yellow][!] {kind} at {url} unreachable — agents pointing at it "
            "will fail on dispatch.[/yellow]"
        )

    sess = session or Session.new(Path(cfg.session_dir).expanduser())
    state = ReplState(sess=sess)
    console.print(f"[dim]session: {state.sess.session_id}[/dim] "
                  f"[dim]· /help for commands · ↑/↓ history · Alt-Enter newline · Ctrl-D exit[/dim]")

    prompt_session = _make_prompt_session()

    while True:
        console.print()
        _print_status_banner(state, cfg)
        try:
            raw = prompt_session.prompt(_prompt_message(state.sticky_agent))
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return
        line = raw.strip()
        if not line:
            continue

        expanded = _expand_alias(line)
        if expanded != line:
            console.print(f"[dim]alias → {expanded}[/dim]")
            line = expanded

        if line in ("/quit", "/exit"):
            console.print("[dim]bye[/dim]")
            return

        if line.startswith("/"):
            handled = _handle_command(line, state, cfg)
            if handled == "consumed":
                continue
            if handled == "exit":
                return
            if handled == "dispatch_direct":
                # state.pending_* filled by handler
                prompt = state._pending_prompt  # type: ignore[attr-defined]
                agent = state._pending_agent  # type: ignore[attr-defined]
                del state._pending_prompt  # type: ignore[attr-defined]
                del state._pending_agent  # type: ignore[attr-defined]
                _run_direct(prompt, agent, state, cfg)
                continue
            # fall through → treat as normal prompt (unknown slash already handled)

        prompt_with_pins = _apply_pins(line, state.pins)
        if state.sticky_agent:
            _run_direct(prompt_with_pins, state.sticky_agent, state, cfg)
        else:
            _run_routed(prompt_with_pins, state, cfg, original_prompt=line)


def _handle_command(line: str, state: ReplState, cfg: LuxeConfig) -> str:
    """Return 'consumed', 'exit', 'dispatch_direct', or 'fallthrough'."""
    # Most commands hand the remainder of the line off as free-form prose
    # (e.g. /tasks <goal>, /review <url>, /pin <text>). Plain whitespace
    # split is safe there. Only fall back to shlex when it actually helps
    # — and if that raises (unbalanced quotes in user prose like "it's"),
    # use plain split rather than crash.
    if "'" in line or '"' in line:
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
    else:
        parts = line.split()
    if not parts:
        return "consumed"
    cmd = parts[0]
    args = parts[1:]

    if cmd == "/help":
        console.print(_render_help())
        return "consumed"
    if cmd == "/agents":
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column()  # enabled mark
        t.add_column()  # agent name
        t.add_column()  # model tag
        t.add_column(justify="right")  # params
        t.add_column()  # display label
        for a in cfg.agents:
            mark = f"\\[{'x' if a.enabled else ' '}]"
            endpoint = cfg.resolve_endpoint(a)
            params = parameter_size(a.model, endpoint)
            t.add_row(mark, a.name, a.model, params, a.display)
        console.print(t)
        return "consumed"
    if cmd == "/session":
        console.print(f"  id:   {state.sess.session_id}")
        console.print(f"  path: {state.sess.path}")
        return "consumed"
    if cmd == "/models":
        from luxe_cli.repl.models import _default_provider as _picker_provider

        provider = _picker_provider(cfg)
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column()  # model tag
        t.add_column(justify="right")  # params
        for m in provider.list_models():
            t.add_row(m, provider.parameter_size(m) or "—")
        console.print(t)
        return "consumed"

    if cmd == "/variants":
        _print_variants(args[0] if args else None, cfg)
        return "consumed"

    if cmd == "/pull":
        if not args:
            console.print("[yellow]usage:[/yellow] /pull <tag>   e.g. /pull gemma3:4b")
            return "consumed"
        from luxe_cli.repl.models import _default_provider as _picker_provider

        provider = _picker_provider(cfg)
        if provider.name != "ollama":
            console.print(
                f"[yellow]/pull is Ollama-only[/yellow] — active provider is "
                f"[cyan]{provider.name}[/cyan]. Manage models in the LM Studio "
                "GUI or use Ollama directly."
            )
            return "consumed"
        tag = args[0]
        if _pull_with_progress(tag, provider.base_url):
            _prompt_assign_to_agent(tag, cfg)
        return "consumed"

    if cmd == "/context":
        _show_context_info(state, cfg)
        return "consumed"

    if cmd == "/tools":
        _handle_tools_command(args)
        return "consumed"

    if cmd == "/review":
        if not args:
            console.print("[yellow]usage:[/yellow] /review <git-url>")
            return "consumed"
        _start_review(args[0], "review", state, cfg)
        return "consumed"

    if cmd == "/refactor":
        if not args:
            console.print("[yellow]usage:[/yellow] /refactor <git-url>")
            return "consumed"
        _start_review(args[0], "refactor", state, cfg)
        return "consumed"

    if cmd == "/tasks":
        known_subs = ("status", "log", "abort", "save", "tail", "watch", "analyze", "resume")
        sub = args[0] if args else ""
        if sub == "":
            _tasks_list_recent()
            return "consumed"
        if sub == "status":
            _tasks_status(args[1] if len(args) > 1 else None)
            return "consumed"
        if sub == "log":
            _tasks_log(args[1] if len(args) > 1 else None)
            return "consumed"
        if sub == "abort":
            _tasks_abort(args[1] if len(args) > 1 else None)
            return "consumed"
        if sub == "save":
            _tasks_save(args[1] if len(args) > 1 else None)
            return "consumed"
        if sub == "tail":
            tail_args = [a for a in args[1:] if a not in ("-v", "--verbose")]
            verbose = any(a in ("-v", "--verbose") for a in args[1:])
            _tasks_tail(
                tail_args[0] if tail_args else None,
                verbose=verbose,
                state=state,
                cfg=cfg,
            )
            return "consumed"
        if sub == "watch":
            _tasks_watch(args[1] if len(args) > 1 else None)
            return "consumed"
        if sub == "analyze":
            _tasks_analyze(args[1] if len(args) > 1 else None)
            return "consumed"
        if sub == "resume":
            _tasks_resume(args[1] if len(args) > 1 else None)
            return "consumed"
        # Typo guard: if the first arg isn't a known subcommand AND the
        # next token looks like a task id (T-YYYYMMDDT…), the user
        # almost certainly mistyped a subcommand — don't silently
        # interpret `/tasks tails T-…` as a new goal.
        if len(args) >= 2 and re.match(r"^T-\d{8}T\d{6}-", args[1]):
            import difflib
            close = difflib.get_close_matches(sub, known_subs, n=1, cutoff=0.6)
            hint = f" Did you mean [cyan]/tasks {close[0]} {args[1]}[/cyan]?" if close else ""
            console.print(
                f"[yellow]unknown /tasks subcommand:[/yellow] {sub}.{hint}\n"
                f"[dim]valid subcommands: {', '.join(known_subs)}[/dim]"
            )
            return "consumed"
        # Parse --sync flag then treat the rest as the goal.
        raw = line[len("/tasks "):].strip()
        run_sync = False
        if raw.startswith("--sync"):
            run_sync = True
            raw = raw[len("--sync"):].strip()
        if not raw:
            console.print("[yellow]usage:[/yellow] /tasks [--sync] <goal> | status [id] | log [id] | abort [id]")
            return "consumed"
        if run_sync:
            _tasks_run_sync(raw, state, cfg)
        else:
            _tasks_run_background(raw, state, cfg)
        return "consumed"

    if cmd == "/history":
        n = int(args[0]) if args and args[0].isdigit() else 10
        _print_history(state.sess, n)
        return "consumed"

    if cmd == "/retry":
        if not state.last_prompt:
            console.print("[yellow]nothing to retry yet[/yellow]")
            return "consumed"
        state._pending_prompt = state.last_prompt  # type: ignore[attr-defined]
        state._pending_agent = state.last_agent or "general"  # type: ignore[attr-defined]
        return "dispatch_direct"

    if cmd == "/redo":
        if not state.last_prompt:
            console.print("[yellow]nothing to redo yet[/yellow]")
            return "consumed"
        if not args:
            console.print("[yellow]usage:[/yellow] /redo <agent>")
            return "consumed"
        agent = args[0]
        if not _is_valid_agent(agent, cfg):
            console.print(f"[yellow]unknown agent:[/yellow] {agent}")
            return "consumed"
        state._pending_prompt = state.last_prompt  # type: ignore[attr-defined]
        state._pending_agent = agent  # type: ignore[attr-defined]
        return "dispatch_direct"

    if cmd == "/model":
        if not args:
            console.print(
                f"[yellow]usage:[/yellow] /model <tag>   "
                f"current pending: {state.pending_model or 'none'}"
            )
            return "consumed"
        state.pending_model = args[0]
        console.print(f"[dim]next turn will use model[/dim] [cyan]{state.pending_model}[/cyan]")
        return "consumed"

    if cmd == "/params":
        if not args:
            current = state.param_override or "(auto-detected)"
            console.print(f"[dim]banner params override:[/dim] {current}")
            return "consumed"
        if args[0].lower() == "clear":
            state.param_override = None
            console.print("[dim]params override cleared (back to auto-detect)[/dim]")
            return "consumed"
        state.param_override = " ".join(args)
        console.print(f"[dim]params forced to[/dim] [cyan]{state.param_override}[/cyan]")
        return "consumed"

    if cmd == "/pin":
        text = line[len("/pin"):].strip()
        if not text:
            console.print("[yellow]usage:[/yellow] /pin <text>")
            return "consumed"
        state.pins.append(text)
        console.print(f"[dim]pinned #{len(state.pins)}:[/dim] {text}")
        return "consumed"
    if cmd == "/pins":
        if not state.pins:
            console.print("[dim]no pins[/dim]")
        else:
            for i, p in enumerate(state.pins, 1):
                console.print(f"  [dim]{i}.[/dim] {p}")
        return "consumed"
    if cmd == "/unpin":
        if not state.pins:
            console.print("[dim]no pins[/dim]")
            return "consumed"
        if not args:
            state.pins.clear()
            console.print("[dim]all pins cleared[/dim]")
            return "consumed"
        try:
            idx = int(args[0]) - 1
            removed = state.pins.pop(idx)
            console.print(f"[dim]removed:[/dim] {removed}")
        except (ValueError, IndexError):
            console.print(f"[yellow]invalid pin index:[/yellow] {args[0]}")
        return "consumed"

    if cmd == "/save":
        if not args:
            console.print("[yellow]usage:[/yellow] /save <name>")
            return "consumed"
        prefs.save_bookmark(args[0], state.sess.session_id)
        console.print(f"[dim]saved bookmark[/dim] [cyan]{args[0]}[/cyan] → {state.sess.session_id}")
        return "consumed"

    if cmd == "/sessions":
        _print_sessions(cfg)
        return "consumed"

    if cmd == "/resume":
        if not args:
            console.print("[yellow]usage:[/yellow] /resume <id-or-name>")
            return "consumed"
        root = Path(cfg.session_dir).expanduser()
        target = prefs.resolve_session_key(args[0], root)
        if not target:
            console.print(f"[yellow]no session matches[/yellow] {args[0]}")
            return "consumed"
        new_sess = Session.load(target)
        state.reset_for_new_session(new_sess)
        console.print(f"[dim]resumed[/dim] [cyan]{new_sess.session_id}[/cyan]")
        _print_last_events(new_sess, 4)
        return "consumed"

    if cmd == "/new":
        root = Path(cfg.session_dir).expanduser()
        new_sess = Session.new(root)
        state.reset_for_new_session(new_sess)
        console.print(f"[dim]new session[/dim] [cyan]{new_sess.session_id}[/cyan]")
        return "consumed"

    if cmd == "/clear":
        if state.sticky_agent:
            console.print(f"[dim]cleared sticky agent[/dim] ({state.sticky_agent})")
            state.sticky_agent = ""
        else:
            console.print("[dim]no sticky agent set[/dim]")
        return "consumed"

    if cmd == "/memory":
        sub = args[0] if args else ""
        if sub == "view":
            mem = prefs.load_memory()
            console.print(mem or "[dim](empty)[/dim]")
        elif sub == "clear":
            prefs.clear_memory()
            console.print("[dim]memory cleared[/dim]")
        else:
            _edit_memory()
        return "consumed"

    if cmd == "/alias":
        _handle_alias(args)
        return "consumed"

    # Direct-dispatch flag? (/general, /research, /writing, /image, /code)
    direct = _parse_direct_dispatch(line, cfg)
    if direct is not None:
        agent_name, task = direct
        if not task:
            # Bare `/writing` → make it sticky and pre-warm the model so the
            # banner reflects the new mode and the first real prompt is fast.
            state.sticky_agent = agent_name
            agent_cfg = cfg.get(agent_name)
            model = state.pending_model or agent_cfg.model
            endpoint = cfg.resolve_endpoint(agent_cfg)
            with console.status(f"[dim]loading {model}...[/dim]", spinner="dots"):
                _prewarm(model, endpoint)
            console.print(f"[dim]→ sticky agent set to[/dim] [cyan]{agent_name}[/cyan]")
            # Banner will re-render on the next prompt with the new mode/model.
            return "consumed"
        state._pending_prompt = task  # type: ignore[attr-defined]
        state._pending_agent = agent_name  # type: ignore[attr-defined]
        return "dispatch_direct"

    console.print(f"[yellow]unknown command:[/yellow] {cmd} — try /help")
    return "consumed"


def _run_direct(prompt: str, agent: str, state: ReplState, cfg: LuxeConfig) -> None:
    prompt_with_pins = _apply_pins(prompt, state.pins)
    if state.sess:
        state.sess.append({"role": "user", "agent": agent, "content": prompt_with_pins})
    decision = RouterDecision(
        agent=agent, task=prompt_with_pins, reasoning=f"direct /{agent} flag"
    )
    _dispatch(decision, state, cfg, original_prompt=prompt)


def _run_routed(
    prompt_with_pins: str,
    state: ReplState,
    cfg: LuxeConfig,
    *,
    original_prompt: str,
) -> None:
    def ask(q: str) -> str:
        console.print(f"[yellow]router asks:[/yellow] {q}")
        try:
            return _ask_styled("  answer")
        except (EOFError, KeyboardInterrupt):
            return ""

    try:
        with console.status("[dim]routing...[/dim]", spinner="dots"):
            decision = router.route(prompt_with_pins, cfg, ask_fn=ask, session=state.sess)
    except KeyboardInterrupt:
        console.print("[yellow]⚠ router interrupted — returning to prompt[/yellow]")
        return

    _dispatch(decision, state, cfg, original_prompt=original_prompt)


def _dispatch(
    decision: RouterDecision,
    state: ReplState,
    cfg: LuxeConfig,
    *,
    original_prompt: str,
) -> None:
    # Surface a router error visibly (yellow) rather than hiding it in the
    # usual dim reasoning text — it's often the first hint Ollama or the
    # router model is unreachable.
    if decision.reasoning.startswith("router error"):
        reasoning = f" [yellow]({decision.reasoning})[/yellow]"
    elif decision.reasoning:
        reasoning = f" [dim]({decision.reasoning})[/dim]"
    else:
        reasoning = ""
    model_note = ""
    if state.pending_model:
        model_note = f" [dim]· override →[/dim] [cyan]{state.pending_model}[/cyan]"
    console.print(
        f"[dim]→ routed to[/dim] [bold cyan]{decision.agent}[/bold cyan]{reasoning}{model_note}"
    )

    override = state.pending_model
    state.pending_model = None  # consume whether or not the call succeeds

    try:
        with console.status(f"[cyan]{decision.agent}[/cyan] working...", spinner="dots"):
            result = runner.dispatch(decision, cfg, session=state.sess, model_override=override)
    except KeyboardInterrupt:
        console.print("[yellow]⚠ interrupted — returning to prompt[/yellow]")
        return

    if result.aborted:
        console.print(f"[yellow]⚠ aborted:[/yellow] {result.abort_reason}")
    if result.final_text:
        console.print(Markdown(result.final_text))
    if decision.agent == "image":
        _render_images(result.final_text)

    # Update session totals / last-turn state
    state.last_prompt = original_prompt
    state.last_agent = decision.agent
    state.total_prompt_tokens += result.prompt_tokens
    state.total_completion_tokens += result.completion_tokens
    state.total_wall_s += result.wall_s
    state.turns += 1
    state.last_ctx_used = result.prompt_tokens
    state.last_model = override or cfg.get(decision.agent).model
    state.last_endpoint = cfg.resolve_endpoint(cfg.get(decision.agent))
    state.sticky_agent = decision.agent

    _print_stats(decision, result, state, cfg)


def _resolve_ctx_total(state: ReplState, cfg: LuxeConfig) -> int:
    """Pick the right ctx denominator for the totals/snapshot lines.

    Prefer the agent's explicit `num_ctx` override from agents.yaml,
    fall back to backend introspection. Server-side metadata is the
    only authoritative source for stock Ollama tags but isn't returned
    by oMLX (which would otherwise show the hardcoded 8192 default
    even for an agent configured with num_ctx: 32768)."""
    agent_name = state.last_agent or state.sticky_agent
    if agent_name:
        try:
            num_ctx = cfg.get(agent_name).num_ctx
            if num_ctx:
                return int(num_ctx)
        except KeyError:
            pass
    return context_length(state.last_model, state.last_endpoint)


def _print_stats(decision, result, state: ReplState, cfg: LuxeConfig) -> None:
    ctx_total = _resolve_ctx_total(state, cfg)
    used = state.last_ctx_used
    free = max(ctx_total - used, 0)
    pct_free = (free / ctx_total * 100.0) if ctx_total else 100.0

    # True decode rate uses pure model time, not total wall. Tool round-
    # trips (web fetch, bash, file I/O) can dominate wall_s and made the
    # old `completion / wall_s` ratio look misleadingly slow (e.g. "2
    # tok/s" for a research turn where actual decode was ~17 tok/s but
    # most of the wall was HTTP + context prefill).
    model_s = result.model_wall_s or result.wall_s
    tool_wait_s = max(0.0, result.wall_s - model_s) if result.model_wall_s else 0.0
    tok_per_s = (
        result.completion_tokens / model_s
        if model_s > 0 and result.completion_tokens > 0
        else 0.0
    )
    rate = f" · [dim]{tok_per_s:.0f} tok/s decode[/dim]" if tok_per_s else ""
    tool_wait = (
        f" [dim][tools: {_fmt_wall(tool_wait_s)}][/dim]"
        if tool_wait_s >= 1.0
        else ""
    )
    turn = (
        f"[dim]{decision.agent} · {_fmt_wall(result.wall_s)} · "
        f"{result.prompt_tokens}↑ {result.completion_tokens}↓ tokens · "
        f"{result.steps_taken} steps · {result.tool_calls_total} tool calls[/dim]"
        f"{rate}{tool_wait}"
    )
    ctx_line = (
        f"[dim]ctx: {used:,}/{ctx_total:,} ({pct_free:.0f}% free) · "
        f"{state.last_model}[/dim]"
    )
    totals = (
        f"[dim]session totals: {state.turns} turns · "
        f"{_fmt_wall(state.total_wall_s)} · "
        f"{state.total_prompt_tokens:,}↑ {state.total_completion_tokens:,}↓ tokens[/dim]"
    )
    console.print(turn)
    console.print(ctx_line)
    console.print(totals)
    if result.near_cap_turns:
        cap = cfg.get(decision.agent).max_tokens_per_turn
        console.print(
            f"[yellow]⚠ {result.near_cap_turns} turn(s) used ≥80% of "
            f"max_tokens_per_turn ({cap})[/yellow] [dim]— output may be "
            f"truncated; raise this agent's budget in agents.yaml if it "
            f"keeps happening[/dim]"
        )


# ── Helpers ─────────────────────────────────────────────────────────────

def _parse_direct_dispatch(line: str, cfg: LuxeConfig) -> tuple[str, str] | None:
    if not line.startswith("/"):
        return None
    head, _, rest = line[1:].partition(" ")
    for a in cfg.agents:
        if a.name == head and a.name != "router" and a.enabled:
            return a.name, rest.strip()
    return None


def _is_valid_agent(name: str, cfg: LuxeConfig) -> bool:
    return any(a.name == name and a.enabled and a.name != "router" for a in cfg.agents)
