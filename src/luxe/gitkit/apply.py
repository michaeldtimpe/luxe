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
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

from rich.markup import escape

from luxe import gitcmd
from luxe.agents import prompts
from luxe.cancel import ChatCancelled
from luxe.repo_index import _detect_languages_for_repo

# A plan step's `verify` is MODEL-WRITTEN text: either a shell command or a
# behavior to preserve ("preserve the public API"). It is treated as a command
# only when its FIRST token is one of these runners (or a ./path) — a
# substring test read "latest" as "test" — and even then it only runs after
# the operator has seen it and answered y (default N).
_VERIFY_RUNNERS = frozenset({
    "pytest", "python", "python3", "tox", "nox", "ruff", "mypy", "pyright",
    "uv", "poetry", "hatch", "make", "just", "npm", "npx", "pnpm", "yarn",
    "node", "jest", "vitest", "cargo", "go", "bash", "sh", "zsh", "gradle",
    "mvn", "bundle", "rake", "rspec", "ctest", "dotnet", "swift", "mix",
})
# The sanctioned orchestrator write (gitkit.sdd) — exempt from the clean-tree
# checks, git-excluded for the run, never part of a step's commit.
_MIRROR = ".luxe/gitkit/"
_COMMIT_TIMEOUT_S = 300


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return gitcmd.run_in(repo, *args)


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _revert_to_head(repo: Path) -> None:
    """Throw away every uncommitted write since the last kept step. Kept steps
    are commits, so HEAD is exactly "the plan so far". `reset --hard` (not
    `checkout -- .`) also drops the intent-to-add entries `diff_against_base`
    stages with `add -N` — a checkout restores those as EMPTY files that the
    next kept step then commits. `clean -fd` leaves ignored/excluded files
    (the mirror) alone."""
    _git(repo, "reset", "-q", "--hard", "HEAD")
    _git(repo, "clean", "-fdq")


def _exclude_mirror(repo: Path) -> None:
    """Add the gitkit mirror to `.git/info/exclude` so plan generation's
    mirror write neither dirties the tree nor lands in a step's commit.
    Best-effort (a read-only .git just means the mirror stays visible — the
    clean checks still exempt it)."""
    r = _git(repo, "rev-parse", "--git-path", "info/exclude")
    if r.returncode != 0 or not r.stdout.strip():
        return
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = repo / p
    line = "/" + _MIRROR
    try:
        existing = p.read_text() if p.is_file() else ""
        if line in existing.splitlines():
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        p.write_text(f"{existing}{sep}{line}\n")
    except OSError:
        pass


def _restore_tracked_mirror(repo: Path) -> None:
    """A COMMITTED mirror is rewritten by plan generation — restore it before
    the steps so every step's diff and revert starts from a clean HEAD (the
    refreshed copy stays canonical under ~/.luxe/reports/)."""
    r = _git(repo, "diff", "--name-only", "HEAD", "--", _MIRROR)
    if r.returncode == 0 and r.stdout.strip():
        _git(repo, "checkout", "HEAD", "--", _MIRROR)


def _is_tty(console) -> bool:
    """Interactive guard — both a Rich terminal AND a real stdin. Module-level so
    tests can monkeypatch it."""
    try:
        return bool(getattr(console, "is_terminal", False)) and sys.stdin.isatty()
    except Exception:
        return False


def _dirty_paths(repo: Path) -> list[str] | None:
    """Porcelain entries other than the sanctioned mirror, or None when `git
    status` itself failed (callers treat that as DIRTY — an unknown tree is
    not a clean one). `-z` + `--untracked-files=all` give one unquoted path
    per entry, so the exemption is exactly `.luxe/gitkit/`, never the rest of
    `.luxe/` (memory.md is user content)."""
    r = _git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
    if r.returncode != 0:
        return None
    dirt: list[str] = []
    toks = r.stdout.split("\0")
    i = 0
    while i < len(toks):
        ent = toks[i]
        i += 1
        if not ent.strip():
            continue
        paths = [ent[3:]]
        if ent[:1] in ("R", "C"):          # renames/copies: "XY new\0old"
            paths.append(toks[i] if i < len(toks) else "")
            i += 1
        if all(p.startswith(_MIRROR) for p in paths if p):
            continue
        dirt.append(f"{ent[:2]} {' <- '.join(p for p in paths if p)}")
    return dirt


def _require_clean(repo: Path, console, when: str) -> bool:
    """Invariant 2, checked on entry and re-checked (TOCTOU) after branch
    creation and after plan generation — which can be minutes-to-hours after
    the entry check. The sanctioned `.luxe/gitkit/` mirror is exempt —
    run_git_report writes it during plan generation (gitkit.sdd's one
    orchestrator write). Prints the offending paths on dirt."""
    dirt = _dirty_paths(repo)
    if dirt == []:
        return True
    if dirt is None:
        console.print(f"[red]· `git status` failed {when} — refusing to apply "
                      "over a tree whose state is unknown.[/]")
        return False
    console.print(f"[red]· working tree is dirty {when} — commit or stash "
                  "first (apply needs a clean, attributable starting point).[/]")
    for ln in dirt[:20]:
        console.print(f"[dim]    {ln}[/]")
    return False


