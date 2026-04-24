"""/review and /refactor entry point + plan review / clarify helpers."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from luxe.registry import LuxeConfig

console = Console()


def _start_review(url: str, mode: str, state: "ReplState", cfg: LuxeConfig) -> None:
    """Shared entry point for /review and /refactor. Clone/pull into cwd,
    plan a review-flavored task pinned to the `review` or `refactor`
    agent, then spawn it in the background."""
    from luxe.git import repo_name_from_url, resolve_repo
    from luxe.tasks import plan, spawn_background
    from luxe.tasks.model import Task, persist, task_id
    from luxe.repl.tasks import _print_launch_hints

    console.print(f"[dim]resolving[/dim] [cyan]{url}[/cyan][dim]...[/dim]")
    with console.status("[dim]git[/dim]", spinner="dots"):
        repo_path, status_msg = resolve_repo(url, Path.cwd())
    if repo_path is None:
        console.print(f"[red]{status_msg}[/red]")
        return
    console.print(f"[dim]{status_msg} → {repo_path}[/dim]")

    repo_label = repo_name_from_url(url) or repo_path.name
    from luxe.review import build_review_goal
    goal = build_review_goal(repo_label, repo_path, mode)

    # 30 min was too tight once review moved to qwen2.5:32b — on a
    # mid-sized repo a single inspection subtask can spend 13+ min
    # on three sequential grep/read_file rounds. 60 min gives the
    # 7-subtask plan room without making aborted runs obvious by
    # skipping the synthesis pass.
    task = Task(id=task_id(), goal=goal, max_wall_s=3600.0)
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
    from luxe.repl.prompt import _ask_styled
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
    from luxe.repl.prompt import _ask_styled
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
