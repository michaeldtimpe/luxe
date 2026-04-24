"""Task management — list, resolve, status, watch, tail, log, save, abort, run."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from luxe.registry import LuxeConfig

console = Console()


def _tasks_list_recent() -> None:
    from luxe.tasks import list_all as _list_all
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
    from luxe.tasks import list_all as _list_all
    from luxe.tasks.model import resolve_partial as _resolve
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
    from luxe.repl.status import _fmt_wall
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
    from luxe.tasks import load as _load_task

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


def _tasks_tail(partial: str | None) -> None:
    """Follow a task's log.jsonl in real time and render events with the
    same sync-mode formatter. Exits when the task hits a `finish` event
    or the subprocess is no longer alive."""
    import json as _json
    import time as _time

    task = _tasks_resolve(partial)
    if task is None:
        return
    log_path = task.dir() / "log.jsonl"

    console.print(f"[dim]following[/dim] [cyan]{task.id}[/cyan] [dim](Ctrl-C to stop watching)[/dim]")
    # Replay what's already on disk first so the user sees current state.
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            try:
                _sync_event_printer(_json.loads(line))
            except _json.JSONDecodeError:
                continue

    # Early exit if the task is already finished.
    from luxe.tasks import load as _load_task
    latest = _load_task(task.id)
    if latest and latest.finished():
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
                        _sync_event_printer(_json.loads(line))
                    except _json.JSONDecodeError:
                        continue
            if latest and latest.finished():
                return
            # Subprocess died without writing 'finish' → give up politely.
            if latest and latest.pid and not latest.is_alive() and not latest.finished():
                console.print("[yellow]subprocess gone — task may have crashed[/yellow]")
                return
    except KeyboardInterrupt:
        console.print("[dim]stopped watching[/dim]")


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
    from luxe.repl.status import _fmt_wall
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
    from luxe.repl.prompt import _ask_styled
    from luxe.tasks.report import build_markdown_report
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


def _tasks_abort(partial: str | None) -> None:
    from luxe.tasks import abort_task
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


def _sync_event_printer(event: dict) -> None:
    """Tail-style live output for sync + background tail runs. Surfaces
    model tag on begin (so you can see which weights the subtask
    actually asked for) and prompt/completion token counts on end
    (so 'why is this so slow' is answerable from the log)."""
    from luxe.repl.status import _fmt_clock, _fmt_wall
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
    elif kind == "finish":
        console.print(f"[dim]└ task {event.get('status', '')}[/dim]")


def _tasks_run_sync(goal: str, state: "ReplState", cfg: LuxeConfig) -> None:
    """Plan + run synchronously. Blocks the REPL until done. Streams
    tail-style events to the console as each subtask starts/finishes,
    then shows the status table and offers the save prompt."""
    from luxe.tasks import Orchestrator
    from luxe.repl.review import _plan_and_persist

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
    from luxe.tasks import spawn_background
    from luxe.tasks.model import persist
    from luxe.repl.review import _plan_and_persist

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
        ("save",      "save"),
    ]
    label_w = max(len(lbl) for lbl, _ in rows)
    for label, sub in rows:
        console.print(
            f"[dim]  {label:<{label_w}}[/dim]  "
            f"[cyan]/tasks {sub} {task_id}[/cyan]"
        )
