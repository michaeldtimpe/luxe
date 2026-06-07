"""Tests for gitchange — the apply-ready structured change-plan kind (read-only).

Covers the deterministic plan.py (parse/normalize/order/render/save) and the
gitchange single-pass + deep orchestration with a STUBBED run_single.
"""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from luxe.gitkit import deep, plan


def _QuietConsole():
    return Console(file=io.StringIO(), force_terminal=False, width=120)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


_PLAN_JSON = (
    'Here is the plan:\n```json\n'
    '{"schema":"gitplan/v1","summary":"split the god module","steps":['
    '{"id":"S1","title":"Extract client factory","target_files":["src/api/client.py"],'
    '"change":{"op":"extract","symbols":["build_client"],"detail":"move construction"},'
    '"rationale":"dup across call sites","risk":"low","verify":"pytest -q","depends_on":[]},'
    '{"id":"S2","title":"Inline helper","target_files":["src/api/util.py"],'
    '"change":{"op":"inline","symbols":["_h"],"detail":"inline single-use helper"},'
    '"rationale":"needless indirection","risk":"med","verify":"pytest -q","depends_on":["S1"]}'
    ']}\n```\nthat is all.'
)


# --- plan.py unit tests -----------------------------------------------------

def test_parse_plan_lenient_extraction():
    raw = plan.parse_plan(_PLAN_JSON)
    assert raw is not None and len(raw["steps"]) == 2


def test_normalize_fills_defaults_and_prunes_dangling_deps():
    raw = {"steps": [
        {"title": "no id no risk", "target_files": ["a.py"],
         "change": {"detail": "x"}, "depends_on": ["S99"]},  # dangling dep dropped
        {"id": "X", "change": {"op": "weird"}},               # no content -> dropped
    ]}
    p = plan.normalize_plan(raw, head="abc", default_verify="make test")
    assert len(p["steps"]) == 1
    s = p["steps"][0]
    assert s["id"] == "S1" and s["risk"] == "med"            # defaults
    assert s["change"]["op"] == "change"                     # unknown op normalized
    assert s["verify"] == "make test"                        # default verify
    assert s["depends_on"] == []                             # dangling pruned
    assert p["schema"] == "gitplan/v1" and p["head"] == "abc"


def test_order_steps_topological_and_cycle():
    p = plan.normalize_plan(plan.parse_plan(_PLAN_JSON), head="h")
    ordered = [s["id"] for s in plan.order_steps(p)]
    assert ordered.index("S1") < ordered.index("S2")        # dep before dependent
    # cycle raises
    cyc = {"steps": [
        {"id": "A", "title": "a", "change": {"detail": "x"}, "depends_on": ["B"]},
        {"id": "B", "title": "b", "change": {"detail": "y"}, "depends_on": ["A"]}]}
    with pytest.raises(ValueError):
        plan.order_steps(plan.normalize_plan(cyc, head="h"))


def test_render_markdown_shape():
    p = plan.normalize_plan(plan.parse_plan(_PLAN_JSON), head="h")
    md = plan.render_markdown(p)
    assert md.startswith("# Change plan")
    assert "**Steps: 2**" in md
    assert "## S1: Extract client factory" in md
    assert "**Verify:** pytest -q" in md
    assert "**Depends on:** S1" in md                        # S2's dep rendered


def test_save_and_latest_plan_roundtrip(tmp_path):
    repo = tmp_path / "repo"
    p = plan.normalize_plan(plan.parse_plan(_PLAN_JSON), head="dead")
    saved = plan.save_plan_json(repo, p)
    assert saved.is_file()
    got = plan.latest_plan_for(repo, "dead")
    assert got is not None and len(got["steps"]) == 2
    assert plan.latest_plan_for(repo, "other") is None      # head mismatch


def test_finalize_falls_back_to_digest_steps(tmp_path):
    repo = tmp_path / "repo"
    # unparseable text + aggregated steps -> plan assembled from the steps
    steps = [{"id": "S1", "title": "t", "target_files": ["a.py"],
              "change": {"op": "split", "detail": "d"}, "risk": "high",
              "verify": "pytest", "depends_on": []}]
    md, p = plan.finalize_and_save(repo, "h", "no json here at all",
                                   fallback_steps=steps)
    assert "## S1: t" in md and len(p["steps"]) == 1
    assert plan.latest_plan_for(repo, "h") is not None       # persisted


