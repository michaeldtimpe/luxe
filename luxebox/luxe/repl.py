"""Interactive REPL. Reads prompts, routes, prints responses, logs sessions."""

from __future__ import annotations

import os
import random
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from luxe import prefs, router, runner
from luxe.backend import (
    MODEL_VARIANTS,
    clear_caches,
    context_length,
    estimate_kv_ram_gb,
    installed_by_family,
    list_models,
    max_context_length,
    parameter_size,
    ping,
    prewarm as _prewarm,
    pull_stream,
    server_process_rss_gb,
)
from luxe.registry import LuxeConfig
from luxe.router import RouterDecision
from luxe import __version__
from luxe.session import Session, list_sessions

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


def _show_context_info(state: ReplState, cfg: LuxeConfig) -> None:
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


def _start_review(url: str, mode: str, state: "ReplState", cfg: LuxeConfig) -> None:
    """Shared entry point for /review and /refactor. Clone/pull into cwd,
    plan a review-flavored task pinned to the `review` or `refactor`
    agent, then spawn it in the background."""
    from luxe.git import repo_name_from_url, resolve_repo
    from luxe.tasks import plan, spawn_background
    from luxe.tasks.model import Task, persist, task_id

    console.print(f"[dim]resolving[/dim] [cyan]{url}[/cyan][dim]...[/dim]")
    with console.status("[dim]git[/dim]", spinner="dots"):
        repo_path, status_msg = resolve_repo(url, Path.cwd())
    if repo_path is None:
        console.print(f"[red]{status_msg}[/red]")
        return
    console.print(f"[dim]{status_msg} → {repo_path}[/dim]")

    repo_label = repo_name_from_url(url) or repo_path.name
    if mode == "review":
        goal = (
            f"Review the `{repo_label}` repository at {repo_path}. "
            f"Start by listing the root with `list_dir` and reading any "
            f"README/ARCHITECTURE/CONTRIBUTING/SECURITY/docs files. Then "
            f"systematically look for: (1) security issues — input handling, "
            f"auth, secrets, injection, deserialization, path traversal, "
            f"dependency vulns; (2) correctness bugs — error handling, race "
            f"conditions, resource leaks, silent failures; (3) robustness — "
            f"missing timeouts/retries, unbounded loops; (4) maintainability "
            f"issues that mask real risk. End with a severity-grouped "
            f"markdown report. Ground every finding in code you read via "
            f"tools — no invented filenames or quotes."
        )
    else:  # refactor
        goal = (
            f"Analyze the `{repo_label}` repository at {repo_path} for "
            f"optimization and refactor opportunities. Start by listing "
            f"the root with `list_dir` and reading the README and core "
            f"entry points. Then systematically identify: (1) performance "
            f"— obvious algorithmic inefficiency, missing caching, "
            f"unbatched I/O; (2) architectural issues — leaky "
            f"abstractions, modules that should split or merge, painful "
            f"API surfaces; (3) code-size wins — duplication, dead code; "
            f"(4) idiomatic improvements that cut real complexity. End "
            f"with an impact-ranked markdown report of recommended changes. "
            f"Ground every suggestion in code you actually read."
        )

    task = Task(id=task_id(), goal=goal, max_wall_s=1800.0)
    task.subtasks = plan(goal, cfg, task.id)
    # Pin every subtask to the dedicated agent — planner may default to
    # `code`, but we want the review-flavored system prompt on the whole
    # run.
    for s in task.subtasks:
        s.agent = mode
    persist(task)

    if not _plan_review_loop(task, cfg):
        console.print("[dim]aborted — no task launched[/dim]")
        return
    persist(task)

    # Spawn the background subprocess with cwd set to the repo so fs
    # tools resolve paths against the repo root.
    pid = _spawn_in_repo(task, repo_path)
    task.pid = pid
    persist(task)

    # Stash repo path in the task dir so /tasks save can default to it.
    (task.dir() / "repo_path").write_text(str(repo_path))

    console.print(
        f"[green]→ launched[/green] [cyan]{task.id}[/cyan] "
        f"[dim](pid {pid}, cwd {repo_path})[/dim]"
    )
    _print_launch_hints(task.id)