def _abort_branch(repo: Path, console, branch: str, orig_ref: str,
                  orig_sha: str) -> None:
    """Restore the original checkout and delete the dedicated gitchange branch
    — an orphaned `gitchange/*` branch must never survive an abort (it
    pollutes subsequent runs). A detached HEAD (`orig_ref == "HEAD"`) is
    restored by SHA: `checkout HEAD` would be a no-op that leaves the repo on
    the gitchange branch and makes the delete fail."""
    detached = orig_ref in ("", "HEAD")
    args = (("checkout", "-q", "--detach", orig_sha) if detached
            else ("checkout", "-q", orig_ref))
    label = orig_sha[:12] if detached else orig_ref
    if _git(repo, *args).returncode != 0:
        console.print(f"[red]· could not restore {label}; left on {branch}.[/]")
        return
    _git(repo, "branch", "-D", branch)
    console.print(f"[dim]· restored {label}; removed {branch}.[/]")


def _commit_step(repo: Path, message: str) -> tuple[bool, str]:
    """Stage + commit one kept step. (ok, error text). Hooks run, so stdin is
    closed (a hook must not eat the operator's next answer) and each call is
    bounded; the mirror is never staged."""
    try:
        add = gitcmd.run_in(repo, "add", "-A", "--", ".",
                            f":(exclude){_MIRROR}", timeout=_COMMIT_TIMEOUT_S,
                            stdin=subprocess.DEVNULL)
        if add.returncode != 0:
            return False, (add.stderr or add.stdout).strip()
        cm = gitcmd.run_in(repo, "commit", "-q", "-m", message,
                           timeout=_COMMIT_TIMEOUT_S, stdin=subprocess.DEVNULL)
        if cm.returncode != 0:
            return False, ((cm.stderr or cm.stdout).strip()
                           or f"git commit exited {cm.returncode}")
    except subprocess.TimeoutExpired:
        return False, f"git timed out after {_COMMIT_TIMEOUT_S}s (a hook?)"
    except OSError as e:
        return False, str(e)
    return True, ""


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


