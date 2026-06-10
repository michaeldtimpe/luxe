"""Tests for the diff-scoped audit (`gitaudit --base/--pr`) — diffscope.py and
its runner routing.

Deterministic pieces (merge-base, changed files, hunks, cap/truncation, tag
priors, gh degradation) run against a real temp 2-commit repo; the routing
tests drive `run_git_report` with a STUBBED run_single (the deep-test pattern).
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from luxe.gitkit import diffscope


def _QuietConsole():
    return Console(file=io.StringIO(), force_terminal=False, width=120)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True, text=True)
    return r.stdout.strip()


@pytest.fixture
def diff_repo(tmp_path: Path) -> tuple[Path, str]:
    """Two-commit repo: base commit (a.py, b.py, gone.py) → HEAD modifies a.py,
    adds c.py, renames b.py → renamed.py, deletes gone.py."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n")
    (repo / "b.py").write_text("VALUE = 41\n" * 12)
    (repo / "gone.py").write_text("dead = True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "a.py").write_text(
        "def f():\n    return 99  # changed\n\n\ndef g():\n    return 2\n")
    (repo / "c.py").write_text("def new():\n    return 'fresh'\n")
    (repo / "b.py").rename(repo / "renamed.py")
    (repo / "gone.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return repo, base_sha


# --- diffscope determinism on a real repo ------------------------------------

def test_changed_files_follows_renames_and_drops_deletions(diff_repo):
    repo, base = diff_repo
    mb = diffscope.merge_base(repo, base)
    assert mb == base                       # linear history → merge-base = base
    files = diffscope.changed_files(repo, mb)
    assert set(files) == {"a.py", "c.py", "renamed.py"}
    assert "gone.py" not in files           # deletion dropped
    assert "b.py" not in files              # rename followed to the NEW path
    # deterministic across calls
    assert files == diffscope.changed_files(repo, mb)


def test_diff_stats_and_file_recs(diff_repo):
    repo, base = diff_repo
    mb = diffscope.merge_base(repo, base)
    n, adds, dels = diffscope.diff_stats(repo, mb)
    assert n >= 3 and adds >= 3 and dels >= 2
    recs = diffscope.file_recs(repo, diffscope.changed_files(repo, mb))
    assert {r.rel for r in recs} == {"a.py", "c.py", "renamed.py"}
    assert all(r.tokens >= 1 and r.language for r in recs)


def test_changed_hunks_new_side_line_ranges(diff_repo):
    repo, base = diff_repo
    mb = diffscope.merge_base(repo, base)
    hunks = diffscope.changed_hunks(repo, mb)
    # a.py line 2 changed; c.py fully added
    assert diffscope.in_changed_hunk(hunks, "a.py", 2)
    assert not diffscope.in_changed_hunk(hunks, "a.py", 5)   # untouched line
    assert diffscope.in_changed_hunk(hunks, "c.py", 1)
    assert "gone.py" not in hunks                            # no new side


def test_resolve_base_ref_falls_back_to_origin(diff_repo, tmp_path):
    repo, base = diff_repo
    assert diffscope.resolve_base_ref(repo, base) == base
    assert diffscope.resolve_base_ref(repo, "main") == "main"
    assert diffscope.resolve_base_ref(repo, "no-such-ref") is None


# --- <change_diff> block: cap + truncation notice -----------------------------

def test_change_diff_block_carries_header_and_diff(diff_repo):
    repo, base = diff_repo
    mb = diffscope.merge_base(repo, base)
    block = diffscope.change_diff_block(repo, mb, base_label="main",
                                        max_tokens=100000)
    assert block.startswith("<change_diff>") and block.endswith("</change_diff>")
    assert f"merge-base {mb[:8]}" in block and "Base: main" in block
    assert "return 99" in block                    # the actual change
    assert "truncated" not in block


def test_change_diff_block_truncates_with_notice(diff_repo):
    repo, base = diff_repo
    mb = diffscope.merge_base(repo, base)
    block = diffscope.change_diff_block(repo, mb, base_label="main", max_tokens=30)
    assert "truncated at the token cap" in block
    assert block.endswith("</change_diff>")


def test_change_diff_block_scopes_to_files(diff_repo):
    repo, base = diff_repo
    mb = diffscope.merge_base(repo, base)
    block = diffscope.change_diff_block(repo, mb, base_label="main",
                                        max_tokens=100000, files=["c.py"])
    assert "fresh" in block and "return 99" not in block
    assert "scoped to 1 file(s)" in block


# --- hunk-overlap prior + deterministic header/caveat -------------------------

_HUNKS = {"a.py": [(1, 4)]}


def test_apply_tag_priors_default_tags_by_overlap():
    report = ("# Diff audit\n## Bugs & security\n"
              "- **high** `a.py:2` — bug inside the change\n"
              "- **low** `a.py:40` — bug far from the change\n"
              "## Change-scoped structural notes\n- note `a.py:2` untouched\n")
    out = diffscope.apply_tag_priors(report, _HUNKS)
    lines = out.splitlines()
    assert "likely-introduced" in lines[2]                 # inside hunk
    assert "pre-existing (touched code)" in lines[3]       # outside hunk
    assert "introduced" not in lines[5]                    # notes section untouched


def test_apply_tag_priors_refutes_unsupported_likely_introduced():
    report = ("# Diff audit\n## Bugs & security\n"
              "- **high** `a.py:40` — **likely-introduced** claim outside hunk\n"
              "- **med** `a.py:2` — **likely-introduced** supported\n")
    out = diffscope.apply_tag_priors(report, _HUNKS)
    lines = out.splitlines()
    assert "pre-existing (touched code)" in lines[2]       # refined, not trusted
    assert "likely-introduced" in lines[3]                 # supported tag kept


def test_apply_tag_priors_keeps_model_preexisting_refinement():
    report = ("# Diff audit\n## Bugs & security\n"
              "- **high** `a.py:2` — **pre-existing (touched code)** despite hunk\n")
    out = diffscope.apply_tag_priors(report, _HUNKS)
    # the model may REFINE likely→pre-existing; the prior never overrides that
    assert "pre-existing (touched code)" in out
    assert "likely-introduced" not in out


def test_ensure_header_inserts_base_line_and_caveat():
    out = diffscope.ensure_header("# Diff audit\n\n## Bugs & security\n",
                                  "main", "abcd1234ef", (3, 10, 2))
    head = out.splitlines()[:4]
    assert any("**Base: main (merge-base abcd1234)" in ln for ln in head)
    assert any("hunk overlap" in ln for ln in head)
    # idempotent — never duplicated
    again = diffscope.ensure_header(out, "main", "abcd1234ef", (3, 10, 2))
    assert again.count("**Base:") == 1
    assert again.lower().count("hunk overlap") == 1


# --- gh degradation says WHY ---------------------------------------------------

@pytest.mark.parametrize("gh_out,expect", [
    ("gh CLI not installed", "not installed"),
    ("gh timed out after 30s", "network"),
    ("GraphQL: Could not resolve to a PullRequest with the number of 999.",
     "not found"),
    ("HTTP 401: To get started with GitHub CLI, please run: gh auth login",
     "not authenticated"),
])
def test_pr_base_ref_degradation_names_failure_class(tmp_path, monkeypatch,
                                                     gh_out, expect):
    monkeypatch.setattr(diffscope, "_run_gh", lambda *a, **k: (False, gh_out))
    ref, why = diffscope.pr_base_ref(tmp_path, 999)
    assert ref is None
    assert expect in why
    assert "--base" in why          # always offers the local-ref escape hatch


def test_pr_base_ref_success(tmp_path, monkeypatch):
    monkeypatch.setattr(diffscope, "_run_gh",
                        lambda *a, **k: (True, '{"baseRefName": "develop"}'))
    ref, why = diffscope.pr_base_ref(tmp_path, 7)
    assert ref == "develop" and why == ""


# --- runner routing with a stubbed run_single ---------------------------------

class _FakeResult:
    def __init__(self, text: str):
        self.final_text = text
        self.steps = 1
        self.tool_calls_total = 0
        self.wall_s = 0.1
        self.completion_tokens = 10


@pytest.fixture
def _cfg():
    from luxe.config import PipelineConfig, RoleConfig
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")})


def _stub_backend(monkeypatch):
    import luxe.backend as backend_mod

    class _FB:
        def __init__(self, *a, **k):
            self.model = "Champ"
    monkeypatch.setattr(backend_mod, "Backend", _FB)


_DIFF_REPORT = ("# Diff audit\n**Base: x (merge-base y) — 3 files, +3/−2**\n"
                "*Classification is heuristic — hunk overlap, not proof.*\n\n"
                "## Bugs & security\n- **high** `a.py:2` — issue\n")


def test_small_diff_routes_single_pass_with_diff_blocks(diff_repo, _cfg,
                                                        monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report, store

    repo, base = diff_repo
    _stub_backend(monkeypatch)
    contexts: list[str] = []

    def fake_run_single(backend, role_cfg, *, extra_context="", run_id="", **kw):
        contexts.append(extra_context)
        return _FakeResult(_DIFF_REPORT)

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    report, saved = run_git_report("gitaudit", cfg=_cfg, repo_path=repo,
                                   console=_QuietConsole(), save=True, base=base)
    assert len(contexts) == 1                      # exactly ONE pass
    assert "<change_diff>" in contexts[0]
    assert "<chunk_files>" in contexts[0]          # changed-file list shape
    assert "a.py" in contexts[0] and "c.py" in contexts[0]
    assert report.startswith("# Diff audit")
    # frontmatter: kind + base + merge_base
    assert saved is not None and saved.name.startswith("gitaudit-diff-")
    front = saved.read_text().split("\n---\n")[0]
    assert "kind: gitaudit-diff" in front
    assert "base:" in front and "merge_base:" in front


def test_large_diff_routes_deep_chunks_over_changed_files_only(
        diff_repo, _cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import deep, run_git_report, store

    repo, base = diff_repo
    _stub_backend(monkeypatch)
    monkeypatch.setattr(deep, "_CONTENT_BUDGET_FRAC", 0.0005)  # force chunking
    seen: list[tuple[str, str]] = []

    def fake_run_single(backend, role_cfg, *, extra_context="", run_id="", **kw):
        seen.append((run_id, extra_context))
        if "synthesis" in run_id:
            return _FakeResult(_DIFF_REPORT)
        return _FakeResult('```json\n{"findings": []}\n```')

    monkeypatch.setattr(single_mod, "run_single", fake_run_single)
    report, saved = run_git_report("gitaudit", cfg=_cfg, repo_path=repo,
                                   console=_QuietConsole(), save=True,
                                   base=base, deep=True)
    run_ids = [r for r, _ in seen]
    assert not any("survey" in r for r in run_ids)            # NO survey pass
    chunk_calls = [(r, c) for r, c in seen if "chunk" in r]
    assert chunk_calls                                        # chunked
    for _, ctx in chunk_calls:
        assert "<change_diff>" in ctx                         # per-chunk diff
        assert "gone.py" not in ctx                           # changed files only
    # diff runs never write the map cache
    assert not (store.reports_dir(repo) / "map").exists()
    assert report.startswith("# Diff audit")


def test_no_changes_vs_base_aborts_cleanly(diff_repo, _cfg, monkeypatch):
    import luxe.agents.single as single_mod
    from luxe.gitkit import run_git_report

    repo, _ = diff_repo
    head = _git(repo, "rev-parse", "HEAD")
    _stub_backend(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(single_mod, "run_single",
                        lambda *a, **k: calls.append("x") or _FakeResult("x"))
    report, saved = run_git_report("gitaudit", cfg=_cfg, repo_path=repo,
                                   console=_QuietConsole(), save=True, base=head)
    assert report == "" and saved is None and not calls


def test_base_and_pr_mutually_exclusive(diff_repo, _cfg, monkeypatch):
    from luxe.gitkit import run_git_report
    repo, base = diff_repo
    _stub_backend(monkeypatch)
    report, saved = run_git_report("gitaudit", cfg=_cfg, repo_path=repo,
                                   console=_QuietConsole(), base=base, pr=3)
    assert report == "" and saved is None
