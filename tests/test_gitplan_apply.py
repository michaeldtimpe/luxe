"""Tests for the gated gitchange executor (apply.py) — the safety invariants.

Stubs run_single (the agent's edit) and drives the per-step keep/discard gate. The
focus is the SIX invariants: interactive-only, clean-tree-only, dedicated non-default
branch, per-step confirm, depends_on gating, and NEVER push/merge/commit-to-default.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from luxe.gitkit import apply, health, plan


def _TTYConsole():
    return Console(file=io.StringIO(), force_terminal=True, width=120)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
def _stub_backend(monkeypatch):
    class _FB:
        def __init__(self, *a, **k):
            self.model = "Champ"
    monkeypatch.setattr("luxe.backend.Backend", _FB)


@pytest.fixture
def _cfg():
    from luxe.config import PipelineConfig, RoleConfig
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")})


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo_on_main(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    (repo / "main.py").write_text("def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _save_plan(repo: Path, steps: list[dict]) -> str:
    head = health.current_head(repo)
    p = plan.normalize_plan({"summary": "test", "steps": steps}, head=head)
    plan.save_plan_json(repo, p)
    return head


_STEP = {"id": "S1", "title": "tweak f", "target_files": ["main.py"],
         "change": {"op": "rename", "symbols": ["f"], "detail": "rename"},
         "rationale": "r", "risk": "low", "verify": "preserve behavior",
         "depends_on": []}


def _writer_stub(repo: Path, content="def g():\n    return 2\n"):
    """A run_single stub that edits main.py (so a diff appears), recording calls."""
    calls: list[str] = []

    def fake(backend, role, *, run_id="", **kw):
        calls.append(run_id)
        (repo / "main.py").write_text(content)

        class _R:
            final_text = "done"
        return _R()
    return fake, calls


# --- the invariants ---------------------------------------------------------

def test_apply_refused_without_tty(repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: False)
    _save_plan(repo_on_main, [dict(_STEP)])
    fake, calls = _writer_stub(repo_on_main)
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=lambda _p: "keep",
                         run_single_fn=fake)
    assert rc == 2
    assert not calls                                   # no agent run, no writes
    assert "gitchange/" not in _out(repo_on_main, "branch", "--list", "gitchange/*")


def test_apply_aborts_on_dirty_tree(repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    (repo_on_main / "dirty.txt").write_text("x")        # untracked => dirty
    _save_plan(repo_on_main, [dict(_STEP)])
    fake, calls = _writer_stub(repo_on_main)
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=lambda _p: "keep",
                         run_single_fn=fake)
    assert rc == 2 and not calls


def test_apply_keeps_on_dedicated_branch_never_main(repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    _save_plan(repo_on_main, [dict(_STEP)])
    main_commits_before = _out(repo_on_main, "rev-list", "--count", "main")
    fake, calls = _writer_stub(repo_on_main)
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=lambda _p: "keep",
                         run_single_fn=fake)
    assert rc == 0 and calls == ["gitchange-apply-S1"]
    cur = _out(repo_on_main, "rev-parse", "--abbrev-ref", "HEAD")
    assert cur.startswith("gitchange/")                  # on a dedicated branch
    # the kept commit is on the gitchange branch, NOT on main
    assert _out(repo_on_main, "rev-list", "--count", "main") == main_commits_before
    assert "gitchange S1: tweak f" in _out(repo_on_main, "log", "-1", "--pretty=%s")
    assert "return 2" in (repo_on_main / "main.py").read_text()


def test_apply_discard_reverts_no_commit(repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    _save_plan(repo_on_main, [dict(_STEP)])
    fake, calls = _writer_stub(repo_on_main)
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=lambda _p: "discard",
                         run_single_fn=fake)
    assert rc == 0
    # working tree reverted, no commit on the branch
    assert "return 1" in (repo_on_main / "main.py").read_text()
    assert _out(repo_on_main, "log", "-1", "--pretty=%s") == "init"
    assert _out(repo_on_main, "status", "--porcelain") == ""   # clean


def test_apply_depends_on_skips_dependent_on_discard(repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    s2 = {"id": "S2", "title": "second", "target_files": ["main.py"],
          "change": {"op": "split", "detail": "d"}, "risk": "low",
          "verify": "preserve", "depends_on": ["S1"]}
    _save_plan(repo_on_main, [dict(_STEP), s2])
    fake, calls = _writer_stub(repo_on_main)
    apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg, console=_TTYConsole(),
                    reader=lambda _p: "discard", run_single_fn=fake)
    # S1 ran and was discarded; S2 was skipped (its dep wasn't kept) → never invoked
    assert calls == ["gitchange-apply-S1"]


def test_apply_never_pushes_or_merges(repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    _save_plan(repo_on_main, [dict(_STEP)])
    git_subcmds: list[list[str]] = []
    real = subprocess.run

    def spy(args, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "git":
            git_subcmds.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(apply.subprocess, "run", spy)
    fake, _ = _writer_stub(repo_on_main)
    apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg, console=_TTYConsole(),
                    reader=lambda _p: "keep", run_single_fn=fake)
    flat = [tok for cmd in git_subcmds for tok in cmd]
    assert "push" not in flat
    assert "merge" not in flat


# --- A1: dirty-tree TOCTOU re-checks ---------------------------------------

def test_require_clean_exempts_gitkit_mirror(repo_on_main):
    (repo_on_main / ".luxe" / "gitkit").mkdir(parents=True)
    (repo_on_main / ".luxe" / "gitkit" / "survey_notes.md").write_text("m")
    assert apply._require_clean(repo_on_main, _TTYConsole(), "now") is True
    (repo_on_main / "stray.txt").write_text("x")
    assert apply._require_clean(repo_on_main, _TTYConsole(), "now") is False


def test_apply_aborts_when_tree_dirtied_during_plan_generation(
        repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)

    def dirtying_report(kind, **kw):
        (repo_on_main / "user-edited.txt").write_text("mid-generation edit")

    monkeypatch.setattr("luxe.gitkit.run_git_report", dirtying_report)
    fake, calls = _writer_stub(repo_on_main)
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=lambda _p: "keep",
                         run_single_fn=fake)
    assert rc == 2 and not calls                       # no step ever ran
    # original branch restored AND the gitchange branch no longer exists
    assert _out(repo_on_main, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _out(repo_on_main, "branch", "--list", "gitchange/*") == ""


def test_apply_no_steps_does_not_orphan_branch(repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    monkeypatch.setattr("luxe.gitkit.run_git_report", lambda kind, **kw: None)
    fake, calls = _writer_stub(repo_on_main)
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=lambda _p: "keep",
                         run_single_fn=fake)
    assert rc == 1 and not calls
    assert _out(repo_on_main, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _out(repo_on_main, "branch", "--list", "gitchange/*") == ""


# --- A2: step-loop exception gate (continue/abort, NEVER retry) -------------

def _flaky_stub(repo: Path, raise_on: set[str]):
    """run_single stub: distinct edit per step; raises (after a partial write)
    for step ids in `raise_on`."""
    calls: list[str] = []

    def fake(backend, role, *, run_id="", **kw):
        calls.append(run_id)
        sid = run_id.replace("gitchange-apply-", "")
        (repo / "main.py").write_text(f"def g():\n    return '{sid}'\n")
        if sid in raise_on:
            (repo / "partial.txt").write_text("junk from the failed pass")
            raise RuntimeError("backend exploded mid-step")

        class _R:
            final_text = "done"
        return _R()
    return fake, calls


def _reader(on_fail: str):
    """keep every gated step; answer `on_fail` at the continue/abort prompt."""
    def r(prompt: str) -> str:
        if "[c]ontinue" in prompt:
            return on_fail
        return "keep"
    return r


def _steps(*ids: str) -> list[dict]:
    return [{**_STEP, "id": i, "title": f"step {i}"} for i in ids]


def test_apply_step_exception_continue_runs_next_step(
        repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    _save_plan(repo_on_main, _steps("S1", "S2", "S3"))
    fake, calls = _flaky_stub(repo_on_main, raise_on={"S2"})
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=_reader("c"),
                         run_single_fn=fake)
    assert rc == 0
    assert calls == ["gitchange-apply-S1", "gitchange-apply-S2",
                     "gitchange-apply-S3"]            # S3 ran after the failure
    assert "return 'S3'" in (repo_on_main / "main.py").read_text()
    assert not (repo_on_main / "partial.txt").exists()  # failed writes reverted
    assert _out(repo_on_main, "status", "--porcelain") == ""


def test_apply_step_exception_default_is_abort(repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    _save_plan(repo_on_main, _steps("S1", "S2"))
    fake, calls = _flaky_stub(repo_on_main, raise_on={"S1"})
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=_reader(""),
                         run_single_fn=fake)
    assert rc == 0
    assert calls == ["gitchange-apply-S1"]             # S2 never ran
    assert not (repo_on_main / "partial.txt").exists()
    assert _out(repo_on_main, "status", "--porcelain") == ""


def test_apply_kept_commit_survives_later_step_failure(
        repo_on_main, _cfg, monkeypatch):
    """Revert-scoping: keep commits BEFORE the next pass, so the full-tree
    revert after a step-2 failure only discards step 2's partial writes."""
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    _save_plan(repo_on_main, _steps("S1", "S2"))
    fake, calls = _flaky_stub(repo_on_main, raise_on={"S2"})
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=_reader(""),
                         run_single_fn=fake)
    assert rc == 0 and calls == ["gitchange-apply-S1", "gitchange-apply-S2"]
    # step 1's kept edit survives step 2's full-tree revert…
    assert "return 'S1'" in (repo_on_main / "main.py").read_text()
    # …because it was already committed on the branch
    assert "gitchange S1" in _out(repo_on_main, "log", "-1", "--pretty=%s")
    assert not (repo_on_main / "partial.txt").exists()
    assert _out(repo_on_main, "status", "--porcelain") == ""


def test_apply_aborts_on_cycle(repo_on_main, _cfg, monkeypatch):
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    a = {"id": "A", "title": "a", "change": {"detail": "x"}, "depends_on": ["B"]}
    b = {"id": "B", "title": "b", "change": {"detail": "y"}, "depends_on": ["A"]}
    _save_plan(repo_on_main, [a, b])
    fake, calls = _writer_stub(repo_on_main)
    rc = apply.run_apply(repo_path=str(repo_on_main), cfg=_cfg,
                         console=_TTYConsole(), reader=lambda _p: "keep",
                         run_single_fn=fake)
    assert rc == 2 and not calls