def _spawn_in_repo(task, repo_path: Path) -> int:
    """spawn_background copies parent cwd; we need cwd = repo_path."""
    import subprocess
    import sys
    log_path = task.dir() / "stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    f = log_path.open("ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, "-m", "luxe.tasks.run", task.id],
        stdout=f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(repo_path),
    )
    return proc.pid


def _tasks_save(partial: str | None) -> None:
    """Assemble a finished task's subtask outputs into a markdown report
    and write it into the task's target folder. Defaults to the repo root
    for /review and /refactor runs; otherwise falls back to cwd."""
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

    # Build the report body.
    lines: list[str] = [
        f"# Task report — {task.id}",
        "",
        f"- **Goal**: {task.goal}",
        f"- **Status**: {task.status}",
        f"- **Started**: {task.created_at}",
        f"- **Finished**: {task.completed_at}",
        "",
    ]
    for s in task.subtasks:
        lines.append(f"## {s.index}. {s.title}")
        lines.append(f"*Agent: `{s.agent or 'route'}` · status: {s.status} · "
                     f"wall: {_fmt_wall(s.wall_s)} · tool calls: {s.tool_calls_total}*")
        lines.append("")
        if s.error:
            lines.append(f"> **Error:** {s.error}")
            lines.append("")
        if s.result_text:
            lines.append(s.result_text.rstrip())
            lines.append("")
    body = "\n".join(lines) + "\n"

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


def _plan_and_persist(goal: str, cfg: LuxeConfig):
    """Shared prelude for sync + background paths.

    Flow: (1) ask the screener for clarifying questions, prompt the user,
    fold answers back into the goal; (2) plan; (3) show the plan and let
    the user edit agent assignments / drop / add subtasks / abort before
    spending compute. Returns the Task, or None on abort or failure.
    """
    from luxe.tasks import clarify, plan
    from luxe.tasks.model import Task, persist, task_id

    augmented = _clarify_goal(goal, clarify, cfg)
    if augmented is None:
        return None
    goal = augmented

    task = Task(id=task_id(), goal=goal)
    console.print(f"[dim]→ planning[/dim] [cyan]{task.id}[/cyan][dim]...[/dim]")
    try:
        task.subtasks = plan(goal, cfg, task.id)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]planner failed:[/red] {e}")
        return None
    persist(task)

    if not _plan_review_loop(task, cfg):
        console.print("[dim]aborted — no task launched[/dim]")
        return None
    persist(task)  # save any edits
    return task


def _clarify_goal(goal: str, clarify_fn, cfg: LuxeConfig) -> str | None:
    """Ask the screener for questions, prompt user for each, fold
    non-empty answers into the goal. Returns augmented goal (same goal
    if no questions), or None on Ctrl-C / EOF."""
    try:
        questions = clarify_fn(goal, cfg)
    except Exception:  # noqa: BLE001
        questions = []
    if not questions:
        return goal
    console.print("[dim]A few clarifying questions before planning:[/dim]")
    out = goal
    for q in questions:
        console.print(f"  [yellow]?[/yellow] {q}")
        try:
            ans = _ask_styled("  answer")
        except (EOFError, KeyboardInterrupt):
            console.print("[dim]aborted during clarification[/dim]")
            return None
        if ans:
            out += f"\n\nClarification — {q}\n{ans}"
    return out


