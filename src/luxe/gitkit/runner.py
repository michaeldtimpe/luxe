"""gitkit runner — one read-only `run_single` pass per report.

Single-pass by contract (gitkit.sdd): no repair loop, no follow-up runs. The
caller is responsible for `set_repo_root` + building/setting BM25/symbol indices
(the CLI commands do this; the REPL reuses the session's resident indices).
"""

from __future__ import annotations

from pathlib import Path

from luxe.agents import prompts

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


def run_git_report(
    kind: str,
    *,
    cfg,
    repo_path: str | Path,
    languages,
    console,
    save: bool = True,
    expected_head: str | None = None,
) -> tuple[str, Path | None]:
    """Run one read-only analysis pass over `repo_path` and report the result.

    Args:
        kind: gitsummary | gitreview | gitrefactor.
        cfg: loaded PipelineConfig (provides oMLX URL, model, role).
        repo_path: already-resolved local repo path; repo_root + indices must
            already be set by the caller.
        languages: detected language frozenset for the repo.
        console: Rich console for output.
        save: when True, persist the markdown report under ~/.luxe/reports/.
        expected_head: if set (REPL reuse path), warn when the repo's current
            HEAD differs (resident indices may be stale).

    Returns:
        (report_text, saved_path | None).

    Side effects: one `run_single` call (read-only role); optional file write.
    """
    from rich.markdown import Markdown

    from luxe.agents.single import run_single
    from luxe.backend import Backend
    from luxe.gitkit import health
    from luxe.gitkit import store
    from luxe.mcp.server import make_read_only_role

    if kind not in KINDS:
        raise ValueError(f"unknown gitkit kind {kind!r}; expected {sorted(KINDS)}")
    task_type, ask, hint = KINDS[kind]

    if expected_head:
        cur = health.current_head(repo_path)
        if cur and cur != expected_head:
            console.print(
                f"[yellow]· repo HEAD moved since indices were built "
                f"({expected_head} → {cur}); search results may be stale.[/]")

    model = cfg.model_for_slot("chat")
    console.print(f"[dim]· {_TITLES[kind]} — model: {model} (read-only)[/]")
    console.print("[dim]· gathering git history + GitHub metadata…[/]")
    extra_context = health.gather_context(repo_path)

    backend = Backend(base_url=cfg.omlx_base_url, model=model)
    role_cfg = make_read_only_role(cfg.role("monolith"))
    goal = f"{ask}\n\n{hint}"

    result = run_single(
        backend, role_cfg,
        goal=goal, task_type=task_type, languages=languages,
        extra_context=extra_context, phase="chat", run_id=f"gitkit-{kind}",
    )
    text = (result.final_text or "").strip() or "(no report produced)"

    console.print()
    console.print(Markdown(text))
    console.print(
        f"\n[dim]· {result.steps} steps · {result.tool_calls_total} tool calls · "
        f"{result.wall_s:.1f}s · {result.completion_tokens} out-tok[/]")

    saved: Path | None = None
    if save:
        saved = store.save_report(
            repo_path, kind, text,
            meta={"model": model, "head": health.current_head(repo_path),
                  "repo": str(repo_path)},
        )
        console.print(f"[green]✓[/] report saved to [cyan]{saved}[/]")
    return text, saved
