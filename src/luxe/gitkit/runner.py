"""gitkit runner — one read-only `run_single` pass per report.

Single-pass by contract (gitkit.sdd): no repair loop, no follow-up runs. The
runner owns the full target lifecycle: it resolves the target repo (prompting
to clone a URL when the path is NOT a git working tree), sets `repo_root`,
builds the BM25/symbol indices for the target, runs one read-only pass, then
restores whatever repo_root/indices were resident before (so a `luxe chat`
session is left untouched after a `/gitsummary` over a freshly-cloned repo).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from luxe.agents import prompts

# Per-run generation ceiling for the FINAL report (safety margin, not the fix —
# WS1's prompt discipline keeps reports well under this). The first test hit the
# chat role's 8192 cap and truncated mid-report. Applied via a per-run
# model_copy in run_git_report; never mutates the shared role / chat.yaml.
GITKIT_MAX_TOKENS = 16384

# On-screen preview cap (full report is always saved + available via --verbose).
_PREVIEW_LINES = 30

_H1_RE = re.compile(r"^#\s", re.MULTILINE)


def extract_report(text: str) -> str:
    """Slice the report from its first level-1 (`# `) header onward, dropping any
    leading monologue the model emits before the report (WS1 safety net for the
    "treats the final turn as more reasoning" failure mode). If no header is
    found, return the text unchanged (never drop content)."""
    m = _H1_RE.search(text or "")
    return text[m.start():] if m else text

# kind -> (task_type overlay reused, goal ask, directive HINT). No new task
# types: each maps onto an existing overlay; the per-kind directive rides in the
# goal (gitkit.sdd / agents.sdd single-source rule).
KINDS: dict[str, tuple[str, str, str]] = {
    "gitsummary": (
        "summarize",
        "Summarize and assess the repository in the current working directory.",
        prompts.GIT_SUMMARY_HINT,
    ),
    "gitreview": (
        "review",
        "Review the codebase in the current working directory for serious bugs "
        "and security issues.",
        prompts.GIT_REVIEW_HINT,
    ),
    "gitrefactor": (
        "review",
        "Analyze the codebase in the current working directory and propose a "
        "structural refactor plan.",
        prompts.GIT_REFACTOR_HINT,
    ),
}

_TITLES = {
    "gitsummary": "Repository summary & risk assessment",
    "gitreview": "Bug & security review",
    "gitrefactor": "Refactor plan",
}


def _looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@", "ssh://"))


def _derive_dest(base_path: str | Path, url: str) -> Path:
    """Local destination for a clone: `<base dir>/<repo name>`, de-duplicated."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    name = name or "repo"
    base = Path(base_path)
    parent = base if base.is_dir() else base.parent
    dest = parent / name
    i = 2
    while dest.exists():
        dest = parent / f"{name}-{i}"
        i += 1
    return dest


def _clone(url: str, dest: Path, *, full_history: bool, console) -> bool:
    """Clone `url` into `dest`. Full history for summaries; shallow otherwise."""
    args = ["--filter=blob:none"] if full_history else ["--depth=1"]
    console.print(f"[dim]· cloning {url} → {dest}…[/]")
    proc = subprocess.run(
        ["git", "clone", *args, url, str(dest)], capture_output=True, text=True)
    if proc.returncode != 0:
        console.print(f"[red]clone failed:[/] {(proc.stderr or proc.stdout).strip()}")
        return False
    return True


def _resolve_or_clone(path, *, full_history: bool, console, reader) -> str | None:
    """Return a local git-repo path for `path`. If `path` is not a git working
    tree, prompt the user for a URL and clone a local copy (showing the path);
    returns None if the user cancels or the clone fails."""
    from luxe.gitkit import health

    if health.is_git_repo(path):
        return str(Path(path).resolve())

    console.print(f"[yellow]· {Path(path).resolve()} is not a git repository.[/]")
    url = reader("  git URL to clone (blank to cancel): ").strip()
    if not url:
        return None
    dest = _derive_dest(path, url)
    confirm = reader(f"  clone into {dest}? [Y/n]: ").strip().lower()
    if confirm in ("n", "no"):
        return None
    if not _clone(url, dest, full_history=full_history, console=console):
        return None
    return str(dest.resolve())


def _activity_callbacks(update):
    """Build (on_tool_event, on_token) that coalesce tool calls into a phased
    status string passed to `update(text)`: `analyzing… read_file (31) · grep
    (12)` while tools fire, then `writing report…` once the model starts emitting
    prose after the last tool. Factored out so the coalescing/phasing is unit-
    testable without a TTY (the spinner itself only runs when console.is_terminal)."""
    from collections import Counter

    counts: Counter = Counter()
    state = {"writing": False}

    def _text() -> str:
        if not counts:
            return "analyzing…"
        top = " · ".join(f"{n} ({c})" for n, c in counts.most_common(4))
        return f"analyzing… {top}"

    def on_event(tc):
        counts[getattr(tc, "name", "?")] += 1
        state["writing"] = False  # more tools → back to analyzing
        update(_text())

    def on_token(_delta):
        # First prose after at least one tool call = the report being written.
        if counts and not state["writing"]:
            state["writing"] = True
            update("writing report…")

    return on_event, on_token


