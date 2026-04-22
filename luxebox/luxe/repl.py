"""Interactive REPL. Reads prompts, routes, prints responses, logs sessions."""

from __future__ import annotations

import os
import readline  # noqa: F401  — side-effect: enables arrow-key history
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from luxe import prefs, router, runner
from luxe.backend import context_length, list_models, ping
from luxe.registry import LuxeConfig
from luxe.router import RouterDecision
from luxe.session import Session, list_sessions

console = Console()

BANNER = """[bold cyan]luxe[/bold cyan] — local multi-agent CLI
type a prompt, or [dim]/help[/dim] for commands. [dim]Ctrl-D to exit.[/dim]"""

HELP = """[bold]Core[/bold]
  [cyan]/help[/cyan]                      show this message
  [cyan]/agents[/cyan]                    list configured agents
  [cyan]/models[/cyan]                    list available Ollama models
  [cyan]/quit[/cyan]                      exit (session auto-saved)

[bold]Direct dispatch[/bold] (skip the router)
  [cyan]/general[/cyan] | [cyan]/research[/cyan] | [cyan]/writing[/cyan] | [cyan]/image[/cyan] | [cyan]/code[/cyan]  <prompt>

[bold]Turn control[/bold]
  [cyan]/retry[/cyan]                     rerun last prompt with same agent
  [cyan]/redo[/cyan] <agent>              rerun last prompt with a different agent
  [cyan]/model[/cyan] <tag>               one-off model override for the next turn
  [cyan]/pin[/cyan] <text>                prepend a sticky note to every subsequent prompt
  [cyan]/pins[/cyan]                      list current pins
  [cyan]/unpin[/cyan] [n]                 remove pin #n (default: all)
  [cyan]/history[/cyan] [n]               show the last n session events (default 10)

[bold]Sessions[/bold]
  [cyan]/session[/cyan]                   show current session id + path
  [cyan]/save[/cyan] <name>               bookmark current session under <name>
  [cyan]/sessions[/cyan]                  list saved sessions (bookmarks first)
  [cyan]/resume[/cyan] <id-or-name>       switch to another session
  [cyan]/new[/cyan]                       start a fresh session (reset totals + pins)

[bold]Memory & aliases[/bold]
  [cyan]/memory[/cyan]                    open ~/.luxe/memory.md in $EDITOR
  [cyan]/memory view[/cyan]               print current memory
  [cyan]/memory clear[/cyan]              delete memory
  [cyan]/alias add[/cyan] <name> <expansion>
  [cyan]/alias list[/cyan]
  [cyan]/alias remove[/cyan] <name>
"""

BUILTIN_CMDS = {
    "/help", "/agents", "/session", "/models", "/quit", "/exit",
    "/retry", "/redo", "/history", "/model", "/pin", "/pins", "/unpin",
    "/save", "/sessions", "/resume", "/new",
    "/memory", "/alias",
}


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