def _looks_like_command(cmd: str) -> bool:
    """First-token test (after any `VAR=value` prefixes): a known runner or an
    explicit `./path`. Prose like "preserve the latest behavior" is advisory."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = cmd.split()
    while toks and "=" in toks[0] and not toks[0].startswith(("=", "./")):
        toks = toks[1:]
    if not toks:
        return False
    first = toks[0]
    return first.startswith("./") or Path(first).name in _VERIFY_RUNNERS


def _run_verify(cmd: str, repo: Path, timeout: int, *, console,
                reader) -> tuple[bool | None, str]:
    """Run the step's verify command — MODEL-WRITTEN shell, so only after the
    operator has seen it and confirmed (default N). Returns (None, '') for an
    advisory (non-command) verify or a declined run: never run unconfirmed."""
    cmd = (cmd or "").strip()
    if not cmd or not _looks_like_command(cmd):
        return None, ""
    console.print(f"[bold]· verify command (from the plan):[/] {escape(cmd)}")
    ans = reader("  run it? [y/N]: ").strip().lower()
    if ans not in ("y", "yes"):
        return None, ""
    try:
        r = subprocess.run(["bash", "-lc", cmd], cwd=str(repo),
                           capture_output=True, text=True,
                           # A verify command that emits one non-UTF-8 byte
                           # must still yield its tail, and it is run to be
                           # graded, not talked to: an inherited stdin lets it
                           # consume the operator's answer to the keep/discard
                           # prompt (or block on it until the timeout).
                           errors="replace", stdin=subprocess.DEVNULL,
                           timeout=timeout)
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
    # (3) clean tree. The mirror a previous run left behind is git-excluded
    # first, so it can neither read as dirt nor land in a step's commit.
    _exclude_mirror(repo)
    if not _require_clean(repo, console, "at start"):
        return 2
    # (4) dedicated, non-default branch — ALWAYS (never main/master/default).
    # The original checkout is recorded by SHA too: a detached HEAD has no
    # branch name to go back to.
    orig_branch = _current_branch(repo)
    orig_sha = pr.head_sha(repo)
    orig_label = (orig_sha[:12] if orig_branch in ("", "HEAD")
                  else orig_branch)
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
                  f"(original: {orig_label}) — main is never touched")
    if not _require_clean(repo, console, "after branch creation"):
        _abort_branch(repo, console, branch, orig_branch, orig_sha)
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
    _restore_tracked_mirror(repo)
    if not _require_clean(repo, console, "during plan generation"):
        _abort_branch(repo, console, branch, orig_branch, orig_sha)
        return 2
    if not plan or not plan.get("steps"):
        console.print("[yellow]· no plan steps to apply.[/]")
        _abort_branch(repo, console, branch, orig_branch, orig_sha)
        return 1
    # (6) order (abort on dependency cycle).
    try:
        steps = plan_mod.order_steps(plan)
    except ValueError as e:
        console.print(f"[red]· plan has a dependency cycle: {e}[/]")
        _abort_branch(repo, console, branch, orig_branch, orig_sha)
        return 2

    # --- write environment (the inverse of gitkit-today: FULL role, not read-only)
    from luxe.backend import Backend
    from luxe.gitkit.workspace import indexed_target
    if run_single_fn is None:
        from luxe.agents.single import run_single as run_single_fn

    survey = ""
    smap = store.reports_dir(repo) / "map" / "survey_notes.md"
    if smap.is_file():
        survey = smap.read_text()[:4000]

    backend = Backend(base_url=cfg.omlx_base_url, model=cfg.model_for_slot("chat"))
    role = cfg.role("monolith")    # FULL, write-enabled — NOT make_read_only_role
    languages = _detect_languages_for_repo(str(repo))
    timeout = int(getattr(cfg, "test_timeout_s", 300) or 300)

    kept: list[str] = []
    discarded: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    interrupted = False
    with indexed_target(str(repo), reuse=False, note=""):
        branch_head = pr.head_sha(repo)
        try:
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
                    # Kept steps are already committed (keep => commit before
                    # the next pass starts), so this full-tree revert only
                    # ever discards the FAILED step's partial writes.
                    _revert_to_head(repo)
                    failed.append(step["id"])
                    ans = reader("  [c]ontinue with next step / [a]bort? [c/A]: ").strip().lower()
                    if ans in ("c", "continue"):
                        continue
                    console.print("[yellow]· aborted — remaining steps not run.[/]")
                    break
                try:
                    _adds, _dels, diff = pr.diff_against_base(repo, branch_head)
                except pr.GitDiffError as e:
                    # "git broke" is not "the step wrote nothing" — a step whose
                    # result cannot be shown must not be silently skipped past.
                    console.print(f"[red]· could not read the diff for step "
                                  f"{step['id']}: {e}[/]")
                    _revert_to_head(repo)
                    failed.append(step["id"])
                    continue
                if not diff.strip():
                    console.print("[yellow]· no changes produced — skipping.[/]")
                    _revert_to_head(repo)
                    skipped.append(step["id"])
                    continue
                from rich.syntax import Syntax
                console.print(Syntax(diff[:8000], "diff", theme="ansi_dark",
                                     word_wrap=True))
                ok, tail = _run_verify(step.get("verify", ""), repo, timeout,
                                       console=console, reader=reader)
                if ok is True:
                    console.print("[green]· verify passed[/]")
                elif ok is False:
                    console.print(f"[red]· verify FAILED[/]\n[dim]{escape(tail[-600:])}[/]")
                else:
                    console.print(f"[dim]· verify (not run): {step.get('verify', '—')}[/]")
                default_keep = ok is not False
                prompt = "  keep or discard? [" + ("K/d" if default_keep else "k/D") + "]: "
                ans = reader(prompt).strip().lower()
                keep = ans in ("k", "keep", "y", "yes") or (ans == "" and default_keep)
                if not keep:
                    _revert_to_head(repo)
                    discarded.append(step["id"])
                    console.print(f"[yellow]· discarded {step['id']}[/]")
                    continue
                committed, err = _commit_step(
                    repo, f"gitchange {step['id']}: {step['title']}")
                if not committed:
                    # NOT kept: a step reported "kept" while uncommitted would
                    # be wiped by the next discard's revert. Stop here with
                    # the work left in the tree for the operator to commit.
                    failed.append(step["id"])
                    console.print(f"[red]· could not commit {step['id']} — it is "
                                  f"NOT kept. git said:[/]\n[dim]{escape(err[-600:])}[/]")
                    console.print("[yellow]· stopping; the step's changes are left "
                                  "uncommitted on the branch — fix the cause and "
                                  "commit them, or `git reset --hard` to drop "
                                  "them.[/]")
                    break
                branch_head = pr.head_sha(repo)
                kept.append(step["id"])
                console.print(f"[green]· kept {step['id']} (committed on {branch})[/]")
        except (KeyboardInterrupt, ChatCancelled):
            # Ctrl-C mid-step: the in-flight step's writes are reverted (kept
            # steps are commits and survive); the summary still prints.
            _revert_to_head(repo)
            interrupted = True
            console.print("\n[yellow]· interrupted — the in-flight step was "
                          "reverted; kept steps stay committed.[/]")
        except BaseException:
            _revert_to_head(repo)
            raise

    console.print(f"\n[bold]· done.[/] kept={len(kept)} discarded={len(discarded)} "
                  f"skipped={len(skipped)} failed={len(failed)} "
                  f"on branch [cyan]{branch}[/]")
    back = orig_sha if orig_branch in ("", "HEAD") else orig_branch
    console.print(f"[dim]  review:  git -C {repo} log {back}..{branch}[/]")
    console.print(f"[dim]  merge:   git -C {repo} checkout {back} && "
                  f"git merge {branch}   (you do this — apply never merges)[/]")
    console.print(f"[dim]  discard: git -C {repo} checkout {back} && "
                  f"git branch -D {branch}[/]")
    return 130 if interrupted else 0
