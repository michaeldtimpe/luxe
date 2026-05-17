"""Tests for src/luxe/pr.py — preflight, branch naming, test detection."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from luxe import pr as pr_mod
from luxe.pr import (
    CmdResult,
    DirtyTreeError,
    GhAuthError,
    NoMutationsError,
    PRConfig,
    PRError,
    assert_clean_tree,
    detect_test_command,
    is_dirty,
    plan_branch_name,
    slugify_goal,
)
from luxe.run_state import RunSpec


def _cfg() -> PRConfig:
    return PRConfig(
        test_commands=[
            {"command": "pytest -q", "markers": ["pyproject.toml", "pytest.ini"]},
            {"command": "npm test", "markers": ["package.json"]},
            {"command": "cargo test", "markers": ["Cargo.toml"]},
        ],
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# repo\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


# --- slug / branch ----------------------------------------------------------

def test_slugify_goal_basic():
    # Splits on non-alphanumerics; hyphens become token separators.
    # "Fix the off-by-one in pagination" → 8 tokens, capped at max_words=6.
    assert slugify_goal("Fix the off-by-one in pagination") == "fix-the-off-by-one-in"


def test_slugify_goal_truncates_long():
    s = slugify_goal("review the whole authentication subsystem for issues")
    assert s.count("-") <= 5  # 6 words, 5 hyphens


def test_slugify_goal_handles_no_words():
    assert slugify_goal("!!!") == "goal"


def test_plan_branch_name_no_collision(git_repo: Path, monkeypatch):
    monkeypatch.setattr(pr_mod, "_branch_exists_local", lambda r, n: False)
    monkeypatch.setattr(pr_mod, "_branch_exists_remote", lambda r, n: False)
    name = plan_branch_name("bugfix", "fix the bug", git_repo, _cfg())
    assert name == "luxe/bugfix/fix-the-bug"


def test_plan_branch_name_with_collision(git_repo: Path, monkeypatch):
    taken = {"luxe/bugfix/fix-the-bug", "luxe/bugfix/fix-the-bug-2"}
    monkeypatch.setattr(pr_mod, "_branch_exists_local", lambda r, n: n in taken)
    monkeypatch.setattr(pr_mod, "_branch_exists_remote", lambda r, n: False)
    name = plan_branch_name("bugfix", "fix the bug", git_repo, _cfg())
    assert name == "luxe/bugfix/fix-the-bug-3"


# --- test detection ---------------------------------------------------------

def test_detect_test_command_python(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("")
    assert detect_test_command(tmp_path, _cfg()) == "pytest -q"


def test_detect_test_command_node(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}")
    assert detect_test_command(tmp_path, _cfg()) == "npm test"


def test_detect_test_command_first_match_wins(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "package.json").write_text("{}")
    # Python is first in the list
    assert detect_test_command(tmp_path, _cfg()) == "pytest -q"


def test_detect_test_command_none_matched(tmp_path: Path):
    assert detect_test_command(tmp_path, _cfg()) == ""


# --- dirty-tree -------------------------------------------------------------

def test_is_dirty_clean_tree(git_repo: Path):
    assert not is_dirty(git_repo)


def test_is_dirty_with_untracked(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    assert is_dirty(git_repo)


def test_assert_clean_tree_passes_when_clean(git_repo: Path):
    assert_clean_tree(git_repo, allow_dirty=False)


def test_assert_clean_tree_aborts_when_dirty(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    with pytest.raises(DirtyTreeError):
        assert_clean_tree(git_repo, allow_dirty=False)


def test_allow_dirty_requires_confirmation(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    with pytest.raises(DirtyTreeError):
        # No confirm_callback → not confirmed
        assert_clean_tree(git_repo, allow_dirty=True, confirm_callback=None)


def test_allow_dirty_with_confirm_yes(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    assert_clean_tree(git_repo, allow_dirty=True, confirm_callback=lambda: True)


def test_allow_dirty_with_confirm_no(git_repo: Path):
    (git_repo / "new.txt").write_text("untracked")
    with pytest.raises(DirtyTreeError):
        assert_clean_tree(git_repo, allow_dirty=True, confirm_callback=lambda: False)


# --- gh auth ----------------------------------------------------------------
#
# assert_gh_auth() probes GitHub via `gh api user --jq .login` (NOT
# `gh auth status` — see project_gh_auth_flake.md for the rationale). The
# function has a TTL cache and retries with a configured delay tuple; tests
# below patch `time.sleep` to zero out retries and reset the cache between
# tests via the `_reset_gh_auth_cache` test seam.


@pytest.fixture(autouse=True)
def _reset_gh_auth_state(monkeypatch):
    """Reset the TTL cache before each test and patch sleep to no-op so the
    retry loop runs synchronously."""
    pr_mod._reset_gh_auth_cache()
    monkeypatch.setattr(pr_mod.time, "sleep", lambda *_: None)


def test_assert_gh_auth_missing(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(pr_mod, "_run", boom)
    with pytest.raises(GhAuthError) as excinfo:
        pr_mod.assert_gh_auth()
    assert "not found" in str(excinfo.value)


def test_assert_gh_auth_unauthed(monkeypatch):
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=1, stdout="", stderr="not authenticated"))
    with pytest.raises(GhAuthError):
        pr_mod.assert_gh_auth()


def test_assert_gh_auth_ok(monkeypatch):
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=0, stdout="Logged in", stderr=""))
    pr_mod.assert_gh_auth()


# --- gh-auth v1.10.3+ hardening ---------------------------------------------

def test_assert_gh_auth_uses_api_probe(monkeypatch):
    """Probe MUST be `gh api user --jq .login`, NOT `gh auth status`.
    `gh auth status` only validates local CLI state (flaps on keychain
    issues); the API probe exercises the same network+auth path PR
    creation actually uses."""
    captured: list[list[str]] = []

    def _capture(cmd, cwd, env=None, timeout=None):
        captured.append(list(cmd))
        return CmdResult(rc=0, stdout="user", stderr="")

    monkeypatch.setattr(pr_mod, "_run", _capture)
    pr_mod.assert_gh_auth()
    assert captured == [["gh", "api", "user", "--jq", ".login"]]


def test_assert_gh_auth_per_attempt_timeout_passed_to_run(monkeypatch):
    """Each subprocess call must pass a per-attempt timeout. `gh api user`
    is a real HTTP call that can hang on a degraded network; without a
    per-attempt timeout, a single stuck subprocess can eat the entire
    retry budget."""
    timeouts: list[float | None] = []

    def _capture(cmd, cwd, env=None, timeout=None):
        timeouts.append(timeout)
        return CmdResult(rc=0, stdout="user", stderr="")

    monkeypatch.setattr(pr_mod, "_run", _capture)
    pr_mod.assert_gh_auth()
    assert timeouts == [pr_mod._GH_AUTH_PROBE_TIMEOUT_S]
    assert pr_mod._GH_AUTH_PROBE_TIMEOUT_S > 0


def test_assert_gh_auth_widened_retry_window(monkeypatch):
    """5 attempts (vs the old 3) at the configured delays — gives a ~22s
    worst-case window that covers most transient network drops without
    distorting suite latency past the operational threshold."""
    attempts: list[int] = []

    def _capture(cmd, cwd, env=None, timeout=None):
        attempts.append(len(attempts) + 1)
        return CmdResult(rc=1, stdout="", stderr="could not resolve github.com")

    monkeypatch.setattr(pr_mod, "_run", _capture)
    with pytest.raises(GhAuthError):
        pr_mod.assert_gh_auth()
    assert len(attempts) == len(pr_mod._GH_AUTH_RETRY_DELAYS_S) == 5


def test_assert_gh_auth_classifies_network_failure(monkeypatch, caplog):
    """A stderr that screams "network" must classify as `network`, not
    `auth` or `unknown`. The classifier drives future "should we
    auto-retry?" / "did GitHub degrade?" analytics."""
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=1, stdout="",
                                  stderr="dial tcp: lookup api.github.com: "
                                         "no such host"))
    with caplog.at_level("INFO", logger="luxe.pr.gh_auth"):
        with pytest.raises(GhAuthError) as excinfo:
            pr_mod.assert_gh_auth()
    # Every attempt's record must carry failure_kind=network.
    network_records = [r for r in caplog.records
                       if getattr(r, "failure_kind", None) == "network"]
    assert len(network_records) == len(pr_mod._GH_AUTH_RETRY_DELAYS_S)
    # Final error message surfaces the classification.
    assert "'network'" in str(excinfo.value)