def start(cfg: LuxeConfig, session: Session | None = None) -> None:
    if not ping():
        console.print(
            "[red]Ollama is not reachable at http://127.0.0.1:11434.[/red] "
            "Start it with [cyan]ollama serve[/cyan]."
        )
        return

    sess = session or Session.new(Path(cfg.session_dir).expanduser())
    state = ReplState(sess=sess)
    console.print(Panel.fit(BANNER, border_style="cyan"))
    console.print(f"[dim]session: {state.sess.session_id}[/dim]")

    while True:
        try:
            line = input("\nluxe> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return
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
        _run_routed(prompt_with_pins, state, cfg, original_prompt=line)


def _handle_command(line: str, state: ReplState, cfg: LuxeConfig) -> str:
    """Return 'consumed', 'exit', 'dispatch_direct', or 'fallthrough'."""
    parts = shlex.split(line) if "'" in line or '"' in line else line.split()
    cmd = parts[0]
    args = parts[1:]

    if cmd == "/help":
        console.print(HELP)
        return "consumed"
    if cmd == "/agents":
        for a in cfg.agents:
            mark = "x" if a.enabled else " "
            console.print(f"  \\[{mark}] {a.name:10s} {a.model:30s} {a.display}")
        return "consumed"
    if cmd == "/session":
        console.print(f"  id:   {state.sess.session_id}")
        console.print(f"  path: {state.sess.path}")
        return "consumed"
    if cmd == "/models":
        for m in list_models():
            console.print(f"  {m}")
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
            console.print(
                f"[yellow]usage:[/yellow] /{agent_name} <prompt>"
            )
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
            return input("> ").strip()
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
    reasoning = f" [dim]({decision.reasoning})[/dim]" if decision.reasoning else ""
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

    _print_stats(decision, result, state, cfg)


def _print_stats(decision, result, state: ReplState, cfg: LuxeConfig) -> None:
    ctx_total = context_length(state.last_model, cfg.ollama_base_url)
    used = state.last_ctx_used
    free = max(ctx_total - used, 0)
    pct_free = (free / ctx_total * 100.0) if ctx_total else 100.0

    turn = (
        f"[dim]{decision.agent} · {result.wall_s:.1f}s · "
        f"{result.prompt_tokens}↑ {result.completion_tokens}↓ tokens · "
        f"{result.steps_taken} steps · {result.tool_calls_total} tool calls[/dim]"
    )
    ctx_line = (
        f"[dim]ctx: {used:,}/{ctx_total:,} ({pct_free:.0f}% free) · "
        f"{state.last_model}[/dim]"
    )
    totals = (
        f"[dim]session totals: {state.turns} turns · "
        f"{state.total_wall_s:.1f}s · "
        f"{state.total_prompt_tokens:,}↑ {state.total_completion_tokens:,}↓ tokens[/dim]"
    )
    console.print(turn)
    console.print(ctx_line)
    console.print(totals)


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


def _apply_pins(prompt: str, pins: list[str]) -> str:
    if not pins:
        return prompt
    block = "\n".join(f"- {p}" for p in pins)
    return f"[Pinned context]\n{block}\n\n{prompt}"


def _expand_alias(line: str) -> str:
    if not line.startswith("/"):
        return line
    head, _, rest = line[1:].partition(" ")
    if head in BUILTIN_CMDS_NAMES:
        return line
    aliases = prefs.load_aliases()
    if head not in aliases:
        return line
    expansion = aliases[head]
    return f"{expansion} {rest}".strip() if rest else expansion


BUILTIN_CMDS_NAMES = {c.lstrip("/") for c in BUILTIN_CMDS} | {
    "general", "research", "writing", "image", "code",
}


def _handle_alias(args: list[str]) -> None:
    if not args:
        console.print("[yellow]usage:[/yellow] /alias add|list|remove ...")
        return
    sub = args[0]
    if sub == "list":
        aliases = prefs.load_aliases()
        if not aliases:
            console.print("[dim]no aliases[/dim]")
            return
        width = max(len(k) for k in aliases)
        for k, v in sorted(aliases.items()):
            console.print(f"  [cyan]/{k:<{width}}[/cyan]  {v}")
        return
    if sub == "add":
        if len(args) < 3:
            console.print("[yellow]usage:[/yellow] /alias add <name> <expansion>")
            return
        name = args[1]
        if name in BUILTIN_CMDS_NAMES:
            console.print(f"[yellow]cannot shadow builtin:[/yellow] /{name}")
            return
        expansion = " ".join(args[2:])
        prefs.save_alias(name, expansion)
        console.print(f"[dim]alias /{name} →[/dim] {expansion}")
        return
    if sub == "remove":
        if len(args) < 2:
            console.print("[yellow]usage:[/yellow] /alias remove <name>")
            return
        if prefs.remove_alias(args[1]):
            console.print(f"[dim]removed alias[/dim] /{args[1]}")
        else:
            console.print(f"[yellow]no such alias:[/yellow] /{args[1]}")
        return
    console.print(f"[yellow]unknown alias subcommand:[/yellow] {sub}")


def _edit_memory() -> None:
    prefs._ensure_dir()
    if not prefs.MEMORY_FILE.exists():
        prefs.MEMORY_FILE.write_text(
            "# luxe memory\n\nGuidance injected into every specialist's system prompt.\n"
        )
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    try:
        subprocess.run([editor, str(prefs.MEMORY_FILE)], check=False)
    except FileNotFoundError:
        console.print(f"[yellow]$EDITOR not found:[/yellow] {editor}")
        return
    size = prefs.MEMORY_FILE.stat().st_size
    console.print(f"[dim]memory: {size} bytes at {prefs.MEMORY_FILE}[/dim]")


def _print_history(sess: Session, n: int) -> None:
    events = sess.read_all()
    if not events:
        console.print("[dim]no history yet[/dim]")
        return
    for e in events[-n:]:
        role = e.get("role", "?")
        agent = e.get("agent", "")
        text = str(e.get("content") or e.get("tool") or "")
        if len(text) > 200:
            text = text[:200] + "…"
        console.print(f"  [dim]{role}/{agent}:[/dim] {text}")


def _print_last_events(sess: Session, n: int) -> None:
    events = sess.read_all()
    if not events:
        return
    console.print(f"[dim]last {min(n, len(events))} events:[/dim]")
    for e in events[-n:]:
        role = e.get("role", "?")
        agent = e.get("agent", "")
        text = str(e.get("content") or e.get("tool") or "")
        if len(text) > 110:
            text = text[:110] + "…"
        console.print(f"  [dim]{role}/{agent}:[/dim] {text}")


def _print_sessions(cfg: LuxeConfig) -> None:
    root = Path(cfg.session_dir).expanduser()
    sessions = list_sessions(root)
    bookmarks = prefs.load_bookmarks()
    inv = {v: k for k, v in bookmarks.items()}

    if bookmarks:
        console.print("[bold]Bookmarks[/bold]")
        for name, sid in sorted(bookmarks.items()):
            p = root / f"{sid}.jsonl"
            mark = " " if p.exists() else "!"
            console.print(f"  [cyan]{name:<20s}[/cyan] {sid} {mark}")

    if not sessions:
        if not bookmarks:
            console.print("[yellow]no sessions yet[/yellow]")
        return
    console.print("[bold]Recent sessions[/bold]")
    for p in sessions[:15]:
        sid = p.stem
        label = inv.get(sid, "")
        suffix = f"   [dim]({label})[/dim]" if label else ""
        console.print(f"  {sid}{suffix}")


def _render_images(text: str) -> None:
    """If the assistant mentioned a PNG path, render it as a clickable file:// link."""
    import re
    for m in re.finditer(r"(/\S+\.png)", text):
        p = Path(m.group(1))
        if p.exists():
            console.print(f"  [dim]↗ file://{p}[/dim]")