def _plan_review_loop(task, cfg: LuxeConfig) -> bool:
    """Interactive plan review. Returns True = run, False = abort.

    Commands at the `plan>` prompt:
      <enter>             run the plan as shown
      abort / n / no      cancel, don't launch
      agent <i> <name>    change agent of subtask #i
      drop <i>            remove subtask #i and re-index the rest
      add <title>         append a new subtask (router picks its agent)
    """
    from luxe.tasks.model import Subtask, subtask_id

    valid = {a.name for a in cfg.agents if a.enabled and a.name != "router"}

    while True:
        console.print()
        console.print("[bold]Plan:[/bold]")
        for s in task.subtasks:
            console.print(
                f"  [dim]{s.index}.[/dim] [cyan]{s.agent or '(route)'}[/cyan] · {s.title}"
            )
        console.print(
            "[dim]<enter>=run · abort · agent <i> <name> · drop <i> · add <title>[/dim]"
        )
        try:
            answer = _ask_styled("plan")
        except (EOFError, KeyboardInterrupt):
            return False
        if not answer:
            return True
        if answer.lower() in ("abort", "cancel", "n", "no"):
            return False

        parts = answer.split(maxsplit=2)
        head = parts[0].lower()

        if head == "agent" and len(parts) >= 3:
            try:
                idx = int(parts[1])
            except ValueError:
                console.print("[yellow]usage:[/yellow] agent <number> <agent-name>")
                continue
            new_agent = parts[2].strip()
            if new_agent not in valid:
                console.print(
                    f"[yellow]unknown agent:[/yellow] {new_agent} "
                    f"[dim](valid: {', '.join(sorted(valid))})[/dim]"
                )
                continue
            sub = next((s for s in task.subtasks if s.index == idx), None)
            if not sub:
                console.print(f"[yellow]no subtask {idx}[/yellow]")
                continue
            sub.agent = new_agent
            continue

        if head == "drop" and len(parts) >= 2:
            try:
                idx = int(parts[1])
            except ValueError:
                console.print("[yellow]usage:[/yellow] drop <number>")
                continue
            task.subtasks = [s for s in task.subtasks if s.index != idx]
            for i, s in enumerate(task.subtasks, 1):
                s.index = i
                s.id = subtask_id(task.id, i)
            continue

        if head == "add" and len(parts) >= 2:
            title = answer[len("add"):].strip()
            next_idx = (task.subtasks[-1].index + 1) if task.subtasks else 1
            task.subtasks.append(
                Subtask(
                    id=subtask_id(task.id, next_idx),
                    parent_id=task.id,
                    index=next_idx,
                    title=title,
                    agent="",
                )
            )
            continue

        console.print(
            "[yellow]unknown command[/yellow] "
            "[dim]— try <enter>, abort, agent, drop, add[/dim]"
        )