# --- orchestration (stubbed run_single) -------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def small_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    (repo / "main.py").write_text("def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def _gitkit_cfg():
    from luxe.config import PipelineConfig, RoleConfig
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")})


class _FakeResult:
    def __init__(self, text):
        self.final_text = text
        self.steps = 1
        self.tool_calls_total = 0
        self.wall_s = 0.1
        self.completion_tokens = 10


def _stub_run(monkeypatch, fn):
    import luxe.agents.single as single_mod
    import luxe.backend as backend_mod

    class _FakeBackend:
        def __init__(self, *a, **k):
            self.model = "Champ"

    monkeypatch.setattr(backend_mod, "Backend", _FakeBackend)
    monkeypatch.setattr(single_mod, "run_single", fn)


def test_gitchange_single_pass_saves_plan_and_renders(small_repo, _gitkit_cfg, monkeypatch):
    from luxe.gitkit import run_git_report, store
    from luxe.memory.project import repo_hash

    _stub_run(monkeypatch, lambda *a, **k: _FakeResult(_PLAN_JSON))
    report, saved = run_git_report("gitchange", cfg=_gitkit_cfg, repo_path=small_repo,
                                   console=_QuietConsole(), save=True, deep=False)
    assert report.startswith("# Change plan")
    assert "## S1: Extract client factory" in report
    # plan.json persisted (canonical) + mirrored into the repo
    rdir = Path.home() / ".luxe" / "reports" / repo_hash(small_repo)
    assert list(rdir.glob("plan-*.json"))
    assert list((small_repo / ".luxe" / "gitkit" / "plans").glob("plan-*.json"))


def test_gitchange_small_repo_auto_stays_single_pass(small_repo, _gitkit_cfg,
                                                   monkeypatch):
    """A small repo (footprint under the deep trigger) takes the single-pass path —
    one analysis pass, no survey/chunk/synthesis (a holistic plan is best in one
    window when it fits)."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    calls: list[str] = []

    def fake(backend, role, *, run_id="", **kw):
        calls.append(run_id)
        return _FakeResult(_PLAN_JSON)

    _stub_run(monkeypatch, fake)
    run_git_report("gitchange", cfg=_gitkit_cfg, repo_path=small_repo,
                   console=_QuietConsole(), save=True, deep=None)  # auto by footprint
    assert calls == ["gitkit-gitchange"]
    assert not any("deep" in c for c in calls)


def test_gitchange_recovers_via_extraction_pass(small_repo, _gitkit_cfg, monkeypatch):
    """When the analysis emits prose (no JSON), a transcription pass recovers it."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report
    from luxe.memory.project import repo_hash

    calls: list[str] = []

    def fake(backend, role, *, run_id="", **kw):
        calls.append(run_id)
        if "extract" in run_id:                              # recovery pass → JSON
            return _FakeResult(_PLAN_JSON)
        return _FakeResult("Here is a prose plan: 1. extract the client. No JSON.")

    _stub_run(monkeypatch, fake)
    report, _ = run_git_report("gitchange", cfg=_gitkit_cfg, repo_path=small_repo,
                               console=_QuietConsole(), save=True, deep=False)
    assert "gitkit-gitchange-extract" in calls                 # recovery fired
    assert "## S1: Extract client factory" in report         # steps recovered
    rdir = Path.home() / ".luxe" / "reports" / repo_hash(small_repo)
    pj = json.loads(next(rdir.glob("plan-*.json")).read_text())
    assert len(pj["steps"]) == 2


# --- deep gitchange (large-repo map-reduce) -----------------------------------