def test_assert_gh_auth_classifies_auth_failure(monkeypatch, caplog):
    """A 401 / "not authenticated" stderr must classify as `auth`, so the
    operator knows to run `gh auth login` rather than wait out a fake
    network problem."""
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=1, stdout="",
                                  stderr="HTTP 401: Bad credentials"))
    with caplog.at_level("INFO", logger="luxe.pr.gh_auth"):
        with pytest.raises(GhAuthError) as excinfo:
            pr_mod.assert_gh_auth()
    auth_records = [r for r in caplog.records
                    if getattr(r, "failure_kind", None) == "auth"]
    assert auth_records, "no records classified as auth"
    assert "'auth'" in str(excinfo.value)


def test_assert_gh_auth_logs_each_attempt_with_classifier(monkeypatch, caplog):
    """Each attempt emits a structured log record with the required
    fields. Future post-mortems need this — the old code only surfaced
    the final stderr."""
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=1, stdout="", stderr="timeout"))
    with caplog.at_level("INFO", logger="luxe.pr.gh_auth"):
        with pytest.raises(GhAuthError):
            pr_mod.assert_gh_auth()
    # One record per attempt (5).
    attempt_records = [r for r in caplog.records
                       if r.message == "assert_gh_auth_attempt"]
    assert len(attempt_records) == 5
    # Every record carries the required structured fields.
    for r in attempt_records:
        for field in ("attempt", "delay_s", "rc", "stderr_excerpt",
                      "failure_kind", "cache_hit"):
            assert hasattr(r, field), f"record missing {field!r}"


