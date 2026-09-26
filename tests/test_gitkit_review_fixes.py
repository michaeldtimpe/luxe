"""Regressions for the 2026-09 gitkit review: cache staleness, the apply
executor's revert/keep/verify paths, report parsing, and ephemeral leaks.

Every repro builds a throwaway git repo under tmp_path with HOME pointed at a
temp dir — nothing here may touch the real ~/.luxe.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LUXE_HOME", raising=False)
    return home


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True).stdout.strip()


def _init_repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "T")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


# --- 3: severity headers as real reports write them --------------------------

_REAL_AUDIT = """# Repository audit
**Findings: 4**

## Repository summary & risk
Use-risk: low.

## Bugs & security

### Critical
- **critical** `a.py:1` — takeover

### High
- `b.py:2` — token bypass

### M — `StopIteration` on bare `next()`
Evidence:
```python
# audio_meta.py:29
x = next(it)
```

### Low
- `d.py:4` — tidy
- `e.py:5` — nit

## Structural improvements
- split the god module
"""


def test_extract_findings_reads_nested_and_shorthand_severity_headers():
    from luxe.gitkit import store
    out = store.extract_findings(_REAL_AUDIT)
    assert "takeover" in out and "token bypass" in out
    assert "StopIteration" in out and "audio_meta.py:29" in out
    assert "tidy" in out
    assert "Repository summary" not in out
    assert "split the god module" not in out


def test_min_severity_hides_nested_sections():
    from luxe.gitkit import store
    out, dropped = store.filter_min_severity(_REAL_AUDIT, "high")
    assert "takeover" in out and "token bypass" in out
    assert "StopIteration" not in out           # ### M — … (one finding)
    assert "tidy" not in out and "nit" not in out
    assert "split the god module" in out        # later ## section survives
    assert dropped == 3


# --- apply executor (2, 4, 5, 6, 7) ------------------------------------------

def _tty_console():
    return Console(file=io.StringIO(), force_terminal=True, width=160,
                   color_system=None, highlight=False)


@pytest.fixture
def _cfg():
    from luxe.config import PipelineConfig, RoleConfig
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")})


@pytest.fixture
def apply_env(monkeypatch):
    from luxe.gitkit import apply

    class _FB:
        def __init__(self, *a, **k):
            self.model = "Champ"
    monkeypatch.setattr("luxe.backend.Backend", _FB)
    monkeypatch.setattr(apply, "_is_tty", lambda c: True)
    return apply


def _save_plan(repo: Path, steps: list[dict]) -> None:
    from luxe.gitkit import health, plan
    p = plan.normalize_plan({"summary": "t", "steps": steps},
                            head=health.current_head(repo))
    plan.save_plan_json(repo, p)


def _step(sid: str, verify: str = "preserve behavior", **kw) -> dict:
    return {"id": sid, "title": f"step {sid}", "target_files": ["main.py"],
            "change": {"op": "rename", "detail": "d"}, "risk": "low",
            "verify": verify, "depends_on": [], **kw}


class _R:
    final_text = "done"


def _stub(repo: Path, writes: dict[str, dict[str, str]],
          raise_on: dict | None = None):
    """run_single stub: step id -> {path: content} written into the repo."""
    calls: list[str] = []

    def fake(backend, role, *, run_id="", **kw):
        sid = run_id.replace("gitchange-apply-", "")
        calls.append(sid)
        for rel, text in writes.get(sid, {}).items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
        if raise_on and sid in raise_on:
            raise raise_on[sid]
        return _R()
    return fake, calls


def _repo(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "repo", {"main.py": "def f():\n    return 1\n"})


def test_discarded_new_file_is_not_committed_by_next_kept_step(
        tmp_path, _cfg, apply_env):
    """2: `add -N .` staged the discarded step's new file as intent-to-add;
    `checkout -- .` left it as an EMPTY file the next keep committed."""
    repo = _repo(tmp_path)
    _save_plan(repo, [_step("S1"), _step("S2")])
    fake, _ = _stub(repo, {"S1": {"newmod.py": "x = 1\n"},
                           "S2": {"main.py": "def f():\n    return 2\n"}})
    answers = iter(["discard", "keep"])
    rc = apply_env.run_apply(repo_path=str(repo), cfg=_cfg,
                             console=_tty_console(),
                             reader=lambda _p: next(answers), run_single_fn=fake)
    assert rc == 0
    committed = _out(repo, "show", "--name-only", "--pretty=", "HEAD").split()
    assert committed == ["main.py"]
    assert not (repo / "newmod.py").exists()
    assert _out(repo, "status", "--porcelain") == ""


def test_failed_commit_is_not_reported_kept(tmp_path, _cfg, apply_env):
    """4: a failing hook used to leave the step "kept" but uncommitted."""
    repo = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'lint says no' >&2\nexit 1\n")
    hook.chmod(0o755)
    _save_plan(repo, [_step("S1"), _step("S2")])
    fake, calls = _stub(repo, {"S1": {"main.py": "def f():\n    return 2\n"},
                               "S2": {"other.py": "y = 2\n"}})
    con = _tty_console()
    apply_env.run_apply(repo_path=str(repo), cfg=_cfg, console=con,
                        reader=lambda _p: "keep", run_single_fn=fake)
    out = con.file.getvalue()
    assert "kept=0" in out and "failed=1" in out
    assert "lint says no" in out
    assert calls == ["S1"]                        # stopped; S2 never ran
    assert _out(repo, "log", "-1", "--pretty=%s") == "init"
    # the operator's work is still there to commit, not silently wiped
    assert "return 2" in (repo / "main.py").read_text()


def test_mirror_does_not_block_or_pollute_apply(tmp_path, _cfg, apply_env):
    """5: an untracked .luxe/gitkit/ mirror made --apply refuse "dirty", and
    `add -A` then committed it with the step."""
    repo = _repo(tmp_path)
    mirror = repo / ".luxe" / "gitkit"
    mirror.mkdir(parents=True)
    (mirror / "README.md").write_text("mirror\n")
    _save_plan(repo, [_step("S1")])
    fake, calls = _stub(repo, {"S1": {"main.py": "def f():\n    return 2\n"}})
    rc = apply_env.run_apply(repo_path=str(repo), cfg=_cfg,
                             console=_tty_console(), reader=lambda _p: "keep",
                             run_single_fn=fake)
    assert rc == 0 and calls == ["S1"]
    committed = _out(repo, "show", "--name-only", "--pretty=", "HEAD").split()
    assert committed == ["main.py"]
    assert (mirror / "README.md").exists()


def test_other_luxe_files_still_count_as_dirty(tmp_path, apply_env):
    """5: the exemption is exactly .luxe/gitkit/, not everything under .luxe."""
    repo = _repo(tmp_path)
    (repo / ".luxe").mkdir()
    (repo / ".luxe" / "memory.md").write_text("notes\n")
    assert apply_env._require_clean(repo, _tty_console(), "now") is False


def test_failed_git_status_counts_as_dirty(tmp_path, apply_env):
    notrepo = tmp_path / "plain"
    notrepo.mkdir()
    assert apply_env._require_clean(notrepo, _tty_console(), "now") is False


def test_abort_from_detached_head_restores_it(tmp_path, _cfg, apply_env,
                                              monkeypatch):
    """6: `checkout HEAD` on abort was a no-op — the repo stayed on the
    gitchange branch and the branch delete failed."""
    repo = _repo(tmp_path)
    sha = _out(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach", sha)
    monkeypatch.setattr("luxe.gitkit.run_git_report", lambda kind, **kw: None)
    fake, calls = _stub(repo, {})
    rc = apply_env.run_apply(repo_path=str(repo), cfg=_cfg,
                             console=_tty_console(), reader=lambda _p: "keep",
                             run_single_fn=fake)
    assert rc == 1 and not calls                 # no plan → abort path
    assert _out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _out(repo, "rev-parse", "HEAD") == sha
    assert _out(repo, "branch", "--list", "gitchange/*") == ""


def test_ctrl_c_mid_step_reverts_the_step(tmp_path, _cfg, apply_env):
    """6: KeyboardInterrupt escaped the step loop with the partial writes
    still in the tree."""
    repo = _repo(tmp_path)
    _save_plan(repo, [_step("S1"), _step("S2")])
    fake, calls = _stub(repo, {"S1": {"main.py": "def f():\n    return 2\n"},
                               "S2": {"half.py": "partial\n"}},
                        raise_on={"S2": KeyboardInterrupt()})
    rc = apply_env.run_apply(repo_path=str(repo), cfg=_cfg,
                             console=_tty_console(), reader=lambda _p: "keep",
                             run_single_fn=fake)
    assert rc == 130 and calls == ["S1", "S2"]
    assert "gitchange S1" in _out(repo, "log", "-1", "--pretty=%s")
    assert not (repo / "half.py").exists()
    assert _out(repo, "status", "--porcelain") == ""


def test_verify_command_needs_confirmation(tmp_path, _cfg, apply_env):
    """7: the model-written verify string ran via bash -lc unconfirmed."""
    repo = _repo(tmp_path)
    marker = tmp_path / "ran"
    _save_plan(repo, [_step("S1", verify=f"python3 -c \"open('{marker}','w')\"")])
    fake, _ = _stub(repo, {"S1": {"main.py": "def f():\n    return 2\n"}})
    prompts_seen: list[str] = []

    def reader(p):
        prompts_seen.append(p)
        return "" if "run it?" in p else "keep"   # default answer = N
    con = _tty_console()
    apply_env.run_apply(repo_path=str(repo), cfg=_cfg, console=con,
                        reader=reader, run_single_fn=fake)
    assert any("run it?" in p for p in prompts_seen)
    assert "python3 -c" in con.file.getvalue()    # the command was SHOWN
    assert not marker.exists()                    # …and not run by default


def test_verify_runs_after_yes_and_prose_is_advisory(tmp_path, apply_env):
    marker = tmp_path / "ran"
    con = _tty_console()
    ok, _ = apply_env._run_verify(f"python3 -c \"open('{marker}','w')\"",
                                  tmp_path, 30, console=con,
                                  reader=lambda _p: "y")
    assert ok is True and marker.exists()
    asked: list[str] = []
    ok, _ = apply_env._run_verify("preserve the latest behavior", tmp_path, 30,
                                  console=con,
                                  reader=lambda p: asked.append(p) or "y")
    assert ok is None and not asked               # first token, not substring


# --- structural: one repo-root/index swap -------------------------------------

def test_indexed_target_restores_an_unset_root(tmp_path, monkeypatch):
    from luxe import search, symbols
    from luxe.gitkit.workspace import indexed_target
    from luxe.tools import fs
    repo = _init_repo(tmp_path / "r", {"a.py": "def a():\n    return 1\n"})
    monkeypatch.setattr(fs, "_REPO_ROOT", None)
    search.reset_index()
    symbols.reset_index()
    with indexed_target(str(repo)) as swapped:
        assert swapped and fs.get_repo_root() == repo.resolve()
        assert search.get_index() is not None
    assert fs.get_repo_root() is None
    assert search.get_index() is None and symbols.get_index() is None
    # a resident root that already IS the target is reused, not rebuilt
    fs.set_repo_root(repo)
    with indexed_target(str(repo.resolve())) as swapped:
        assert swapped is False