@pytest.fixture
def multi_repo(tmp_path: Path) -> Path:
    """A multi-file repo so the chunker (with a tiny content budget) produces ≥2
    chunks — the large-repo path that single-pass gitchange emptied on."""
    repo = tmp_path / "mrepo"
    (repo / "api").mkdir(parents=True)
    (repo / "core").mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    for d, n in (("api", 3), ("core", 3)):
        for i in range(n):
            (repo / d / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n" * 5)
    (repo / "main.py").write_text("def main():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# one apply-ready step per chunk (distinct titles → all survive synthesis dedup)
def _chunk_steps_json(tag: str) -> str:
    return ('```json\n{"steps":[{"id":"c1","title":"extract ' + tag + '",'
            '"target_files":["' + tag + '/m0.py"],'
            '"change":{"op":"extract","symbols":["f0"],"detail":"pull out helper"},'
            '"rationale":"dup","risk":"low","verify":"pytest -q","depends_on":[]}]}\n```')


def _gitchange_deep_stub(calls, *, chunk_text=None, synth_text=None,
                       extract_text=None):
    """run_single stub for a deep gitchange run, keyed on the pass run_id."""
    def fake(backend, role_cfg, *, run_id="", extra_context="", **kw):
        calls.append(run_id)
        if "survey" in run_id:
            return _FakeResult("Survey: a small python application.")
        if "plan-extract" in run_id:
            return _FakeResult(extract_text if extract_text is not None else _PLAN_JSON)
        if "synthesis" in run_id:
            return _FakeResult(synth_text if synth_text is not None else _PLAN_JSON)
        # chunk pass → its own area's step (the chunk_files listing names the dir)
        if chunk_text is not None:
            return _FakeResult(chunk_text)
        tag = "core" if "- core/" in extra_context else "api"
        return _FakeResult(_chunk_steps_json(tag))
    return fake


def test_gitchange_enters_deep_when_forced(multi_repo, _gitkit_cfg, monkeypatch):
    """Forced deep (or large footprint) routes gitchange through the staged map-reduce:
    survey → chunks → synthesis, and a structured plan is saved + mirrored."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)  # force ≥2 chunks
    calls: list[str] = []
    _stub_run(monkeypatch, lambda *a, **k: None)              # install fake Backend
    monkeypatch.setattr(single_mod, "run_single", _gitchange_deep_stub(calls))

    report, saved = run_git_report("gitchange", cfg=_gitkit_cfg, repo_path=multi_repo,
                                   console=_QuietConsole(), save=True, deep=True)
    assert len([c for c in calls if "survey" in c]) == 1
    assert len([c for c in calls if "synthesis" in c]) == 1
    assert len([c for c in calls if "chunk" in c]) >= 2
    assert "gitkit-gitchange" not in calls                      # NOT the single-pass id
    assert report.startswith("# Change plan")

    # canonical plan.json + repo mirror both written
    rdir = store.reports_dir(multi_repo)
    assert list(rdir.glob("plan-*.json"))
    assert list((multi_repo / ".luxe" / "gitkit" / "plans").glob("plan-*.json"))


def test_gitchange_deep_recovers_prose_chunk(multi_repo, _gitkit_cfg, monkeypatch):
    """A chunk that rambles instead of emitting JSON gets its steps recovered by a
    chunk-level plan-extract pass (not lost to the report-shaped path)."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    calls: list[str] = []
    _stub_run(monkeypatch, lambda *a, **k: None)
    # every chunk emits prose → recovery must fire to produce any steps
    monkeypatch.setattr(single_mod, "run_single", _gitchange_deep_stub(
        calls, chunk_text="Prose only: extract the helper. No JSON block."))

    run_git_report("gitchange", cfg=_gitkit_cfg, repo_path=multi_repo,
                   console=_QuietConsole(), save=True, deep=True)
    assert any("plan-extract" in c for c in calls)            # chunk recovery fired
    pj = json.loads(next(store.reports_dir(multi_repo).glob("plan-*.json")).read_text())
    assert len(pj["steps"]) >= 1                              # steps survived


def test_gitchange_deep_synthesis_falls_back_to_digest_steps(multi_repo, _gitkit_cfg,
                                                           monkeypatch):
    """When BOTH the synthesis and its transcription recovery ramble, the plan is
    assembled deterministically from the aggregated per-chunk digest steps."""
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)
    calls: list[str] = []
    _stub_run(monkeypatch, lambda *a, **k: None)
    # chunks emit clean JSON steps; synthesis + its extract both ramble (no JSON)
    monkeypatch.setattr(single_mod, "run_single", _gitchange_deep_stub(
        calls, synth_text="No JSON, just musing about the plan.",
        extract_text="Still no JSON here."))

    run_git_report("gitchange", cfg=_gitkit_cfg, repo_path=multi_repo,
                   console=_QuietConsole(), save=True, deep=True)
    pj = json.loads(next(store.reports_dir(multi_repo).glob("plan-*.json")).read_text())
    assert len(pj["steps"]) >= 2                              # api + core chunk steps