def _sync_event_printer(event: dict) -> None:
    """Tail-style live output for sync + background tail runs. Surfaces
    model tag on begin (so you can see which weights the subtask
    actually asked for) and prompt/completion token counts on end
    (so 'why is this so slow' is answerable from the log)."""
    kind = event.get("event", "")
    sub = (event.get("subtask") or "").rsplit(".", 1)[-1]
    if kind == "start":
        console.print(f"[dim]┌ running {event.get('n_subtasks', 0)} subtask(s)…[/dim]")
    elif kind == "begin":
        agent = event.get("agent") or "?"
        model = event.get("model") or ""
        model_tag = f" [dim]·[/dim] [cyan]{model}[/cyan]" if model else ""
        console.print(
            f"[dim]│[/dim] [{agent}]{model_tag} "
            f"[dim]·[/dim] {event.get('title', '')[:72]}"
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
        suffix = f"[dim]{_fmt_wall(wall)} · {tools_str}{tok_str}[/dim]"
        err = event.get("error") or ""
        if err:
            suffix += f" [yellow]{err[:80]}[/yellow]"
        console.print(f"[dim]│[/dim] {icon} sub {sub} · {suffix}")
    elif kind == "retry_transport":
        console.print(
            f"[dim]│[/dim] [yellow]retry[/yellow] sub {sub} "
            f"[dim]({event.get('error', '')})[/dim]"
        )
    elif kind == "tool_use_retry":
        console.print(
            f"[dim]│[/dim] [yellow]retry-tools[/yellow] sub {sub} "
            f"[dim]({event.get('reason', '')})[/dim]"
        )
    elif kind == "skip":
        console.print(
            f"[dim]│[/dim] [dim]skip sub {sub} ({event.get('reason', '')})[/dim]"
        )
    elif kind == "finish":
        console.print(f"[dim]└ task {event.get('status', '')}[/dim]")


def _tasks_run_sync(goal: str, state: "ReplState", cfg: LuxeConfig) -> None:
    """Plan + run synchronously. Blocks the REPL until done. Streams
    tail-style events to the console as each subtask starts/finishes,
    then shows the status table and offers the save prompt."""
    from luxe.tasks import Orchestrator

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


def _print_variants(family_filter: str | None, cfg: LuxeConfig) -> None:
    """Render installed vs released variants per model family."""
    installed = list_models(cfg.ollama_base_url)
    grouped = installed_by_family(installed)

    families = [family_filter] if family_filter else sorted(
        set(grouped) | set(MODEL_VARIANTS)
    )

    t = Table(show_header=True, box=None, padding=(0, 2), header_style="dim")
    t.add_column("family")
    t.add_column("installed")
    t.add_column("available")
    for family in families:
        have = grouped.get(family, set())
        released = MODEL_VARIANTS.get(family)
        if not have and released is None:
            continue  # unknown family and nothing installed → skip
        inst_display = ", ".join(sorted(have)) if have else "[dim]—[/dim]"
        if released:
            # Green for installed, dim for pullable.
            avail_display = ", ".join(
                f"[green]{v}[/green]" if v in have else f"[dim]{v}[/dim]"
                for v in released
            )
        else:
            avail_display = "[dim]?[/dim]"
        t.add_row(family, inst_display, avail_display)
    console.print(t)
    console.print(
        "[dim]green = installed · dim = available via[/dim] [cyan]/pull <family>:<size>[/cyan]"
    )


def _pull_with_progress(tag: str, base_url: str) -> bool:
    """Stream Ollama pull and render per-layer progress bars. Returns True
    on clean success. Ctrl-C aborts gracefully."""
    console.print(f"[dim]pulling[/dim] [cyan]{tag}[/cyan][dim]...[/dim]")
    with Progress(
        TextColumn("[dim]{task.description}"),
        BarColumn(bar_width=30),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        tasks: dict[str, int] = {}
        try:
            for event in pull_stream(tag, base_url):
                if err := event.get("error"):
                    progress.console.print(f"[red]error:[/red] {err}")
                    return False
                digest = event.get("digest", "")
                total = event.get("total")
                completed = event.get("completed") or 0
                if digest and total:
                    if digest not in tasks:
                        tasks[digest] = progress.add_task(digest[7:19], total=total)
                    progress.update(tasks[digest], completed=completed)
                else:
                    status = (event.get("status") or "").strip()
                    # Layer lines come with digest — skip those we didn't bind;
                    # surface only the top-level status messages.
                    if status and not status.startswith("pulling ") and status != "success":
                        progress.console.print(f"[dim]· {status}[/dim]")
        except KeyboardInterrupt:
            progress.console.print("[yellow]pull interrupted[/yellow]")
            return False
        except httpx.HTTPError as e:
            progress.console.print(f"[red]pull failed:[/red] {e}")
            return False
    # Fresh weights → invalidate cached ctx + params for this tag so the
    # banner and /context pick up the real values on the next lookup.
    clear_caches(tag)
    console.print(f"[green]✓ pulled[/green] [cyan]{tag}[/cyan]")
    return True


def _prompt_assign_to_agent(tag: str, cfg: LuxeConfig) -> None:
    """After a successful pull, ask if the user wants to wire the new model
    into an agent role. In-memory only for this session — persistence
    requires editing configs/agents.yaml."""
    roles = [a.name for a in cfg.agents]
    console.print(
        f"\n[dim]Assign[/dim] [cyan]{tag}[/cyan] [dim]to an agent role? Options:[/dim] "
        f"{', '.join(roles)} [dim]or[/dim] skip"
    )
    try:
        answer = _ask_styled("role").lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]skipped[/dim]")
        return
    if not answer or answer == "skip":
        return
    if answer not in roles:
        console.print(f"[yellow]not an agent name:[/yellow] {answer} [dim](skipped)[/dim]")
        return
    agent = cfg.get(answer)
    old = agent.model
    agent.model = tag
    console.print(
        f"[dim]→[/dim] [cyan]{answer}[/cyan] [dim]model:[/dim] "
        f"[dim]{old}[/dim] [dim]→[/dim] [cyan]{tag}[/cyan]"
    )
    console.print(
        "[dim]  (session-only · edit configs/agents.yaml to persist across restarts)[/dim]"
    )


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


_BORDER_STYLE = "cyan"


def _status_banner(state: ReplState, cfg: LuxeConfig) -> Panel:
    """Claude Code-style status panel.

    Row 1 is a 2-column grid (left-aligned title, right-aligned version).
    Rows 2–3 are a 4-column grid (label/value × left/right) so long paths
    or model names still keep all four columns aligned. Both grids use
    expand=True inside a Panel with a computed width so their right
    edges line up with each other, giving the visual of three rows in
    a single box. A decorative doubled right edge is painted on by
    _print_status_banner; it's also orange to mirror the luxe title."""
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

    return Panel(grid, border_style=_BORDER_STYLE, expand=False)


def _print_status_banner(state: ReplState, cfg: LuxeConfig) -> None:
    """Render the Panel and tack on a decorative doubled right edge.

    Panel's Box char-set is fixed at 1 char per position, so a doubled
    right border can't be expressed through Rich's usual APIs. We render
    the panel into a capture Console, then append a cyan border char to
    the end of each line so the visual reads as ╮╮ / ││ / ╯╯."""
    from io import StringIO
    from rich.console import Console as _Cons

    panel = _status_banner(state, cfg)
    buf = StringIO()
    capture = _Cons(
        file=buf,
        force_terminal=True,
        color_system=(console.color_system or "truecolor"),
        width=console.width,
    )
    capture.print(panel)
    lines = buf.getvalue().rstrip("\n").split("\n")

    # The inner panel border is cyan; the outer doubled-right layer
    # picks up dark_orange to match the luxe title.
    ORANGE = "\x1b[38;5;208m"  # xterm-256 dark_orange, matches Rich's named color
    RESET = "\x1b[0m"
    decorated: list[str] = []
    for i, ln in enumerate(lines):
        if i == 0:
            extra = "╮"
        elif i == len(lines) - 1:
            extra = "╯"
        else:
            extra = "│"
        decorated.append(f"{ln}{ORANGE}{extra}{RESET}")
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


def _styled_arrow_prompt(lead: str) -> FormattedText:
    """Build a FormattedText prompt with `<lead> >>> ` where the three
    arrows each pick an independent color from the palette and no two
    adjacent arrows share a color. Shared between the main `luxe` prompt
    and sub-prompts (`plan`, `role`, clarifying questions, save name)
    so the whole REPL reads consistently."""
    colors = _pick_no_adjacent_repeats(3)
    return FormattedText([
        ("", f"{lead} "),
        (f"bold fg:{colors[0]}", ">"),
        (f"bold fg:{colors[1]}", ">"),
        (f"bold fg:{colors[2]}", ">"),
        ("", " "),
    ])


def _prompt_message(sticky_agent: str) -> FormattedText:
    """Main-REPL prompt: leading newline for breathing room + `luxe` or
    `luxe (<mode>)`, then the colored `>>>`."""
    lead = f"\nluxe ({sticky_agent})" if sticky_agent else "\nluxe"
    return _styled_arrow_prompt(lead)


# Sub-prompt histories are kept in memory per label so ↑/↓ inside a
# plan-review loop or across clarifying answers works, but we don't
# pollute the main luxe>>> FileHistory. Lives for the luxe process
# lifetime; cleared on REPL exit.
_SUB_PROMPT_HISTORIES: dict[str, InMemoryHistory] = {}


def _ask_styled(lead: str) -> str:
    """One-shot styled sub-prompt — same colored `>>>` look as the main
    prompt. Keeps a per-label in-memory history so ↑/↓ recalls earlier
    entries within the same luxe session (plan commands stay in plan's
    buffer; save filenames in save's; etc.)."""
    from prompt_toolkit import prompt as _ptk_prompt
    key = lead.strip().lower()
    history = _SUB_PROMPT_HISTORIES.setdefault(key, InMemoryHistory())
    try:
        return _ptk_prompt(_styled_arrow_prompt(lead), history=history).strip()
    except (EOFError, KeyboardInterrupt):
        raise


def _make_prompt_session() -> PromptSession:
    """prompt_toolkit session with persistent history, arrow-key recall,
    and Alt/Esc+Enter to insert a newline inside a single prompt.

    Bracketed paste is on by default — pasting a multi-line block arrives
    as one buffer instead of N separate submissions.
    """
    hist_path = Path.home() / ".luxe" / "history"
    hist_path.parent.mkdir(parents=True, exist_ok=True)

    kb = KeyBindings()

    @kb.add("escape", "enter")  # Alt-Enter / Esc-Enter inserts a newline
    def _(event) -> None:  # noqa: ANN001
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=FileHistory(str(hist_path)),
        key_bindings=kb,
        enable_history_search=True,
        mouse_support=False,
    )