def run_git_report(
    kind: str,
    *,
    cfg,
    repo_path: str | Path,
    console,
    reader=None,
    save: bool = True,
    verbose: bool = False,
    expected_head: str | None = None,
) -> tuple[str, Path | None]:
    """Run one read-only analysis pass over a repo and report the result.

    Args:
        kind: gitsummary | gitreview | gitrefactor.
        cfg: loaded PipelineConfig (provides oMLX URL, model, role).
        repo_path: target path; if it is not a git working tree, the user is
            prompted to clone a URL into a local copy.
        console: Rich console for output.
        reader: prompt callable (defaults to `console.input`) — injectable for
            tests / non-interactive callers.
        save: when True, persist the FULL markdown report under ~/.luxe/reports/.
        verbose: print the full report on screen; otherwise a truncated preview.
        expected_head: if set AND the resident indices already cover the target,
            warn when the repo's HEAD has moved (indices may be stale).

    Returns:
        (report_text, saved_path | None); ("", None) if the user cancels.

    Side effects: one `run_single` call (read-only role, with a per-run token
    headroom copy); BM25/symbol indices and repo_root are swapped to the target
    and restored afterward; an optional report file write; an optional clone.
    """
    from rich.markdown import Markdown

    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    from luxe.agents.single import run_single
    from luxe.backend import Backend
    from luxe.chat.render import truncate_for_display
    from luxe.cli import _detect_languages_for_repo
    from luxe.gitkit import health, store
    from luxe.mcp.server import make_read_only_role
    from luxe.tools.fs import get_repo_root, set_repo_root

    if kind not in KINDS:
        raise ValueError(f"unknown gitkit kind {kind!r}; expected {sorted(KINDS)}")
    task_type, ask, hint = KINDS[kind]
    reader = reader or console.input

    target = _resolve_or_clone(
        repo_path, full_history=(kind == "gitsummary"),
        console=console, reader=reader)
    if target is None:
        console.print("[yellow]· cancelled.[/]")
        return "", None

    # Index/repo-root lifecycle: reuse resident indices when they already cover
    # the target (the common REPL case); otherwise build for the target and
    # restore the prior global state afterward (so chat is left untouched).
    prev_root = get_repo_root()
    prev_bm25 = search_mod._index   # module-level resident index (no public getter)
    prev_sym = symbols_mod._index
    reuse = prev_root is not None and str(prev_root) == target
    swapped = False

    try:
        if reuse and expected_head:
            cur = health.current_head(target)
            if cur and cur != expected_head:
                console.print(
                    f"[yellow]· repo HEAD moved since indices were built "
                    f"({expected_head} → {cur}); search results may be stale.[/]")
        if not reuse:
            set_repo_root(target)
            console.print("[dim]· Indexing repository for search…[/]")
            search_mod.set_index(search_mod.build_bm25_index(target))
            symbols_mod.set_index(symbols_mod.build_symbol_index(target))
            swapped = True

        languages = _detect_languages_for_repo(target)
        model = cfg.model_for_slot("chat")
        console.print(f"[dim]· {_TITLES[kind]} — model: {model} (read-only)[/]")

        backend = Backend(base_url=cfg.omlx_base_url, model=model)
        # WS1.3: per-run token headroom so the final report can't truncate
        # mid-thought. A copy — never mutates the shared role / chat.yaml.
        role_cfg = make_read_only_role(cfg.role("monolith")).model_copy(
            update={"max_tokens_per_turn": GITKIT_MAX_TOKENS})
        goal = f"{ask}\n\n{hint}"

        def _do_run(on_event=None, on_token=None):
            extra_context = health.gather_context(target)
            return run_single(
                backend, role_cfg, goal=goal, task_type=task_type,
                languages=languages, extra_context=extra_context,
                on_tool_event=on_event, on_token=on_token,
                phase="chat", run_id=f"gitkit-{kind}",
            )

        # WS2: phased spinner + coalesced tool counts while the model works
        # (terminal only; gitkit is self-contained, no chat LiveActivity).
        if console.is_terminal:
            with console.status("[dim]gathering context & loading model…[/]",
                                spinner="dots") as status:
                on_event, on_token = _activity_callbacks(
                    lambda t: status.update(f"[dim]{t}[/]"))
                result = _do_run(on_event, on_token)
        else:
            console.print("[dim]· gathering git history + GitHub metadata…[/]")
            result = _do_run()

        # WS1.2: slice off any leading monologue before the report's first header.
        report = extract_report((result.final_text or "").strip()) or \
            "(no report produced)"

        # Full report is always saved; on screen show a preview unless verbose.
        saved: Path | None = None
        if save:
            saved = store.save_report(
                target, kind, report,
                meta={"model": model, "head": health.current_head(target),
                      "repo": target},
            )

        console.print()
        if verbose:
            console.print(Markdown(report))
        else:
            shown, hidden = truncate_for_display(report, max_lines=_PREVIEW_LINES)
            console.print(Markdown(shown))
            if hidden:
                console.print(f"[dim]… +{hidden} more lines — full report below[/]")
        console.print(
            f"\n[dim]· {result.steps} steps · {result.tool_calls_total} tool calls "
            f"· {result.wall_s:.1f}s · {result.completion_tokens} out-tok[/]")
        if saved:
            tail = "" if verbose else " — re-run with --verbose / -v for the full report"
            console.print(f"[green]✓[/] report saved to [cyan]{saved}[/]{tail}")
        return report, saved
    finally:
        if swapped:
            if prev_root is not None:
                set_repo_root(prev_root)
            if prev_bm25 is not None:
                search_mod.set_index(prev_bm25)
            else:
                search_mod.reset_index()
            if prev_sym is not None:
                symbols_mod.set_index(prev_sym)
            else:
                symbols_mod.reset_index()
