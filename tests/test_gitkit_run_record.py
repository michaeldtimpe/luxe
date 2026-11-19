"""gitkit per-invocation run records (gitkit.sdd Must: run record).

Every gitkit invocation mints ONE unique run id, writes
`~/.luxe/runs/<id>/run.json`, and every model pass logs under that id.
`--no-save` and `--ephemeral` write nothing under `~/.luxe/runs/`.
No real model: Backend + run_single are stubbed; the fake run_single logs one
event per call through `run_state.append_event`, the same sink the agent loop
uses, so "events land in the run dir" is exercised end to end.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from luxe import ephemeral, run_state
from luxe.gitkit import runrecord
from tests._git import git as _git
from tests.test_gitkit import _FakeResult, _QuietConsole

_ID_RE = re.compile(r"^gitkit-gitaudit-\d{8}T\d{6}Z-[0-9a-f]{6}$")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    ephemeral.disable()
    yield
    ephemeral.disable()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    (repo / "main.py").write_text("def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")
    return repo


@pytest.fixture
def cfg():
    from luxe.config import PipelineConfig, RoleConfig
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )


_REPORT = "# Repository audit\n**Findings: 0**\nclean"


def _stub_run(monkeypatch, fake_run_single):
    """test_gitkit's stub, plus a `model` attr (deep's saved meta reads it)."""
    import luxe.agents.single as single_mod
    import luxe.backend as backend_mod

    class _FakeBackend:
        def __init__(self, *a, model="Champ", **k):
            self.model = model

    monkeypatch.setattr(backend_mod, "Backend", _FakeBackend)
    monkeypatch.setattr(single_mod, "run_single", fake_run_single)


def _recording_run_single(seen: list):
    def fake(backend, role_cfg, **kw):
        seen.append(kw.get("run_id"))
        run_state.append_event(kw.get("run_id"), "fake_turn")
        return _FakeResult(_REPORT)
    return fake


def _runs() -> list[Path]:
    root = run_state.runs_root()
    return sorted(root.iterdir()) if root.exists() else []


def _gitaudit(cfg, repo, *, save=True, deep=False):
    from luxe.gitkit import run_git_report
    return run_git_report("gitaudit", cfg=cfg, repo_path=repo,
                          console=_QuietConsole(), save=save, deep=deep,
                          mirror=False)


def test_mint_run_id_format_and_uniqueness():
    ids = {runrecord.mint_run_id("gitaudit") for _ in range(50)}
    assert len(ids) == 50
    assert all(_ID_RE.match(i) for i in ids)


def test_two_invocations_get_distinct_run_dirs(git_repo, cfg, monkeypatch):
    seen: list = []
    _stub_run(monkeypatch, _recording_run_single(seen))
    _gitaudit(cfg, git_repo)
    _gitaudit(cfg, git_repo)
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(_ID_RE.match(s) for s in seen)
    assert [p.name for p in _runs()] == sorted(seen)


def test_run_json_fields_and_events_land_in_run_dir(git_repo, cfg, monkeypatch):
    from luxe.gitkit import health
    seen: list = []
    _stub_run(monkeypatch, _recording_run_single(seen))
    _gitaudit(cfg, git_repo)
    (rid,) = seen
    rd = run_state.run_dir(rid)
    meta = json.loads((rd / "run.json").read_text())
    assert meta["run_id"] == rid
    assert meta["tool"] == "gitkit"
    assert meta["kind"] == "gitaudit"
    assert meta["mode"] == "single"
    assert meta["model"] == cfg.model_for_slot("chat")
    assert Path(meta["repo_path"]).resolve() == git_repo.resolve()
    assert meta["head_sha"] == health.current_head(git_repo) != ""
    assert meta["started_at"] > 0 and meta["started_at_utc"].endswith("+00:00")
    # still loads as a RunSpec (extra keys ignored)
    assert run_state.load_run_spec(rid).repo_path == meta["repo_path"]
    events = [json.loads(line) for line in
              (rd / "events.jsonl").read_text().splitlines()]
    assert [e["kind"] for e in events] == ["fake_turn"]
    assert all(e["run_id"] == rid for e in events)


def test_deep_passes_log_under_one_invocation_dir(git_repo, cfg, monkeypatch):
    seen: list = []
    _stub_run(monkeypatch, _recording_run_single(seen))
    _gitaudit(cfg, git_repo, deep=True)
    assert len(seen) >= 2, "deep should run survey/chunk/synthesis passes"
    # every pass is `<invocation id>/<label>`, one invocation id
    roots = {s.split("/", 1)[0] for s in seen}
    assert len(roots) == 1 and all(s.count("/") == 1 for s in seen)
    (rid,) = roots
    assert _ID_RE.match(rid)
    assert len(set(seen)) == len(seen)               # distinct pass labels
    assert any(s.endswith("/survey") for s in seen)
    assert any(s.endswith("/synthesis") for s in seen)
    assert [p.name for p in _runs()] == [rid]         # ONE top-level run dir
    rd = run_state.run_dir(rid)
    assert json.loads((rd / "run.json").read_text())["mode"] == "deep"
    for s in seen:
        (ev,) = [json.loads(line) for line in
                 (run_state.run_dir(s) / "events.jsonl").read_text().splitlines()]
        assert ev["run_id"] == s and ev["kind"] == "fake_turn"
    # list_runs sees the invocation once (pass subdirs carry no run.json)
    assert [r.run_id for r in run_state.list_runs()] == [rid]


def test_pass_id_is_one_safe_path_component():
    assert runrecord.pass_id(None, "chunk-1") is None
    assert runrecord.pass_id("", "chunk-1") is None
    assert runrecord.pass_id("gitkit-x", "chunk-1") == "gitkit-x/chunk-1"
    # apply step ids come from model-written plan JSON: no traversal
    pid = runrecord.pass_id("gitkit-x", "step-../../etc/x")
    assert pid == "gitkit-x/step-______etc_x"
    assert run_state.run_dir(pid).resolve().is_relative_to(
        run_state.run_dir("gitkit-x").resolve())


def test_no_save_writes_nothing_under_runs(git_repo, cfg, monkeypatch):
    seen: list = []
    _stub_run(monkeypatch, _recording_run_single(seen))
    _gitaudit(cfg, git_repo, save=False)
    _gitaudit(cfg, git_repo, save=False, deep=True)
    assert seen and all(s is None for s in seen)
    assert _runs() == []


def test_ephemeral_writes_nothing_under_runs(git_repo, cfg, monkeypatch):
    seen: list = []
    _stub_run(monkeypatch, _recording_run_single(seen))
    ephemeral.enable()
    _gitaudit(cfg, git_repo)
    assert len(seen) == 1
    assert not run_state.runs_root().exists() or _runs() == []
