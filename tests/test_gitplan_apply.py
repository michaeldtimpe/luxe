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