_HELP_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Core", [
        ("/help",                 "show this message"),
        ("/agents",                "list configured agents"),
        ("/models",                "list installed Ollama models"),
        ("/variants [family]",     "show released sizes per family"),
        ("/pull <tag>",            "download a model from the Ollama registry"),
        ("/context",               "show loaded/max context and RAM per agent"),
        ("/quit · /exit",          "leave luxe"),
        ("/clear",                 "drop the sticky agent"),
    ]),
    ("Tasks", [
        ("/tasks",                 "list recent tasks"),
        ("/tasks <goal>",          "plan + run in the background"),
        ("/tasks --sync <goal>",   "plan + run synchronously"),
        ("/tasks status [id]",     "snapshot of a task's status table"),
        ("/tasks log [id]",        "print last events from log.jsonl"),
        ("/tasks tail [id]",       "live-follow a task's event stream"),
        ("/tasks watch [id]",      "auto-refreshing dashboard"),
        ("/tasks abort [id]",      "signal a running background task"),
        ("/tasks save [id]",       "assemble subtasks into a report"),
    ]),
    ("Code intelligence", [
        ("/review <git-url>",      "clone/pull, review for flaws/bugs/security"),
        ("/refactor <git-url>",    "clone/pull, suggest optimizations"),
    ]),
    ("Tool library", [
        ("/tools",                 "list saved tools"),
        ("/tools show <name>",     "print source of a saved tool"),
        ("/tools remove <name>",   "delete a saved tool"),
    ]),
    ("Direct dispatch", [
        ("/general <prompt>",      "chat, Q&A"),
        ("/lookup <prompt>",       "quick factual lookup, snippet-only"),
        ("/research <prompt>",     "deep web investigation"),
        ("/calc <prompt>",         "arithmetic, estimation"),
        ("/writing <prompt>",      "prose, drafts, in-folder fs"),
        ("/code <prompt>",         "read/edit/run source"),
        ("/image <prompt>",        "generate an image"),
    ]),
    ("Turn control", [
        ("/retry",                 "rerun last prompt, same agent"),
        ("/redo <agent>",          "rerun last prompt, different agent"),
        ("/model <tag>",           "one-off model override next turn"),
        ("/params <text>",         "force banner params value"),
        ("/pin <text>",            "sticky note prepended to every prompt"),
        ("/pins",                  "list current pins"),
        ("/unpin [n]",             "remove pin n"),
        ("/history [n]",           "show last n session events"),
    ]),
    ("Sessions", [
        ("/session",               "current session id and path"),
        ("/save <name>",           "bookmark current session"),
        ("/sessions",              "list saved sessions"),
        ("/resume <id-or-name>",   "switch to another session"),
        ("/new",                   "start a fresh session"),
    ]),
    ("Memory & aliases", [
        ("/memory",                "open ~/.luxe/memory.md in $EDITOR"),
        ("/memory view",           "print current memory"),
        ("/memory clear",          "delete memory"),
        ("/alias add <name> <expansion>", "define a shortcut"),
        ("/alias list",            "list aliases"),
        ("/alias remove <name>",   "remove an alias"),
    ]),
]


