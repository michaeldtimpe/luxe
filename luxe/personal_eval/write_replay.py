"""B2 — write replay.

For each PR:
1. Create a fresh worktree at the base commit (pre-PR state).
2. Feed the PR's task description to the agent loop.
3. Let the agent read, grep, write, and run tests until it stops or hits
   step budget.
4. Score: did the test command pass? How close is its diff to the human diff?
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from harness import io
from harness.backends import Backend
from harness.metrics import RunMetrics
from harness.server import is_server_alive
from personal_eval.agent_loop import AgentConfig, run_agent
from personal_eval.gh_ingest import PRRecord, load_corpus
from personal_eval.rubric import score_diff_similarity

_console = Console()

SYSTEM_PROMPT = """\
You are a senior engineer making a code change in a git repository.

CRITICAL: complete the task by calling write_file with the updated file
contents. Do NOT produce plans, summaries, or explanations in prose —
make the actual edit. Prose responses without tool calls waste the turn.

Recommended loop (3-6 turns, not 10+):
1. Call read_file on the 1-3 files the task references.
2. Call write_file with the full updated file contents for each change.
3. If the repo has tests (pytest / cargo test / go test), run them
   via shell once. Fix and re-run only if they fail.
4. Stop by emitting zero tool calls. Do not write a summary first.

Each turn must be either (a) one or more tool calls, or (b) empty (to
stop). Never produce multi-paragraph prose inside a turn.
"""


@dataclass
class WriteResult:
    pr_number: int
    language: str
    repo: str
    tests_passed: bool
    test_exit_code: int | None
    diff_similarity: float
    files_touched: list[str]
    steps_taken: int
    tool_calls_total: int
    wall_s: float
    metrics: dict[str, Any]
    first_turn_raw_text: str = ""
    first_turn_finish_reason: str = ""
    # py_compile result per touched Python file. Empty dict if no Python
    # files were touched. Useful as a "did the model at least not break
    # syntax" signal when the repo has no test suite.
    syntax_ok: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_pr(
    backend: Backend,
    pr: PRRecord,
    *,
    candidate_id: str,
    config_id: str,
    max_steps: int = 30,
) -> WriteResult:
    repo = Path(pr.repo_path)

    with _worktree(repo, pr.base_sha) as worktree:
        files_hint = ", ".join(pr.changed_files[:20]) or "(none recorded)"
        task = (
            f"Task (from commit): {pr.title}\n\n"
            f"{pr.body or '(no detail beyond the title)'}\n\n"
            f"Hint — the original commit touched these files: {files_hint}\n"
            f"Start by reading them, then call write_file with the updated "
            f"version of each file that needs to change. Make the edit; do "
            f"not write a plan."
        )

        metrics = RunMetrics(
            candidate_id=candidate_id,
            config_id=config_id,
            benchmark="personal_write",
            task_id=f"{repo.name}#{pr.number}",
        )

        agent = run_agent(
            backend,
            repo_root=worktree,
            task_description=task,
            system_prompt=SYSTEM_PROMPT,
            metrics=metrics,
            config=AgentConfig(max_steps=max_steps),
        )
        metrics.finish()

        gold_diff = _diff(repo, pr.base_sha, pr.head_sha)
        similarity = score_diff_similarity(agent.final_diff, gold_diff)
        syntax_ok = _syntax_check(worktree, agent.files_touched, pr.language)

    return WriteResult(
        pr_number=pr.number,
        language=pr.language,
        repo=repo.name,
        tests_passed=(agent.test_exit_code == 0) if agent.test_exit_code is not None else False,
        test_exit_code=agent.test_exit_code,
        diff_similarity=round(similarity, 3),
        files_touched=agent.files_touched,
        steps_taken=agent.steps_taken,
        tool_calls_total=agent.tool_calls_total,
        wall_s=metrics.wall_s,
        metrics=metrics.to_dict(),
        first_turn_raw_text=agent.first_turn_raw_text,
        first_turn_finish_reason=agent.first_turn_finish_reason,
        syntax_ok=syntax_ok,
    )


def replay_corpus(
    backend: Backend,
    repo_name: str,
    *,
    candidate_id: str,
    config_id: str,
    limit: int | None = None,
    max_steps: int = 30,
) -> list[WriteResult]:
    corpus = load_corpus(repo_name)
    if limit:
        corpus = corpus[:limit]
    out_path = io.runs_path("phase_b", candidate_id, config_id, f"write_{repo_name}")
    done = io.completed_task_ids(out_path)

    results: list[WriteResult] = []
    remaining = [pr for pr in corpus if f"{repo_name}#{pr.number}" not in done]
    for i, pr in enumerate(remaining, 1):
        if not is_server_alive(backend.base_url):
            _console.log(
                f"  [fatal] server at {backend.base_url} is not responding — "
                f"stopping sweep. Re-run after the server restarts."
            )
            break
        _console.log(
            f"[B2 write] {repo_name} · {i}/{len(remaining)} · "
            f"PR/commit #{pr.number}: {pr.title[:80]}"
        )
        tid = f"{repo_name}#{pr.number}"
        try:
            res = write_pr(
                backend,
                pr,
                candidate_id=candidate_id,
                config_id=config_id,
                max_steps=max_steps,
            )
        except Exception as e:  # noqa: BLE001
            _console.log(f"  [error] {type(e).__name__}: {e}")
            continue
        syntax_summary = (
            f"syntax_ok={sum(res.syntax_ok.values())}/{len(res.syntax_ok)}"
            if res.syntax_ok
            else "syntax_ok=(n/a)"
        )
        _console.log(
            f"  → steps={res.steps_taken} tools={res.tool_calls_total} "
            f"{syntax_summary} diff_sim={res.diff_similarity}"
        )
        io.append(out_path, {"task_id": tid, **res.to_dict()})
        results.append(res)
    return results


def _diff(repo: Path, base_sha: str, head_sha: str) -> str:
    res = subprocess.run(  # noqa: S603
        ["git", "diff", f"{base_sha}..{head_sha}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return res.stdout


def _syntax_check(repo: Path, files: list[str], language: str) -> dict[str, bool]:
    """Cheap 'did the agent break syntax' probe for Python files.

    Not a test — just confirms the file parses. Non-Python files are
    ignored (result dict omits them). Agent-touched files that no longer
    exist (agent deleted them) are reported as True.
    """
    if language != "python":
        return {}
    import sys as _sys

    out: dict[str, bool] = {}
    for rel in files:
        path = (repo / rel).resolve()
        try:
            path.relative_to(repo.resolve())
        except ValueError:
            continue
        if not path.exists():
            out[rel] = True
            continue
        if path.suffix != ".py":
            continue
        res = subprocess.run(  # noqa: S603
            [_sys.executable, "-m", "py_compile", str(path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        out[rel] = res.returncode == 0
    return out


class _worktree:
    """Transient git worktree at a specific commit; cleaned up on exit."""

    def __init__(self, repo: Path, commit: str) -> None:
        self.repo = repo
        self.commit = commit
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._path: Path | None = None

    def __enter__(self) -> Path:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="luxe-wt-")
        path = Path(self._tmpdir.name) / "repo"
        subprocess.run(  # noqa: S603
            ["git", "worktree", "add", "--detach", str(path), self.commit],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        self._path = path
        return path

    def __exit__(self, *exc: Any) -> None:
        if self._path:
            subprocess.run(  # noqa: S603
                ["git", "worktree", "remove", "--force", str(self._path)],
                cwd=self.repo,
                capture_output=True,
            )
        if self._tmpdir:
            self._tmpdir.cleanup()
