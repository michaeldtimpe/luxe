"""Tests for the gitkit read-only repo-analysis toolkit.

Covers the deterministic, non-model pieces: git-health gathering (incl. the
empty-repo edge case), graceful gh degradation, report persistence, and the
CLI alias resolution. The agent pass itself (run_single) is exercised
end-to-end manually against oMLX, not here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.gitkit import health, store


def _QuietConsole():
    """A Rich console that discards output (for exercising helpers quietly)."""
    import io

    from rich.console import Console
    return Console(file=io.StringIO(), force_terminal=False, width=100)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    (repo / "main.py").write_text("def f():\n    return 1\n")
    (repo / "requirements.txt").write_text("requests\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")
    (repo / "main.py").write_text("def f():\n    return 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tweak f")
    return repo


def test_gather_repo_health_reports_commits_and_size(git_repo: Path):
    block = health.gather_repo_health(git_repo)
    assert block.startswith("<repo_health>")
    assert block.rstrip().endswith("</repo_health>")
    assert "Total commits: 2" in block
    assert "Distinct authors: 1" in block
    assert "Files:" in block and "Tracked LOC:" in block
    assert "requirements.txt" in block  # manifest detected
    assert "tweak f" in block  # recent subject


def test_gather_repo_health_empty_repo_does_not_crash(tmp_path: Path):
    repo = tmp_path / "blank"
    repo.mkdir()
    _git(repo, "init", "-q")
    block = health.gather_repo_health(repo)
    assert "blank repository" in block.lower()
    assert block.startswith("<repo_health>")


def test_current_head_blank_repo_is_empty(tmp_path: Path):
    repo = tmp_path / "blank"
    repo.mkdir()
    _git(repo, "init", "-q")
    assert health.current_head(repo) == ""


def test_github_metadata_non_github_remote(git_repo: Path):
    block = health.gather_github_metadata(git_repo)
    assert "GitHub metadata unavailable" in block
    assert block.startswith("<github_metadata>")


def test_github_metadata_degrades_when_gh_unavailable(git_repo: Path, monkeypatch):
    # GitHub remote present, but gh fails (missing / unauthenticated / timeout).
    _git(git_repo, "remote", "add", "origin", "https://github.com/acme/widget.git")

    def _fake_gh(args, repo_path, timeout=30):
        return False, "gh CLI not installed"

    monkeypatch.setattr(health, "_run_gh", _fake_gh)
    block = health.gather_github_metadata(git_repo)
    assert "GitHub metadata unavailable (gh CLI not installed)" in block
    assert "local git only" in block


def test_github_metadata_uses_gh_when_available(git_repo: Path, monkeypatch):
    _git(git_repo, "remote", "add", "origin", "https://github.com/acme/widget.git")

    def _fake_gh(args, repo_path, timeout=30):
        if args[:2] == ["repo", "view"]:
            return True, '{"nameWithOwner":"acme/widget","stargazerCount":42}'
        if args[:2] == ["pr", "list"]:
            return True, "[]"
        return True, ""

    monkeypatch.setattr(health, "_run_gh", _fake_gh)
    block = health.gather_github_metadata(git_repo)
    assert "acme/widget" in block
    assert "42" in block


def test_save_report_writes_frontmatter_and_path(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = store.save_report(repo, "gitsummary", "# Report\n\nbody",
                             meta={"model": "Champ", "head": "abc123"})
    assert path.exists()
    assert path.name.startswith("gitsummary-")
    assert path.suffix == ".md"
    text = path.read_text()
    assert text.startswith("---\n")
    assert "kind: gitsummary" in text
    assert "model: Champ" in text
    assert "head: abc123" in text
    assert "# Report" in text
    # Lives under ~/.luxe/reports/<hash>/
    assert "reports" in str(path)


def test_save_report_two_calls_do_not_clash(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    p1 = store.save_report(repo, "gitreview", "a")
    p2 = store.save_report(repo, "gitreview", "b")
    assert p1 != p2  # uuid suffix avoids same-second collision


def test_aliased_group_resolves_aliases():
    import click

    from luxe.cli import AliasedGroup, apply_aliases

    group = AliasedGroup(name="root")

    @group.command(name="realcmd")
    def _realcmd():
        pass

    apply_aliases(group, {"al": "realcmd", "r": "realcmd"})
    assert group.get_command(None, "al").name == "realcmd"
    assert group.get_command(None, "r").name == "realcmd"
    assert group.get_command(None, "realcmd").name == "realcmd"
    assert group.get_command(None, "nope") is None


def test_is_git_repo_detects_working_tree(git_repo: Path, tmp_path: Path):
    assert health.is_git_repo(git_repo) is True
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    assert health.is_git_repo(plain) is False
    assert health.is_git_repo(tmp_path / "missing") is False


def test_resolve_or_clone_passthrough_for_git_repo(git_repo: Path):
    from luxe.gitkit import runner

    def _no_reader(_prompt):
        raise AssertionError("reader must not be called for a real git repo")

    out = runner._resolve_or_clone(
        git_repo, full_history=False, console=_QuietConsole(), reader=_no_reader)
    assert out == str(git_repo.resolve())


def test_resolve_or_clone_blank_url_cancels(tmp_path: Path):
    from luxe.gitkit import runner

    plain = tmp_path / "downloads"
    plain.mkdir()
    out = runner._resolve_or_clone(
        plain, full_history=False, console=_QuietConsole(), reader=lambda _p: "")
    assert out is None


def test_resolve_or_clone_clones_into_local_path(tmp_path: Path, monkeypatch):
    from luxe.gitkit import runner

    plain = tmp_path / "downloads"
    plain.mkdir()
    answers = iter(["https://github.com/acme/widget.git", "y"])

    def _reader(_prompt):
        return next(answers)

    cloned = {}

    def _fake_clone(url, dest, *, full_history, console):
        Path(dest).mkdir(parents=True)
        cloned["url"], cloned["dest"] = url, dest
        return True

    monkeypatch.setattr(runner, "_clone", _fake_clone)
    out = runner._resolve_or_clone(
        plain, full_history=True, console=_QuietConsole(), reader=_reader)
    # Clones into <dir>/<repo name>, and that path is returned.
    assert out == str((plain / "widget").resolve())
    assert cloned["url"] == "https://github.com/acme/widget.git"


def test_derive_dest_dedupes(tmp_path: Path):
    from luxe.gitkit import runner

    (tmp_path / "widget").mkdir()
    dest = runner._derive_dest(tmp_path, "https://github.com/acme/widget.git")
    assert dest == tmp_path / "widget-2"


@pytest.mark.parametrize("alias,canonical", [
    ("gsum", "gitsummary"), ("git-summary", "gitsummary"),
    ("grev", "gitreview"), ("git-review", "gitreview"),
    ("gref", "gitrefactor"), ("git-refactor", "gitrefactor"),
    ("gitsummary", "gitsummary"),
])
def test_main_registers_gitkit_commands_and_aliases(alias, canonical):
    from luxe.cli import main
    resolved = main.get_command(None, alias)
    assert resolved is not None
    assert resolved.name == canonical


# -- WS1 extraction net -----------------------------------------------------

def test_extract_report_strips_leading_monologue():
    from luxe.gitkit.runner import extract_report
    raw = ("1. I looked at foo\n2. what if bar\n"
           "# Bug & security review\n**Findings: 0**\nclean")
    out = extract_report(raw)
    assert out.startswith("# Bug & security review")
    assert "I looked at foo" not in out


def test_extract_report_keeps_headerless_text():
    from luxe.gitkit.runner import extract_report
    assert extract_report("just prose, no header") == "just prose, no header"


# -- WS2 activity callbacks (coalescing + phasing, no TTY needed) -----------

def test_activity_callbacks_coalesce_and_phase():
    from luxe.gitkit.runner import _activity_callbacks

    class _TC:
        def __init__(self, name):
            self.name = name

    updates: list[str] = []
    on_event, on_token = _activity_callbacks(updates.append)
    on_event(_TC("read_file"))
    on_event(_TC("read_file"))
    on_event(_TC("grep"))
    assert updates[-1] == "analyzing… read_file (2) · grep (1)"
    on_token("x")
    assert updates[-1] == "writing report…"
    on_event(_TC("read_file"))  # later tool flips back to analyzing
    assert updates[-1].startswith("analyzing…")


# -- WS1.3 budget + WS5 display (stubbed run_single / Backend) --------------

class _FakeResult:
    def __init__(self, text):
        self.final_text = text
        self.steps = 3
        self.tool_calls_total = 5
        self.wall_s = 1.2
        self.completion_tokens = 100


@pytest.fixture
def _gitkit_cfg():
    from luxe.config import PipelineConfig, RoleConfig
    return PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )


def _stub_run(monkeypatch, fake_run_single):
    """Patch Backend + run_single at their source modules (run_git_report imports
    them at call time, so it picks up the patched attrs)."""
    import luxe.agents.single as single_mod
    import luxe.backend as backend_mod

    class _FakeBackend:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(backend_mod, "Backend", _FakeBackend)
    monkeypatch.setattr(single_mod, "run_single", fake_run_single)


def test_run_git_report_applies_token_headroom_without_shared_mutation(
        git_repo, _gitkit_cfg, monkeypatch):
    from luxe.gitkit import run_git_report
    from luxe.gitkit.runner import GITKIT_MAX_TOKENS

    captured = {}

    def fake_run_single(backend, role_cfg, **kw):
        captured["role"] = role_cfg
        return _FakeResult("# Bug & security review\n**Findings: 0**\nclean")

    _stub_run(monkeypatch, fake_run_single)
    before = _gitkit_cfg.role("monolith").max_tokens_per_turn
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=git_repo,
                   console=_QuietConsole(), save=False)
    assert captured["role"].max_tokens_per_turn == GITKIT_MAX_TOKENS
    # the shared role object is untouched (per-run copy)
    assert _gitkit_cfg.role("monolith").max_tokens_per_turn == before


def test_run_git_report_strips_monologue_in_display_and_save(
        git_repo, _gitkit_cfg, monkeypatch):
    from luxe.gitkit import run_git_report

    raw = ("1. I looked at foo\n# Bug & security review\n**Findings: 0**\nclean")
    _stub_run(monkeypatch, lambda b, r, **k: _FakeResult(raw))
    report, saved = run_git_report(
        "gitreview", cfg=_gitkit_cfg, repo_path=git_repo,
        console=_QuietConsole(), save=True)
    assert report.startswith("# Bug & security review")
    assert "I looked at foo" not in report
    assert saved is not None and "I looked at foo" not in saved.read_text()


def test_run_git_report_preview_vs_verbose(git_repo, _gitkit_cfg, monkeypatch):
    import io

    from rich.console import Console

    from luxe.gitkit import run_git_report

    long_report = ("# Bug & security review\n**Findings: 1**\n"
                   + "\n".join(f"- detail line {i}" for i in range(80)))
    _stub_run(monkeypatch, lambda b, r, **k: _FakeResult(long_report))

    # default: truncated preview + saved path (wide console so the path doesn't
    # wrap and split substrings)
    out = io.StringIO()
    _, saved = run_git_report(
        "gitreview", cfg=_gitkit_cfg, repo_path=git_repo,
        console=Console(file=out, force_terminal=False, width=200),
        save=True, verbose=False)
    text = out.getvalue()
    assert "more lines" in text
    assert "saved to" in text
    assert saved is not None and "reports" in str(saved)

    # verbose: full report, no truncation hint
    out2 = io.StringIO()
    run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=git_repo,
                   console=Console(file=out2, force_terminal=False, width=200),
                   save=True, verbose=True)
    assert "more lines" not in out2.getvalue()


def test_run_git_report_cancellation(git_repo, _gitkit_cfg, monkeypatch):
    from luxe.chat.render import CancelToken
    from luxe.gitkit import run_git_report

    cancel = CancelToken()
    cancel.requested = True

    def fake_run_single(backend, role_cfg, *, on_token=None, **kw):
        if on_token:
            on_token("x")   # triggers raise_if_cancelled in the gitkit callback
        return _FakeResult("# Bug & security review\n**Findings: 0**")

    _stub_run(monkeypatch, fake_run_single)
    report, saved = run_git_report("gitreview", cfg=_gitkit_cfg, repo_path=git_repo,
                                   console=_QuietConsole(), save=True, cancel=cancel)
    assert report == "" and saved is None   # cancelled cleanly, nothing saved


def test_run_git_report_short_report_no_hint(git_repo, _gitkit_cfg, monkeypatch):
    import io

    from rich.console import Console

    from luxe.gitkit import run_git_report

    short = "# Repository summary & risk assessment\n**Use-risk: low** — fine"
    _stub_run(monkeypatch, lambda b, r, **k: _FakeResult(short))
    out = io.StringIO()
    run_git_report("gitsummary", cfg=_gitkit_cfg, repo_path=git_repo,
                   console=Console(file=out, force_terminal=False, width=200),
                   save=True, verbose=False)
    text = out.getvalue()
    assert "more lines" not in text          # whole report fit
    assert "saved to" in text
