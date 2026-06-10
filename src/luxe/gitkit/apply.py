"""gitchange --apply / gitapply — the gated executor (gitkit's SOLE write path).

Orchestrates N MONO `run_single` calls from Python (the deep.py/compare precedent —
NOT an in-agent repair loop / goal-runner): one per ordered plan step, each in WRITE
mode on a dedicated branch, gated by a per-step diff + verify + interactive
keep|discard. The SIX mandatory invariants (gitkit.sdd):
  1. INTERACTIVE-ONLY — requires a TTY; never applies unattended (the sweep never
     applies).
  2. CLEAN-TREE-ONLY — aborts on a dirty working tree.
  3. NON-DEFAULT-BRANCH — never main/master/default; always a dedicated
     `gitchange/<head>-<rand>` branch.
  4. PER-STEP GATING — show the diff + run the step's verify, then keep (commit on
     the branch) or discard (revert just that step); `depends_on` is respected.
  5. NEVER push, NEVER merge, NEVER commit to the default branch.
  6. Front-end Python orchestration — each step is exactly ONE `run_single` (no
     retry / repair loop).
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

from luxe.agents import prompts

_VERIFY_HINTS = ("pytest", "test", "make ", "npm ", "cargo ", "go ", "python",
                 "bash ", "./", "sh ", "tox", "ruff", "mypy", "jest")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _is_tty(console) -> bool:
    """Interactive guard — both a Rich terminal AND a real stdin. Module-level so
    tests can monkeypatch it."""
    try:
        return bool(getattr(console, "is_terminal", False)) and sys.stdin.isatty()
    except Exception:
        return False


def _require_clean(repo: Path, console, when: str) -> bool:
    """TOCTOU re-check of invariant 2: the tree must STILL be clean at `when`
    (branch creation and plan generation can be minutes-to-hours after the
    entry check). The sanctioned `.luxe/gitkit/` mirror is exempt —
    run_git_report writes it during plan generation (gitkit.sdd's one
    orchestrator write). Prints the offending paths on dirt."""
    r = _git(repo, "status", "--porcelain")
    dirt = []
    for ln in r.stdout.splitlines():
        if not ln.strip():
            continue
        # porcelain v1: "XY path" (renames: "XY old -> new"); untracked dirs
        # collapse to "?? .luxe/", so match the path prefix, not a substring.
        paths = ln[3:].split(" -> ")
        if all(p.strip('"').startswith(".luxe") for p in paths if p):
            continue
        dirt.append(ln)
    if not dirt:
        return True
    console.print(f"[red]· working tree became dirty {when} — aborting before "
                  "any step runs.[/]")
    for ln in dirt[:20]:
        console.print(f"[dim]    {ln}[/]")
    return False


def _abort_branch(repo: Path, console, branch: str, orig_branch: str) -> None:
    """Restore the original branch and delete the dedicated gitchange branch —
    an orphaned `gitchange/*` branch must never survive an abort (it pollutes
    subsequent runs)."""
    _git(repo, "checkout", orig_branch)
    _git(repo, "branch", "-D", branch)
    console.print(f"[dim]· restored {orig_branch}; removed {branch}.[/]")


def _step_block(step: dict, plan: dict, survey: str) -> str:
    """Pure-data context blocks for the apply pass (directive is GIT_APPLY_STEP_HINT)."""
    overview = {"summary": plan.get("summary", ""),
                "steps": [{"id": s["id"], "title": s["title"]}
                          for s in plan.get("steps", [])]}
    parts = [f"<step>\n{json.dumps(step, indent=1)}\n</step>",
             "<plan>\nThe full plan (context only — apply ONLY the <step> above):\n"
             f"{json.dumps(overview, indent=1)}\n</plan>"]
    if survey:
        parts.append(f"<survey>\n{survey}\n</survey>")
    return "\n\n".join(parts)


def _run_verify(cmd: str, repo: Path, timeout: int) -> tuple[bool | None, str]:
    """Run the step's verify command if it looks like a shell command; else treat it
    as advisory (returns (None, ''))."""
    cmd = (cmd or "").strip()
    if not cmd or not any(h in cmd for h in _VERIFY_HINTS):
        return None, ""
    try:
        r = subprocess.run(["bash", "-lc", cmd], cwd=str(repo),
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr)[-1500:]
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)


def run_apply(*, repo_path: str, cfg, console, reader=None, deep: bool | None = None,
              rebuild_map: bool = False, run_single_fn=None) -> int:
    """Execute a saved gitchange against a LOCAL repo under the six invariants.
    Returns a process exit code (0 ok, non-zero on abort)."""
    from luxe.gitkit import health, plan as plan_mod, store
    from luxe import pr
    reader = reader or console.input
    repo = Path(repo_path)

    # (1) must be a git working tree — apply NEVER clones.
    if not health.is_git_repo(repo):
        console.print(f"[red]· {repo} is not a git repository — gitapply needs a checkout.[/]")
        return 2
    # (2) interactive-only.
    if not _is_tty(console):
        console.print("[red]· gitchange --apply is interactive-only; refusing to apply "
                      "unattended.[/]")
        return 2
    # (3) clean tree.
    if pr.is_dirty(repo):
        console.print("[red]· working tree is dirty — commit or stash first "
                      "(apply needs a clean, attributable starting point).[/]")
        return 2
    # (4) dedicated, non-default branch — ALWAYS (never main/master/default).
    orig_branch = _current_branch(repo)
    default = pr.detect_base_branch(repo)
    head = health.current_head(repo)
    if orig_branch in ("main", "master") or orig_branch == default:
        console.print(f"[dim]· on default branch '{orig_branch}' — switching to a "
                      "dedicated branch.[/]")
    branch = f"gitchange/{(head or 'nohead')[:8]}-{uuid.uuid4().hex[:4]}"
    if _git(repo, "checkout", "-b", branch).returncode != 0:
        console.print(f"[red]· could not create branch {branch}.[/]")
        return 2
    console.print(f"[green]·[/] applying on branch [cyan]{branch}[/] "
                  f"(original: {orig_branch}) — main is never touched")
    if not _require_clean(repo, console, "after branch creation"):
        _abort_branch(repo, console, branch, orig_branch)
        return 2

    # (5) load (or generate) the plan for the current HEAD.
    plan = plan_mod.latest_plan_for(repo, head)
    if plan is None:
        console.print("[dim]· no saved plan for this HEAD — generating one (read-only)…[/]")
        from luxe.gitkit import run_git_report
        run_git_report("gitchange", cfg=cfg, repo_path=str(repo), console=console,
                       reader=reader, save=True, deep=deep, rebuild_map=rebuild_map)
        plan = plan_mod.latest_plan_for(repo, head)
    # plan generation can run for a long time — re-check invariant 2 before
    # any step touches the tree.
    if not _require_clean(repo, console, "during plan generation"):
        _abort_branch(repo, console, branch, orig_branch)
        return 2
    if not plan or not plan.get("steps"):
        console.print("[yellow]· no plan steps to apply.[/]")
        _abort_branch(repo, console, branch, orig_branch)
        return 1
    # (6) order (abort on dependency cycle).
    try:
        steps = plan_mod.order_steps(plan)
    except ValueError as e:
        console.print(f"[red]· plan has a dependency cycle: {e}[/]")
        _abort_branch(repo, console, branch, orig_branch)
        return 2

    # --- write environment (the inverse of gitkit-today: FULL role, not read-only)
    from luxe import search as search_mod
    from luxe import symbols as symbols_mod
    from luxe.backend import Backend
    from luxe.cli import _detect_languages_for_repo
    from luxe.tools.fs import get_repo_root, set_repo_root
    if run_single_fn is None:
        from luxe.agents.single import run_single as run_single_fn

    survey = ""
    smap = store.reports_dir(repo) / "map" / "survey_notes.md"
    if smap.is_file():
        survey = smap.read_text()[:4000]

    prev_root = get_repo_root()
    prev_bm25 = search_mod._index
    prev_sym = symbols_mod._index
    backend = Backend(base_url=cfg.omlx_base_url, model=cfg.model_for_slot("chat"))
    role = cfg.role("monolith")    # FULL, write-enabled — NOT make_read_only_role
    languages = _detect_languages_for_repo(str(repo))
    timeout = int(getattr(cfg, "test_timeout_s", 300) or 300)

    kept: list[str] = []
    discarded: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    try:
        set_repo_root(str(repo))
        search_mod.set_index(search_mod.build_bm25_index(str(repo)))
        symbols_mod.set_index(symbols_mod.build_symbol_index(str(repo)))
        branch_head = pr.head_sha(repo)
        for step in steps:
            if any(d not in kept for d in step.get("depends_on", [])):
                console.print(f"[yellow]· skip {step['id']} — a prerequisite was "
                              "not kept.[/]")
                skipped.append(step["id"])
                continue
            console.print(f"\n[bold]· {step['id']}: {step['title']}[/]  "
                          f"(risk: {step.get('risk', '?')})")
            # ONE mono pass — no retry / repair loop (invariant 6: a raised
            # pass is reverted and recorded, NEVER re-run).
            try:
                run_single_fn(
                    backend, role,
                    goal="Apply ONLY this single refactor step to the working tree.\n\n"
                         + prompts.GIT_APPLY_STEP_HINT,
                    task_type="implement", languages=languages,
                    extra_context=_step_block(step, plan, survey),
                    run_id=f"gitchange-apply-{step['id']}", phase="main")
            except Exception as e:
                console.print(f"[red]· step {step['id']} raised: "
                              f"{type(e).__name__}: {e}[/]")
                # Kept steps are already committed (keep => add -A + commit
                # before the next pass starts), so this full-tree revert only
                # ever discards the FAILED step's partial writes.
                _git(repo, "checkout", "--", ".")
                _git(repo, "clean", "-fd")
                failed.append(step["id"])
                ans = reader("  [c]ontinue with next step / [a]bort? [c/A]: ").strip().lower()
                if ans in ("c", "continue"):
                    continue
                console.print("[yellow]· aborted — remaining steps not run.[/]")
                break
            _adds, _dels, diff = pr.diff_against_base(repo, branch_head)
            if not diff.strip():
                console.print("[yellow]· no changes produced — skipping.[/]")
                skipped.append(step["id"])
                continue
            from rich.syntax import Syntax
            console.print(Syntax(diff[:8000], "diff", theme="ansi_dark",
                                 word_wrap=True))
            ok, tail = _run_verify(step.get("verify", ""), repo, timeout)
            if ok is True:
                console.print("[green]· verify passed[/]")
            elif ok is False:
                console.print(f"[red]· verify FAILED[/]\n[dim]{tail[-600:]}[/]")
            else:
                console.print(f"[dim]· verify (advisory): {step.get('verify', '—')}[/]")
            default_keep = ok is not False
            prompt = "  keep or discard? [" + ("K/d" if default_keep else "k/D") + "]: "
            ans = reader(prompt).strip().lower()
            keep = ans in ("k", "keep", "y", "yes") or (ans == "" and default_keep)
            if keep:
                _git(repo, "add", "-A")
                _git(repo, "commit", "-m", f"gitchange {step['id']}: {step['title']}")
                branch_head = pr.head_sha(repo)
                kept.append(step["id"])
                console.print(f"[green]· kept {step['id']} (committed on {branch})[/]")
            else:
                _git(repo, "checkout", "--", ".")
                _git(repo, "clean", "-fd")
                discarded.append(step["id"])
                console.print(f"[yellow]· discarded {step['id']}[/]")
    finally:
        if prev_root is not None:
            set_repo_root(prev_root)
        (search_mod.set_index(prev_bm25) if prev_bm25 is not None
         else search_mod.reset_index())
        (symbols_mod.set_index(prev_sym) if prev_sym is not None
         else symbols_mod.reset_index())

    console.print(f"\n[bold]· done.[/] kept={len(kept)} discarded={len(discarded)} "
                  f"skipped={len(skipped)} failed={len(failed)} "
                  f"on branch [cyan]{branch}[/]")
    console.print(f"[dim]  review:  git -C {repo} log {orig_branch}..{branch}[/]")
    console.print(f"[dim]  merge:   git -C {repo} checkout {orig_branch} && "
                  f"git merge {branch}   (you do this — apply never merges)[/]")
    console.print(f"[dim]  discard: git -C {repo} checkout {orig_branch} && "
                  f"git branch -D {branch}[/]")
    return 0
