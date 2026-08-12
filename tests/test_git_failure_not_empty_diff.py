"""A failed git command graded as "the agent wrote nothing".

`pr.diff_against_base` returned `(0, 0, "")` when git FAILED, which is
byte-for-byte what it returns for a clean tree. Downstream, every consumer read
the failure as an agent outcome: `maintain` recorded `additions=0`, the
under-engagement gate fired a second pass on the strength of it, and
gitchange's executor announced "no changes produced — skipping". Same shape in
`spec_validator._added_lines_from_diff`, which returned `[]` and made every
`regex_present` requirement grade unmet with "0 added lines".

The other half is the network: `git ls-remote`, `git push` and `gh pr checks`
ran with no timeout, no `GIT_TERMINAL_PROMPT=0`, and luxe's stdin inherited —
so a private remote could park a maintain run on an unanswerable credential
prompt forever, with no output and no deadline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe import pr, spec_validator
from luxe.pr import CmdResult, GitDiffError
from luxe.spec import Requirement, Spec


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "a.txt").write_text("first\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "one")
    return tmp_path


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


class TestDiffAgainstBase:
    def test_a_clean_tree_is_still_zero(self, repo):
        """The value that used to be ambiguous keeps its honest meaning."""
        assert pr.diff_against_base(repo, _base(repo)) == (0, 0, "")

    def test_real_changes_are_unchanged(self, repo):
        base = _base(repo)
        (repo / "a.txt").write_text("first\nsecond\n")
        (repo / "new.txt").write_text("brand new\n")
        adds, dels, patch = pr.diff_against_base(repo, base)
        assert (adds, dels) == (2, 0)
        assert "brand new" in patch

    def test_a_bad_base_sha_raises_instead_of_reporting_zero(self, repo):
        with pytest.raises(GitDiffError) as e:
            pr.diff_against_base(repo, "no-such-sha")
        assert "no-such-sha" in str(e.value)

    def test_a_timeout_raises_too(self, repo, monkeypatch):
        base = _base(repo)

        def _boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        monkeypatch.setattr(pr.subprocess, "run", _boom)
        with pytest.raises(GitDiffError) as e:
            pr.diff_against_base(repo, base)
        assert "timed out" in str(e.value)

    def test_the_diff_commands_are_bounded_and_non_interactive(self, repo,
                                                               monkeypatch):
        base = _base(repo)
        seen: list[dict] = []
        real = subprocess.run

        def _spy(cmd, **kw):
            # Only the calls this function makes — the spy sees the whole
            # process's subprocess.run, test helpers included.
            if "diff.noprefix=false" in cmd:
                seen.append(kw)
            return real(cmd, **kw)

        monkeypatch.setattr(pr.subprocess, "run", _spy)
        pr.diff_against_base(repo, base)
        assert seen and all(kw.get("timeout") for kw in seen)
        assert all(kw.get("stdin") is subprocess.DEVNULL for kw in seen)
        assert all(kw.get("errors") == "replace" for kw in seen)


class TestSpecValidationDistinguishesErrorFromUnmet:
    def _spec(self) -> Spec:
        return Spec(goal="g", requirements=[
            Requirement(id="R1", must="adds the flag",
                        done_when="pattern in an added line",
                        kind="regex_present", pattern="FLAG"),
        ])

    def test_a_git_failure_is_not_an_unmet_requirement(self, repo):
        got = spec_validator.validate(self._spec(), repo, "no-such-sha")
        assert got.error is not None
        assert "NOT EVALUATED" in got.results[0].detail

    def test_the_detail_says_it_is_not_evidence(self, repo):
        got = spec_validator.validate(self._spec(), repo, "no-such-sha")
        assert "not evidence" in got.results[0].detail

    def test_a_healthy_run_carries_no_error(self, repo):
        base = _base(repo)
        (repo / "b.py").write_text("FLAG = 1\n")
        got = spec_validator.validate(self._spec(), repo, base)
        assert got.error is None
        assert got.all_satisfied is True

    def test_a_genuinely_unmet_requirement_still_reads_as_unmet(self, repo):
        base = _base(repo)
        got = spec_validator.validate(self._spec(), repo, base)
        assert got.error is None
        assert got.all_satisfied is False
        assert "0 added lines" in got.results[0].detail

    def test_the_helper_returns_none_only_for_a_failure(self, repo):
        assert spec_validator._added_lines_from_diff(repo, "no-such-sha") is None
        assert spec_validator._added_lines_from_diff(repo, _base(repo)) == []
        assert spec_validator._added_lines_from_diff(repo, "") == []

    def test_non_diff_requirements_are_evaluated_normally_anyway(self, repo):
        spec = Spec(goal="g", requirements=[
            Requirement(id="R1", must="adds the flag", done_when="pattern",
                        kind="regex_present", pattern="FLAG"),
            Requirement(id="R2", must="tests pass", done_when="exit 0",
                        kind="tests_pass", command="true"),
        ])
        got = spec_validator.validate(spec, repo, "no-such-sha")
        assert got.results[1].satisfied is True


class TestNetworkCommandsAreBounded:
    def test_run_is_non_interactive_and_decodes_loosely(self, tmp_path,
                                                        monkeypatch):
        seen: dict = {}
        real = subprocess.run

        def _spy(cmd, **kw):
            seen.update(kw)
            return real(cmd, **kw)

        monkeypatch.setattr(pr.subprocess, "run", _spy)
        pr._run(["git", "--version"], cwd=tmp_path)
        assert seen.get("stdin") is subprocess.DEVNULL
        assert seen.get("errors") == "replace"

    def test_run_net_passes_a_deadline_and_the_no_prompt_env(self, tmp_path,
                                                             monkeypatch):
        seen: dict = {}

        def _fake(cmd, cwd, env=None, timeout=None):
            seen.update(env=env, timeout=timeout)
            return CmdResult(rc=0, stdout="", stderr="")

        monkeypatch.setattr(pr, "_run", _fake)
        pr._run_net(["git", "ls-remote"], cwd=tmp_path)
        assert seen["timeout"] == pr._NET_TIMEOUT_S
        assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert seen["env"]["GIT_ASKPASS"] == ""

    def test_a_timeout_becomes_a_failed_result_not_empty_output(self, tmp_path,
                                                                monkeypatch):
        def _hang(cmd, cwd, env=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 0)

        monkeypatch.setattr(pr, "_run", _hang)
        got = pr._run_net(["git", "push"], cwd=tmp_path)
        assert got.ok is False
        assert got.rc == 124
        assert "timed out" in got.stderr

    @pytest.mark.parametrize("call,argv0", [
        (lambda p: pr._branch_exists_remote(p, "b"), "git"),
        (lambda p: pr.detect_base_branch(p), "gh"),
    ])
    def test_the_network_probes_route_through_run_net(self, tmp_path, monkeypatch,
                                                      call, argv0):
        seen: list[list[str]] = []

        def _fake_net(cmd, cwd, *, timeout=pr._NET_TIMEOUT_S):
            seen.append(list(cmd))
            return CmdResult(rc=1, stdout="", stderr="")

        monkeypatch.setattr(pr, "_run_net", _fake_net)
        monkeypatch.setattr(pr, "_run",
                            lambda *a, **kw: CmdResult(rc=1, stdout="", stderr=""))
        call(tmp_path)
        assert seen and seen[0][0] == argv0

    def test_push_routes_through_run_net(self, tmp_path, monkeypatch):
        from luxe.run_state import PRState, RunSpec

        seen: list[list[str]] = []

        def _fake_net(cmd, cwd, *, timeout=pr._NET_TIMEOUT_S):
            seen.append(list(cmd))
            return CmdResult(rc=0, stdout="", stderr="")

        monkeypatch.setattr(pr, "_run_net", _fake_net)
        spec = RunSpec(run_id="r1", repo_path=str(tmp_path), goal="g",
                       task_type="implement", base_branch="main",
                       base_sha="abc")
        state = PRState()
        state.branch_name = "luxe/implement/x"
        pr._do_push(spec, state)
        assert seen == [["git", "push", "-u", "origin", "luxe/implement/x"]]

    def test_a_push_timeout_is_reported_not_swallowed(self, tmp_path, monkeypatch):
        from luxe.run_state import PRState, RunSpec

        monkeypatch.setattr(
            pr, "_run_net",
            lambda cmd, cwd, **kw: CmdResult(rc=124, stdout="",
                                             stderr="timed out after 120s"))
        spec = RunSpec(run_id="r1", repo_path=str(tmp_path), goal="g",
                       task_type="implement", base_branch="main",
                       base_sha="abc")
        state = PRState()
        state.branch_name = "luxe/implement/x"
        with pytest.raises(pr.PRError) as e:
            pr._do_push(spec, state)
        assert "timed out" in str(e.value)