def test_assert_gh_auth_ttl_cache_skips_repeat_within_window(monkeypatch):
    """A successful probe stamps the cache. A second call within the TTL
    window must NOT invoke _run at all. This is the defense against
    per-fixture amplification of retry budget during a transient outage."""
    call_count = [0]

    def _capture(cmd, cwd, env=None, timeout=None):
        call_count[0] += 1
        return CmdResult(rc=0, stdout="user", stderr="")

    monkeypatch.setattr(pr_mod, "_run", _capture)
    pr_mod.assert_gh_auth()
    assert call_count[0] == 1
    # Second call within TTL — must short-circuit.
    pr_mod.assert_gh_auth()
    assert call_count[0] == 1, "second call within TTL invoked the probe"


def test_assert_gh_auth_ttl_cache_invalidated_on_failure(monkeypatch):
    """When the retry budget is exhausted, the cache must be invalidated
    so the next caller probes again rather than trusting a stale-ok
    timestamp from earlier in the suite."""
    pr_mod._GH_AUTH_LAST_OK_AT = pr_mod.time.monotonic()
    # First, succeed and stamp the cache:
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=1, stdout="", stderr="timeout"))
    # Force cache to look stale-ok by pre-setting it, then test a fresh call.
    pr_mod._reset_gh_auth_cache()
    pr_mod._GH_AUTH_LAST_OK_AT = pr_mod.time.monotonic()
    # First call — cache hit, no probe:
    pr_mod.assert_gh_auth()
    # Invalidate by hand-clearing (simulates TTL expiry) and force a
    # failing probe — cache must remain cleared afterwards.
    pr_mod._reset_gh_auth_cache()
    with pytest.raises(GhAuthError):
        pr_mod.assert_gh_auth()
    assert pr_mod._GH_AUTH_LAST_OK_AT is None


def test_assert_gh_auth_error_message_references_api_user(monkeypatch):
    """Error string must reference the new probe and must NOT reference
    the old `gh auth status` — otherwise the operator's recovery action
    doesn't match the actual failure."""
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None:
                        CmdResult(rc=1, stdout="",
                                  stderr="HTTP 401: Bad credentials"))
    with pytest.raises(GhAuthError) as excinfo:
        pr_mod.assert_gh_auth()
    msg = str(excinfo.value)
    assert "gh api user" in msg
    assert "auth status" not in msg
    # Reflects new attempt count.
    assert "5 attempts" in msg


def test_assert_gh_auth_timeout_during_probe(monkeypatch):
    """If a single subprocess hits its per-attempt timeout, the retry
    loop must keep going (don't let one hung subprocess collapse the
    whole budget). Repeated timeouts → final error classified as
    `network`."""
    def _hang(cmd, cwd, env=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 0)
    monkeypatch.setattr(pr_mod, "_run", _hang)
    with pytest.raises(GhAuthError) as excinfo:
        pr_mod.assert_gh_auth()
    assert "'network'" in str(excinfo.value)


def test_assert_gh_auth_succeeds_on_retry(monkeypatch):
    """The flake's defining signature: first probe fails, a subsequent
    probe succeeds. The retry path must surface success and stamp the
    cache."""
    seq = iter([
        CmdResult(rc=1, stdout="", stderr="could not resolve"),
        CmdResult(rc=1, stdout="", stderr="could not resolve"),
        CmdResult(rc=0, stdout="user", stderr=""),
    ])
    monkeypatch.setattr(pr_mod, "_run",
                        lambda cmd, cwd, env=None, timeout=None: next(seq))
    pr_mod.assert_gh_auth()
    assert pr_mod._GH_AUTH_LAST_OK_AT is not None
