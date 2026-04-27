"""Task management — list, resolve, status, watch, tail, log, save, abort, run."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from luxe_cli.registry import LuxeConfig

if TYPE_CHECKING:
    from luxe_cli.repl.core import ReplState

console = Console()


def _tasks_list_recent() -> None:
    from luxe_cli.tasks import list_all as _list_all
    tasks = _list_all(limit=15)
    if not tasks:
        console.print("[dim]no tasks yet — /tasks <goal> to start one[/dim]")
        return
    t = Table(show_header=True, box=None, padding=(0, 2), header_style="dim")
    t.add_column("id")
    t.add_column("status")
    t.add_column("subtasks", justify="right")
    t.add_column("created")
    t.add_column("goal")
    for task in tasks:
        done = sum(1 for s in task.subtasks if s.status == "done")
        total = len(task.subtasks)
        goal_preview = task.goal[:60] + ("…" if len(task.goal) > 60 else "")
        # Reconcile stale "running" entries: if state says running but the
        # subprocess is gone, surface it as "stalled" so the user notices.
        status = task.status
        if status == "running" and task.pid and not task.is_alive():
            status = "stalled"
        if status == "running" and task.is_alive():
            status_text = "[cyan]running ●[/cyan]"
        else:
            color = {
                "done": "green", "blocked": "yellow", "stalled": "red",
                "aborted": "red", "planning": "dim",
            }.get(status, "white")
            status_text = f"[{color}]{status}[/{color}]"
        t.add_row(
            task.id,
            status_text,
            f"{done}/{total}",
            task.created_at[:19].replace("T", " "),
            goal_preview,
        )
    console.print(t)


def _tasks_resolve(partial: str | None):
    """Accept a full or prefix id; if None, default to most-recent task."""
    from luxe_cli.tasks import list_all as _list_all
    from luxe_cli.tasks.model import resolve_partial as _resolve
    if partial:
        t = _resolve(partial)
        if not t:
            console.print(f"[yellow]no task matches prefix[/yellow] {partial}")
            return None
        return t
    all_tasks = _list_all(limit=1)
    if not all_tasks:
        console.print("[dim]no tasks yet[/dim]")
        return None
    return all_tasks[0]


def _status_renderable(task) -> Group:
    """Build the status view as a Rich Group so both `/tasks status`
    (one-shot print) and `/tasks watch` (Live auto-refresh) share the
    exact same layout."""
    from luxe_cli.repl.status import _fmt_wall
    status = task.status
    live_hint = ""
    if status == "running":
        if task.is_alive():
            live_hint = f" [cyan]● pid {task.pid}[/cyan]"
        elif task.pid:
            status = "stalled"
            live_hint = " [red](subprocess died without updating state)[/red]"

    header = Text.from_markup(
        f"[bold cyan]{task.id}[/bold cyan] [dim]·[/dim] {status}{live_hint} "
        f"[dim]· created[/dim] {task.created_at[:19].replace('T', ' ')}"
    )
    goal = Text.from_markup(f"[dim]goal:[/dim] {task.goal}")
    blocks: list = [header, goal]
    if task.completed_at:
        blocks.append(Text.from_markup(
            f"[dim]finished:[/dim] {task.completed_at[:19].replace('T', ' ')}"
        ))

    t = Table(show_header=True, box=None, padding=(0, 2), header_style="dim")
    t.add_column("sub")
    t.add_column("status")
    t.add_column("agent")
    t.add_column("tools", justify="right")
    t.add_column("wall", justify="right")
    t.add_column("title")
    for s in task.subtasks:
        icon = {
            "done": "[green]✓[/green]", "blocked": "[yellow]⚠[/yellow]",
            "pending": "[dim]○[/dim]", "running": "[cyan]►[/cyan]",
            "skipped": "[dim]–[/dim]",
        }.get(s.status, "?")
        t.add_row(
            s.id.rsplit(".", 1)[-1],
            f"{icon} {s.status}",
            s.agent or "[dim]—[/dim]",
            str(s.tool_calls_total),
            _fmt_wall(s.wall_s) if s.wall_s else "[dim]—[/dim]",
            s.title[:60] + ("…" if len(s.title) > 60 else ""),
        )
    blocks.append(t)
    for s in task.subtasks:
        if s.error:
            blocks.append(Text.from_markup(f"  [yellow]{s.id} error:[/yellow] {s.error}"))
    return Group(*blocks)


def _tasks_status(partial: str | None) -> None:
    task = _tasks_resolve(partial)
    if not task:
        return
    console.print(_status_renderable(task))


def _tasks_watch(partial: str | None) -> None:
    """Auto-refreshing dashboard view of a task's status table. Polls
    state.json ~4× per second, auto-exits when the task reaches a
    finished status (done/blocked/aborted). Ctrl-C leaves the final
    view on screen rather than clearing it."""
    import time as _time
    from luxe_cli.tasks import load as _load_task

    task = _tasks_resolve(partial)
    if task is None:
        return

    # If the task already finished, just render once instead of entering
    # Live (which would immediately exit and look odd).
    if task.finished():
        console.print(_status_renderable(task))
        return

    try:
        with Live(
            _status_renderable(task),
            console=console,
            refresh_per_second=4,
            transient=False,  # leave final frame on screen after exit
        ) as live:
            try:
                while True:
                    _time.sleep(0.25)
                    latest = _load_task(task.id)
                    if latest is None:
                        break
                    live.update(_status_renderable(latest))
                    if latest.finished():
                        break
                    if latest.pid and not latest.is_alive() and not latest.finished():
                        # subprocess gone but state not reconciled —
                        # show one last frame and let the user exit.
                        break
            except KeyboardInterrupt:
                pass
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]watch failed:[/red] {e}")


def _tasks_tail(
    partial: str | None,
    verbose: bool = False,
    state: "ReplState | None" = None,
    cfg: LuxeConfig | None = None,
) -> None:
    """Follow a task's log.jsonl in real time and render events with the
    same sync-mode formatter. When `verbose` is True, also surface
    per-tool-call begin/end events (name + args preview + duration).
    Exits when the task hits a `finish` event or the subprocess is no
    longer alive.

    When `state` and `cfg` are passed, the tail folds each subtask's
    `prompt_tokens` / `completion_tokens` / `wall_s` into the REPL
    session totals as `end` events arrive (each subtask counts as one
    turn). A snapshot of `ctx` + `session totals` is printed under the
    `following …` header and again on task finish.
    """
    import json as _json
    import time as _time

    task = _tasks_resolve(partial)
    if task is None:
        return
    log_path = task.dir() / "log.jsonl"

    mode = " [dim](verbose)[/dim]" if verbose else ""
    console.print(
        f"[dim]following[/dim] [cyan]{task.id}[/cyan]{mode} "
        f"[dim](Ctrl-C to stop watching)[/dim]"
    )
    if state is not None and cfg is not None:
        _render_tail_totals(state, cfg)

    seen_subtasks: set[str] = set()

    def _process_event(event: dict) -> None:
        _sync_event_printer(event, verbose=verbose)
        if state is None or cfg is None:
            return
        kind = event.get("event", "")
        if kind == "begin":
            model = event.get("model") or ""
            if model:
                state.last_model = model
            agent = event.get("agent") or ""
            if agent and cfg is not None:
                try:
                    state.last_endpoint = cfg.resolve_endpoint(cfg.get(agent))
                except Exception:  # noqa: BLE001
                    pass
        elif kind == "end":
            sub_id = event.get("subtask") or ""
            if sub_id and sub_id in seen_subtasks:
                return  # replay-pass dedupe
            if sub_id:
                seen_subtasks.add(sub_id)
            pt = event.get("prompt_tokens") or 0
            ct = event.get("completion_tokens") or 0
            wall = event.get("wall_s") or 0.0
            state.total_prompt_tokens += int(pt)
            state.total_completion_tokens += int(ct)
            state.total_wall_s += float(wall)
            state.turns += 1
            if pt:
                state.last_ctx_used = int(pt)

    # Replay what's already on disk first so the user sees current state.
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            try:
                _process_event(_json.loads(line))
            except _json.JSONDecodeError:
                continue

    # Early exit if the task is already finished.
    from luxe_cli.tasks import load as _load_task
    latest = _load_task(task.id)
    if latest and latest.finished():
        if state is not None and cfg is not None:
            _render_tail_totals(state, cfg)
        return

    # Incremental tail: reopen file on each poll, seek past what we've seen.
    try:
        seen = log_path.stat().st_size if log_path.exists() else 0
        while True:
            _time.sleep(0.5)
            latest = _load_task(task.id)
            if not log_path.exists():
                if latest and latest.finished():
                    return
                continue
            size = log_path.stat().st_size
            if size > seen:
                with log_path.open() as f:
                    f.seek(seen)
                    chunk = f.read()
                seen = size
                for line in chunk.splitlines():
                    try:
                        _process_event(_json.loads(line))
                    except _json.JSONDecodeError:
                        continue
            if latest and latest.finished():
                if state is not None and cfg is not None:
                    _render_tail_totals(state, cfg)
                return
            # Subprocess died without writing 'finish' → give up politely.
            if latest and latest.pid and not latest.is_alive() and not latest.finished():
                console.print("[yellow]subprocess gone — task may have crashed[/yellow]")
                return
    except KeyboardInterrupt:
        console.print("[dim]stopped watching[/dim]")
        if state is not None and cfg is not None:
            _render_tail_totals(state, cfg)


def _render_tail_totals(state: "ReplState", cfg: LuxeConfig) -> None:
    """Two-line snapshot — ctx + session totals — printed under the
    `following …` header and again on task finish/abort. Mirrors the
    format `_print_stats` uses after foreground turns so the eye
    recognizes it without re-parsing."""
    from luxe_cli.backend import context_length
    from luxe_cli.repl.status import _fmt_wall

    model = state.last_model or "(unknown)"
    endpoint = state.last_endpoint or ""
    ctx_total = context_length(model, endpoint) if endpoint else 0
    used = state.last_ctx_used
    free = max(ctx_total - used, 0) if ctx_total else 0
    pct_free = (free / ctx_total * 100.0) if ctx_total else 100.0

    console.print(
        f"[dim]ctx: {used:,}/{ctx_total:,} ({pct_free:.0f}% free) · "
        f"{model}[/dim]"
    )
    console.print(
        f"[dim]session totals: {state.turns} turns · "
        f"{_fmt_wall(state.total_wall_s)} · "
        f"{state.total_prompt_tokens:,}↑ "
        f"{state.total_completion_tokens:,}↓ tokens[/dim]"
    )


def _tasks_log(partial: str | None, tail: int = 20) -> None:
    task = _tasks_resolve(partial)
    if not task:
        return
    log_path = task.dir() / "log.jsonl"
    if not log_path.exists():
        console.print("[dim]no log events yet[/dim]")
        return
    import json as _json
    lines = log_path.read_text().splitlines()[-tail:]
    for line in lines:
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        ts = ev.get("ts", "")[11:19]  # HH:MM:SS
        rest = {k: v for k, v in ev.items() if k != "ts"}
        console.print(f"[dim]{ts}[/dim]  {rest}")


def _tasks_analyze(partial: str | None) -> None:
    """Per-tool breakdown for a task — surfaces which tools the agent
    actually used per subtask, how long each took, and the real-
    analyzer-vs-grep adoption ratio. Reads directly from state.json."""
    from collections import defaultdict
    from luxe_cli.repl.status import _fmt_wall
    task = _tasks_resolve(partial)
    if not task:
        return
    if not task.subtasks:
        console.print("[dim]no subtasks[/dim]")
        return

    # Tag each tool name as analyzer / reader / orientation / other —
    # lets us print an "analyzer adoption" ratio at the bottom.
    ANALYZERS = frozenset({
        "lint", "typecheck", "security_scan", "deps_audit",
        "security_taint", "secrets_scan",
    })
    READERS = frozenset({"read_file", "grep"})
    ORIENTATION = frozenset({"list_dir", "glob"})

    t = Table(show_header=True, box=None, padding=(0, 2), header_style="dim")
    t.add_column("sub")
    t.add_column("agent")
    t.add_column("tool", overflow="fold")
    t.add_column("calls", justify="right")
    t.add_column("wall", justify="right")
    t.add_column("bytes out", justify="right")
    t.add_column("ok", justify="right")

    totals_by_kind: dict[str, int] = defaultdict(int)

    for sub in task.subtasks:
        if not sub.tool_calls:
            if sub.status == "done":
                t.add_row(
                    str(sub.index), sub.agent or "(route)",
                    "[dim](no tool calls)[/dim]", "0", "-", "-", "-",
                )
            continue
        counts: dict[str, list[float]] = defaultdict(list)
        bytes_by: dict[str, int] = defaultdict(int)
        oks_by: dict[str, int] = defaultdict(int)
        for tc in sub.tool_calls:
            counts[tc.name].append(tc.wall_s)
            bytes_by[tc.name] += tc.bytes_out
            if tc.ok:
                oks_by[tc.name] += 1
            if tc.name in ANALYZERS:
                totals_by_kind["analyzer"] += 1
            elif tc.name in READERS:
                totals_by_kind["reader"] += 1
            elif tc.name in ORIENTATION:
                totals_by_kind["orientation"] += 1
            else:
                totals_by_kind["other"] += 1
        first = True
        for name in sorted(counts, key=lambda k: -sum(counts[k])):
            total_wall = sum(counts[name])
            n = len(counts[name])
            color = (
                "green" if name in ANALYZERS
                else "cyan" if name in READERS
                else "dim" if name in ORIENTATION
                else "white"
            )
            t.add_row(
                str(sub.index) if first else "",
                (sub.agent or "(route)") if first else "",
                f"[{color}]{name}[/{color}]",
                str(n),
                _fmt_wall(total_wall),
                f"{bytes_by[name]:,}",
                f"{oks_by[name]}/{n}",
            )
            first = False

    console.print(t)

    total = sum(totals_by_kind.values())
    if total:
        analyzer_pct = 100 * totals_by_kind["analyzer"] / total
        reader_pct = 100 * totals_by_kind["reader"] / total
        orient_pct = 100 * totals_by_kind["orientation"] / total
        console.print(
            f"[dim]adoption:[/dim] "
            f"[green]analyzer[/green] {totals_by_kind['analyzer']} "
            f"({analyzer_pct:.0f}%) · "
            f"[cyan]reader[/cyan] {totals_by_kind['reader']} "
            f"({reader_pct:.0f}%) · "
            f"[dim]orientation[/dim] {totals_by_kind['orientation']} "
            f"({orient_pct:.0f}%) · "
            f"[dim]other[/dim] {totals_by_kind['other']}"
        )


def _tasks_save(partial: str | None) -> None:
    """Assemble a finished task's subtask outputs into a markdown report
    and write it into the task's target folder. Defaults to the repo root
    for /review and /refactor runs; otherwise falls back to cwd."""
    from luxe_cli.repl.prompt import _ask_styled
    from luxe_cli.tasks.report import build_markdown_report
    task = _tasks_resolve(partial)
    if not task:
        return
    if not task.finished():
        console.print(
            f"[yellow]task not finished yet[/yellow] "
            f"([dim]status:[/dim] {task.status}). "
            f"/tasks abort [dim]first if you want to save partial output.[/dim]"
        )
        return

    # Resolve default target directory.
    repo_ptr = task.dir() / "repo_path"
    if repo_ptr.exists():
        target_dir = Path(repo_ptr.read_text().strip())
        if not target_dir.exists():
            target_dir = Path.cwd()
    else:
        target_dir = Path.cwd()

    body = build_markdown_report(task)

    # Show summary + prompt for filename / format.
    console.print()
    console.print(f"[dim]Summary of[/dim] [cyan]{task.id}[/cyan]:")
    for s in task.subtasks:
        icon = {"done": "[green]✓[/green]", "blocked": "[yellow]⚠[/yellow]",
                "skipped": "[dim]–[/dim]"}.get(s.status, "·")
        preview = (s.result_text or s.error)[:140].replace("\n", " ")
        console.print(f"  {icon} [dim]{s.index}.[/dim] {s.title[:60]}")
        if preview:
            console.print(f"      [dim]{preview}…[/dim]" if len(preview) >= 140 else f"      [dim]{preview}[/dim]")

    default_name = f"REVIEW-{task.id}.md"
    console.print()
    console.print(
        f"[dim]Save report to[/dim] [cyan]{target_dir / default_name}[/cyan]?\n"
        f"[dim]Options: <enter> = yes · [/dim][cyan]new_name.md[/cyan][dim] to rename · "
        f"[/dim][cyan]new_name.txt[/cyan][dim] to save as text · [/dim][cyan]n[/cyan][dim] to skip[/dim]"
    )
    try:
        answer = _ask_styled("save")
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]skipped[/dim]")
        return
    if answer.lower() in ("n", "no"):
        console.print("[dim]skipped[/dim]")
        return
    name = answer or default_name
    if not name.endswith((".md", ".txt")):
        name = f"{name}.md"

    target = target_dir / name
    try:
        target.write_text(body)
    except OSError as e:
        console.print(f"[red]write failed:[/red] {e}")
        return
    console.print(f"[green]✓ wrote[/green] [cyan]{target}[/cyan] [dim]({len(body):,} bytes)[/dim]")


def _tasks_resume(partial: str | None) -> None:
    """Re-run a task's non-done subtasks in place. Flips blocked/skipped/
    running subs back to pending and re-spawns via the background path.
    Done subs are preserved so their results still seed later subs."""
    from luxe_cli.tasks import reset_incomplete_subtasks, spawn_background
    from luxe_cli.tasks.model import persist
    task = _tasks_resolve(partial)
    if not task:
        return
    if task.is_alive():
        console.print(
            f"[yellow]{task.id} is still running[/yellow] "
            f"[dim](pid {task.pid}). /tasks abort first.[/dim]"
        )
        return
    reset = reset_incomplete_subtasks(task)
    if reset == 0:
        console.print(
            f"[dim]{task.id} has no blocked/skipped subtasks — nothing to resume.[/dim]"
        )
        return
    done = sum(1 for s in task.subtasks if s.status == "done")
    total = len(task.subtasks)
    console.print(
        f"[green]↻ resuming[/green] [cyan]{task.id}[/cyan] "
        f"[dim]· {reset} subtask(s) reset · {done}/{total} already done[/dim]"
    )
    pid = spawn_background(task)
    task.pid = pid
    persist(task)
    console.print(f"[dim]pid {pid}[/dim]")
    _print_launch_hints(task.id)


def _tasks_abort(partial: str | None) -> None:
    from luxe_cli.tasks import abort_task
    task = _tasks_resolve(partial)
    if not task:
        return
    if not task.is_alive():
        console.print(f"[dim]{task.id} is not running (status: {task.status})[/dim]")
        return
    console.print(f"[dim]signalling SIGTERM to[/dim] [cyan]{task.id}[/cyan] [dim](pid {task.pid})[/dim]")
    with console.status("[yellow]aborting...[/yellow]", spinner="dots"):
        abort_task(task)
    console.print(f"[yellow]✗ aborted[/yellow] {task.id}")


def _wrap_or_inline(
    prefix: str,
    message: str,
    *,
    message_style: str = "",
    indent: str = "[dim]│[/dim]     ",
) -> None:
    """Print 'prefix + message'. If the combined line would overflow the
    terminal, emit the message on an indented continuation line instead
    of silently truncating. Full untruncated text is also in log.jsonl —
    this is a display-only concern."""
    if not message:
        console.print(prefix.rstrip())
        return
    prefix_cells = Text.from_markup(prefix).cell_len
    avail = max(40, console.width - prefix_cells)
    styled = f"[{message_style}]{message}[/{message_style}]" if message_style else message
    if Text(message).cell_len <= avail:
        console.print(f"{prefix}{styled}")
    else:
        console.print(prefix.rstrip())
        console.print(f"{indent}{styled}", overflow="fold", soft_wrap=False)


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _sync_event_printer(event: dict, verbose: bool = False) -> None:
    """Tail-style live output for sync + background tail runs. Surfaces
    model tag on begin (so you can see which weights the subtask
    actually asked for) and prompt/completion token counts on end
    (so 'why is this so slow' is answerable from the log). When
    `verbose` is True, also renders per-tool-call begin/end events."""
    from luxe_cli.repl.status import _fmt_clock, _fmt_wall
    kind = event.get("event", "")
    sub = (event.get("subtask") or "").rsplit(".", 1)[-1]
    if kind == "start":
        console.print(f"[dim]┌ running {event.get('n_subtasks', 0)} subtask(s)…[/dim]")
    elif kind == "begin":
        agent = event.get("agent") or "?"
        model = event.get("model") or ""
        model_tag = f" [dim]·[/dim] [cyan]{model}[/cyan]" if model else ""
        # Escape the literal brackets around the agent name so Rich
        # doesn't parse "[review]" as a markup tag (unknown style names
        # render as empty). See tasks.py history for the silent bug this
        # replaced.
        _wrap_or_inline(
            f"[dim]│[/dim] \\[[magenta]{agent}[/magenta]]{model_tag} [dim]·[/dim] ",
            event.get("title", ""),
        )
    elif kind == "end":
        status = event.get("status", "")
        icon = {
            "done":    "[green]✓[/green]",
            "blocked": "[yellow]⚠[/yellow]",
            "skipped": "[dim]–[/dim]",
        }.get(status, "·")
        wall = event.get("wall_s", 0)
        tools = event.get("tool_calls", 0)
        pt = event.get("prompt_tokens")
        ct = event.get("completion_tokens")
        tools_str = f"{tools} tool call{'s' if tools != 1 else ''}"
        tok_str = (
            f" · {pt}↑ {ct}↓ tok"
            if (pt is not None and ct is not None)
            else ""
        )
        started = _fmt_clock(event.get("started_at") or "")
        ended = _fmt_clock(event.get("completed_at") or "")
        clock_str = f" · {started} · {ended}" if (started and ended) else ""
        suffix = f"[dim]{_fmt_wall(wall)} · {tools_str}{tok_str}{clock_str}[/dim]"
        near_cap = event.get("near_cap_turns") or 0
        if near_cap:
            suffix += f" [yellow]⚠ {near_cap} near-cap turn(s)[/yellow]"
        err = event.get("error") or ""
        _wrap_or_inline(
            f"[dim]│[/dim] {icon} sub {sub} · {suffix} ",
            err,
            message_style="yellow",
        )
    elif kind == "retry_transport":
        _wrap_or_inline(
            f"[dim]│[/dim] [yellow]retry[/yellow] sub {sub} ",
            f"({event.get('error', '')})" if event.get("error") else "",
            message_style="dim",
        )
    elif kind == "tool_use_retry":
        _wrap_or_inline(
            f"[dim]│[/dim] [yellow]retry-tools[/yellow] sub {sub} ",
            f"({event.get('reason', '')})" if event.get("reason") else "",
            message_style="dim",
        )
    elif kind == "skip":
        _wrap_or_inline(
            f"[dim]│[/dim] [dim]skip sub {sub}[/dim] ",
            f"({event.get('reason', '')})" if event.get("reason") else "",
            message_style="dim",
        )
    elif kind == "report_saved":
        path = event.get("path", "")
        console.print(f"[dim]│[/dim] [green]📝 saved[/green] [cyan]{path}[/cyan]")
    elif kind == "tool_call_begin" and verbose:
        name = event.get("name", "")
        preview = event.get("args_preview", "")
        preview_str = f" [dim]{preview}[/dim]" if preview else ""
        console.print(
            f"[dim]│   →[/dim] [cyan]{name}[/cyan]{preview_str}",
            overflow="ellipsis",
            soft_wrap=False,
        )
    elif kind == "tool_call_end" and verbose:
        name = event.get("name", "")
        ok = event.get("ok", True)
        wall = event.get("wall_s", 0.0)
        bytes_out = event.get("bytes_out", 0)
        icon = "[green]✓[/green]" if ok else "[red]✗[/red]"
        size = _fmt_bytes(int(bytes_out))
        err = event.get("error") or ""
        suffix = f"[dim]{wall:.2f}s · {size}[/dim]"
        if ok:
            console.print(f"[dim]│   [/dim]{icon} [cyan]{name}[/cyan] {suffix}")
        else:
            err_short = err[:80] + ("…" if len(err) > 80 else "")
            console.print(
                f"[dim]│   [/dim]{icon} [cyan]{name}[/cyan] {suffix} "
                f"[yellow]err:[/yellow] [dim]{err_short}[/dim]"
            )
    elif kind == "finish":
        console.print(f"[dim]└ task {event.get('status', '')}[/dim]")


def _tasks_run_sync(goal: str, state: "ReplState", cfg: LuxeConfig) -> None:
    """Plan + run synchronously. Blocks the REPL until done. Streams
    tail-style events to the console as each subtask starts/finishes,
    then shows the status table and offers the save prompt."""
    from luxe_cli.tasks import Orchestrator
    from luxe_cli.repl.review import _plan_and_persist

    task = _plan_and_persist(goal, cfg)
    if task is None:
        return
    orch = Orchestrator(cfg, session=state.sess, on_event=_sync_event_printer)
    try:
        orch.run(task)
    except KeyboardInterrupt:
        console.print("[yellow]⚠ task interrupted[/yellow]")
        return
    console.print()
    _tasks_status(task.id)
    if task.status == "done":
        console.print()
        _tasks_save(task.id)


def _tasks_run_background(goal: str, state: "ReplState", cfg: LuxeConfig) -> None:
    """Plan in the foreground (so the user sees subtasks immediately), then
    spawn a detached subprocess to execute. REPL stays responsive."""
    from luxe_cli.tasks import spawn_background
    from luxe_cli.tasks.model import persist
    from luxe_cli.repl.review import _plan_and_persist

    task = _plan_and_persist(goal, cfg)
    if task is None:
        return
    pid = spawn_background(task)
    task.pid = pid
    persist(task)
    console.print(
        f"[green]→ launched[/green] [cyan]{task.id}[/cyan] "
        f"[dim](pid {pid})[/dim]"
    )
    _print_launch_hints(task.id)


def _print_launch_hints(task_id: str) -> None:
    """One copy-pasteable command per line — triple-click to select,
    paste to run. Shared by /tasks and /review launch paths."""
    rows = [
        ("snapshot",  "status"),
        ("live tail", "tail"),
        ("dashboard", "watch"),
        ("stop",      "abort"),
        ("resume",    "resume"),
        ("save",      "save"),
    ]
    label_w = max(len(lbl) for lbl, _ in rows)
    for label, sub in rows:
        console.print(
            f"[dim]  {label:<{label_w}}[/dim]  "
            f"[cyan]/tasks {sub} {task_id}[/cyan]"
        )
