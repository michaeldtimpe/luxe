"""Typer entry point: `luxe`, `luxe resume`, `luxe list`, `luxe agents`."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

# Side-effect import: pulls ~/.luxe/secrets.env into os.environ before
# any backend code reads OMLX_API_KEY. Must run before `from luxe_cli import
# repl` since the REPL imports backend.py which captures the env var
# at make_backend() call time.
from luxe_cli.secrets import load_secrets, warn_missing_omlx_key
load_secrets()

from luxe_cli import repl  # noqa: E402
from luxe_cli.registry import load_config  # noqa: E402
from luxe_cli.session import Session, latest_session, list_sessions  # noqa: E402

app = typer.Typer(add_completion=False, invoke_without_command=True, no_args_is_help=False)
console = Console()


@app.callback()
def main(
    ctx: typer.Context,
    session_id: str = typer.Option(None, "--session", "-s", help="Resume a session by id"),
) -> None:
    """Start an interactive luxe REPL. Subcommands available too."""
    if ctx.invoked_subcommand is not None:
        return
    cfg = load_config()
    warning = warn_missing_omlx_key(cfg)
    if warning:
        console.print(f"[yellow][!] {warning}[/yellow]")
    sess = _resolve_session(cfg, session_id)
    repl.start(cfg, session=sess)


@app.command()
def resume() -> None:
    """Resume the most recent session."""
    cfg = load_config()
    root = Path(cfg.session_dir).expanduser()
    latest = latest_session(root)
    if not latest:
        console.print("[yellow]no prior sessions[/yellow]")
        return
    sess = Session.load(latest)
    _preview_session(sess)
    repl.start(cfg, session=sess)


def _preview_session(sess: Session, n: int = 4) -> None:
    events = sess.read_all()
    if not events:
        return
    console.print(f"[dim]resuming {sess.session_id} — last {min(n, len(events))} events:[/dim]")
    for e in events[-n:]:
        role = e.get("role", "?")
        agent = e.get("agent", "")
        text = str(e.get("content") or e.get("tool") or "")
        if len(text) > 110:
            text = text[:110] + "…"
        console.print(f"  [dim]{role}/{agent}:[/dim] {text}")
    console.print()


@app.command("list")
def list_cmd() -> None:
    """List saved sessions, newest first."""
    cfg = load_config()
    root = Path(cfg.session_dir).expanduser()
    sessions = list_sessions(root)
    if not sessions:
        console.print("[yellow]no sessions yet[/yellow]")
        return
    for p in sessions:
        console.print(f"  {p.stem}")


@app.command()
def clean(
    days: int = typer.Option(7, "--days", help="Delete sessions older than N days"),
) -> None:
    """Delete session files older than --days (default 7)."""
    import time

    cfg = load_config()
    root = Path(cfg.session_dir).expanduser()
    if not root.exists():
        console.print("[yellow]no session dir[/yellow]")
        return
    cutoff = time.time() - days * 86400
    removed = 0
    for p in root.glob("*.jsonl"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError as e:
            console.print(f"[yellow]skip {p.name}: {e}[/yellow]")
    console.print(f"[green]✓[/green] removed {removed} session(s) older than {days}d")


@app.command()
def agents() -> None:
    """List configured agents."""
    cfg = load_config()
    for a in cfg.agents:
        mark = "x" if a.enabled else " "
        console.print(f"  \\[{mark}] {a.name:10s} {a.model:30s} {a.display}")


@app.command()
def update() -> None:
    """Pull latest luxe and reinstall in-place.

    Picks the installer that fits the project: `uv pip install -e .` when a
    `uv.lock` is present (uv-managed venvs typically ship without pip), else
    `python -m pip install -e .`.
    """
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent  # luxe/
    while not (root / "pyproject.toml").exists():
        if root == root.parent:
            console.print("[red]pyproject.toml not found[/red]")
            raise typer.Exit(1)
        root = root.parent
    console.print(f"[dim]root:[/dim] {root}")

    uv_managed = (root / "uv.lock").exists()
    uv_bin = shutil.which("uv")
    if uv_managed and uv_bin:
        install_cmd = [uv_bin, "pip", "install", "-e", "."]
    else:
        install_cmd = [sys.executable, "-m", "pip", "install", "-e", "."]
        if uv_managed and not uv_bin:
            console.print(
                "[yellow]uv.lock present but `uv` not on PATH — falling back to pip "
                "(may fail on uv-created venvs)[/yellow]"
            )

    for cmd in (["git", "pull"], install_cmd):
        console.print(f"[cyan]$[/cyan] {' '.join(cmd)}")
        r = subprocess.run(cmd, cwd=root)
        if r.returncode != 0:
            raise typer.Exit(r.returncode)
    console.print("[green]✓[/green] luxe updated")


@app.command()
def analyze(
    repo: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True),
    out: Path = typer.Option(None, "--out", help="Output markdown path"),
    model: str = typer.Option(None, "--model", help="Override code agent model"),
    review: bool = typer.Option(False, "--review", help="Route through the review agent (background task) instead of the code-eval pipeline"),
    no_plan_cache: bool = typer.Option(False, "--no-plan-cache", help="Force a fresh planner decomposition instead of reusing the cached subtask list for (repo, mode)"),
) -> None:
    """Run a read-only code review on REPO. Produces a markdown report."""
    if review:
        from luxe_cli.review import start_review_task

        cfg = load_config()
        console.print(f"[bold]Reviewing[/bold] [cyan]{repo}[/cyan]")
        try:
            tid = start_review_task(
                repo, mode="review", cfg=cfg, use_plan_cache=not no_plan_cache,
            )
        except RuntimeError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ spawned review task[/green] {tid}")
        console.print(f"[dim]tail:[/dim] luxe → /tasks tail {tid}")
        return

    from luxe_cli.agents import code
    from luxe_cli.backend import make_backend
    from luxe_cli.tools import fs

    cfg = load_config()
    code_cfg = cfg.get("code")
    if model:
        code_cfg = code_cfg.model_copy(update={"model": model})

    fs.set_repo_root(repo)

    out = out or (
        Path("results/luxe_eval/code_analysis") / f"{repo.name}__{code_cfg.model.replace(':', '_')}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    task = _ANALYZE_TASK
    console.print(f"[bold]Analyzing[/bold] [cyan]{repo}[/cyan] with [cyan]{code_cfg.model}[/cyan]")
    console.print(f"[dim]Output → {out}[/dim]")

    backend = make_backend(
        code_cfg.model, base_url=cfg.resolve_endpoint(code_cfg)
    )
    with console.status("[cyan]code[/cyan] analyzing...", spinner="dots"):
        result = code.run(
            backend, code_cfg, task=task, read_only=True,
        )

    header = (
        f"# Analysis: `{repo.name}`\n\n"
        f"- **Repo:** `{repo}`\n- **Model:** `{code_cfg.model}`\n"
        f"- **Steps:** {result.steps_taken}, **Tool calls:** {result.tool_calls_total}\n"
    )
    if result.aborted:
        header += f"- **ABORTED:** {result.abort_reason}\n"
    out.write_text(header + "\n---\n\n" + (result.final_text or "_(no output)_"))
    console.print(f"\n[green]✓ saved[/green] {out}")


_ANALYZE_TASK = """Analyze this code repository. You MUST follow the exploration protocol
below before writing any conclusions.