def _render_help() -> "Group":
    """Build the /help block as column-aligned Rich grids, one per
    section. Every section's description column starts at the same
    column as the widest command in that section + 2 spaces — no
    ragged right edge. Command text goes through Text objects rather
    than markup strings so literal `[family]` / `[id]` aren't parsed
    as Rich style tags and eaten."""
    blocks: list = []
    for title, rows in _HELP_SECTIONS:
        blocks.append(Text.from_markup(f"[bold]{title}[/bold]"))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True)
        grid.add_column(no_wrap=True)
        for cmd, desc in rows:
            cmd_text = Text("  ")
            cmd_text.append(cmd, style="cyan")  # literal, no markup parsing
            grid.add_row(cmd_text, desc)
        blocks.append(grid)
        blocks.append(Text(""))
    return Group(*blocks)

BUILTIN_CMDS = {
    "/help", "/agents", "/session", "/models", "/quit", "/exit",
    "/retry", "/redo", "/history", "/model", "/params", "/pin", "/pins", "/unpin",
    "/save", "/sessions", "/resume", "/new", "/clear",
    "/memory", "/alias", "/variants", "/pull", "/context", "/tasks",
    "/review", "/refactor", "/tools",
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


def start(cfg: LuxeConfig, session: Session | None = None) -> None:
    if not ping():
        console.print(
            "[red]Ollama is not reachable at http://127.0.0.1:11434.[/red] "
            "Start it with [cyan]ollama serve[/cyan]."
        )
        return

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
            endpoint = a.endpoint or cfg.ollama_base_url
            params = parameter_size(a.model, endpoint)
            t.add_row(mark, a.name, a.model, params, a.display)
        console.print(t)
        return "consumed"
    if cmd == "/session":
        console.print(f"  id:   {state.sess.session_id}")
        console.print(f"  path: {state.sess.path}")
        return "consumed"
    if cmd == "/models":
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column()  # model tag
        t.add_column(justify="right")  # params
        for m in list_models():
            t.add_row(m, parameter_size(m, cfg.ollama_base_url))
        console.print(t)
        return "consumed"

    if cmd == "/variants":
        _print_variants(args[0] if args else None, cfg)
        return "consumed"

    if cmd == "/pull":
        if not args:
            console.print("[yellow]usage:[/yellow] /pull <tag>   e.g. /pull gemma3:4b")
            return "consumed"
        tag = args[0]
        if _pull_with_progress(tag, cfg.ollama_base_url):
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
        known_subs = ("status", "log", "abort", "save", "tail", "watch")
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
            _tasks_tail(args[1] if len(args) > 1 else None)
            return "consumed"
        if sub == "watch":
            _tasks_watch(args[1] if len(args) > 1 else None)
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
            endpoint = agent_cfg.endpoint or cfg.ollama_base_url
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
    state.last_endpoint = cfg.get(decision.agent).endpoint or cfg.ollama_base_url
    state.sticky_agent = decision.agent

    _print_stats(decision, result, state, cfg)


def _print_stats(decision, result, state: ReplState, cfg: LuxeConfig) -> None:
    ctx_total = context_length(state.last_model, state.last_endpoint)
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