### Exploration protocol (DO NOT skip steps)

1. `list_dir(".")` — get top-level layout.
2. `read_file` the README (or AGENTS.md / ARCHITECTURE.md if present) to
   understand purpose and stack.
3. `read_file` the manifest (`package.json`, `pyproject.toml`,
   `requirements.txt`, `Cargo.toml`, `go.mod`, etc.).
4. Use `glob` or `list_dir` to find the main source directory.
5. `read_file` the **top 5 largest or most likely-central source files**
   (entry points, main modules). Use `list_dir` to see sizes.
6. `grep` for at least two of: `TODO`, `FIXME`, `XXX`, `except\\s*:` (bare
   except in Python), `console.log` (stray debug), `print(` in
   non-entry-point files, hardcoded URLs / secrets, duplicated
   constants across files.

You must make at least **8 tool calls** across these steps. Do not write
conclusions until you have actually read the code — grounded findings
only, no speculation.

### Report format

After exploration, produce a single markdown report with exactly three
sections:

## Bugs
List concrete bugs you **found in files you actually read**. Each bug
must name the file and the specific line or symbol, explain the issue,
and suggest a fix. If after thorough exploration you can't find any real
bugs, say so honestly — do not invent issues. Do not list bugs based on
hypotheses — only on code you saw.

## Refactor
Pick ONE meaningful refactor opportunity anchored to code you read.
Describe the change and show the proposed change as a unified diff
(```diff ... ```). The diff must reference real file paths and real
lines — do not fabricate line numbers or contents. Keep it minimal.

## Features
Suggest 3 feature improvements that fit the repo's apparent purpose.
One short paragraph each. Do NOT implement them — just describe what
and why.

Be specific and concrete. If unsure of a fact, use another `read_file`
or `grep` to check before claiming it.
"""


def _resolve_session(cfg, session_id: str | None) -> Session | None:
    if not session_id:
        return None
    root = Path(cfg.session_dir).expanduser()
    p = root / f"{session_id}.jsonl"
    if not p.exists():
        console.print(f"[red]session not found:[/red] {p}")
        raise typer.Exit(1)
    return Session.load(p)


if __name__ == "__main__":
    app()
