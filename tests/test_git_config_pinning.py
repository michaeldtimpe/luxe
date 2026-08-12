"""git ran with whatever the host's config said, and decoded strictly.

Every consumer of `gitcmd` PARSES the text it gets back, and one branch of that
parsing grades a benchmark. Three host settings quietly rewrite it:

- `color.ui=always` makes git emit ANSI escapes even into a pipe, so status,
  branch and diff parsing all see decorated text;
- `diff.noprefix=true` / `diff.mnemonicprefix=true` change `+++ b/<path>` into
  `+++ <path>` / `+++ w/<path>`, which is the exact line `pr.diff_against_base`
  and `spec_validator._added_lines_from_diff` key on — every added line becomes
  invisible and a satisfied requirement grades as unmet with "0 added lines";
- `diff.external` replaces git's diff wholesale.

All three are things a person sets for their own comfort, and none of them
would produce an error — just a wrong number. They are pinned at the
invocation, which is a no-op on a default host. The two PARSING call sites also
set `GIT_CONFIG_NOSYSTEM=1` for /etc/gitconfig and pass `--no-ext-diff`.

Strict decoding is the fourth: a commit message or a filename that is not valid
UTF-8 raised `UnicodeDecodeError` and threw the whole output away.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe import gitcmd, pr, spec_validator


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


class TestTheConfigIsPinnedAtEveryInvocation:
    def test_run_carries_the_pins(self, repo, monkeypatch):
        seen = []
        real = subprocess.run
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: (seen.append(list(cmd)),
                                               real(cmd, **kw))[1])
        gitcmd.run(repo, "status", "--porcelain")
        assert "color.ui=false" in seen[0]
        assert "--no-pager" in seen[0]

    def test_run_in_carries_the_pins(self, repo, monkeypatch):
        seen = []
        real = subprocess.run
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: (seen.append(list(cmd)),
                                               real(cmd, **kw))[1])
        gitcmd.run_in(repo, "status", "--porcelain")
        assert "color.ui=false" in seen[0]

    def test_a_host_forcing_colour_cannot_reach_the_output(self, repo):
        """The reproduction: `color.ui=always` in the repo's own config."""
        _git(repo, "config", "color.ui", "always")
        (repo / "a.txt").write_text("first\nsecond\n")
        out = gitcmd.run(repo, "diff").stdout
        assert "\x1b[" not in out, "ANSI escapes reached a parsed capture"

    def test_the_pins_are_a_no_op_on_a_default_host(self, repo):
        (repo / "a.txt").write_text("first\nsecond\n")
        assert gitcmd.run(repo, "diff").stdout == _git(repo, "diff")

    def test_run_ok_still_reports_a_bad_repo_as_failure(self, tmp_path):
        ok, text = gitcmd.run_ok(tmp_path / "nope", "status")
        assert ok is False and text


class TestTheParsingCallSitesPinDiffFormat:
    def _diff_argv(self, monkeypatch) -> list[list[str]]:
        seen: list[list[str]] = []
        real = subprocess.run

        def _spy(cmd, **kw):
            seen.append(list(cmd))
            return real(cmd, **kw)

        monkeypatch.setattr(subprocess, "run", _spy)
        return seen

    def test_pr_diff_against_base(self, repo, monkeypatch):
        base = _git(repo, "rev-parse", "HEAD").strip()
        seen = self._diff_argv(monkeypatch)
        pr.diff_against_base(repo, base)
        diff_argv = [a for a in seen if "diff" in a]
        assert diff_argv
        for argv in diff_argv:
            assert "diff.noprefix=false" in argv
            assert "--no-ext-diff" in argv

    def test_spec_validator_diff(self, repo, monkeypatch):
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "b.txt").write_text("added line\n")
        seen = self._diff_argv(monkeypatch)
        spec_validator._added_lines_from_diff(repo, base)
        diff_argv = [a for a in seen if "diff" in a]
        assert diff_argv
        assert all("diff.noprefix=false" in a for a in diff_argv)

    def test_the_parsing_env_disables_system_config_and_prompts(self):
        env = gitcmd.parse_env()
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_added_lines_survive_a_host_that_sets_noprefix(self, repo):
        """The grading-zeroed scenario, end to end."""
        _git(repo, "config", "diff.noprefix", "true")
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "b.txt").write_text("the added line\n")
        added = spec_validator._added_lines_from_diff(repo, base)
        assert added is not None
        assert "the added line" in [body for _, body in added]
        assert any("b.txt" in fname for fname, _ in added)

    def test_added_lines_survive_a_host_that_forces_colour(self, repo):
        _git(repo, "config", "color.ui", "always")
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "b.txt").write_text("the added line\n")
        added = spec_validator._added_lines_from_diff(repo, base)
        assert added is not None
        assert "the added line" in [body for _, body in added]

    def test_diff_against_base_survives_a_host_that_forces_colour(self, repo):
        _git(repo, "config", "color.ui", "always")
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "b.txt").write_text("one\ntwo\n")
        adds, dels, patch = pr.diff_against_base(repo, base)
        assert (adds, dels) == (2, 0)
        assert "\x1b[" not in patch


class TestNonUtf8Output:
    def test_a_bad_byte_in_a_commit_message_does_not_kill_the_call(self, repo):
        msg = repo / "msg"
        msg.write_bytes(b"subject with \xff a bad byte\n")
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-F", str(msg)],
                       cwd=repo, check=True)
        proc = gitcmd.run(repo, "log", "-1", "--format=%s")
        assert proc.returncode == 0
        assert "bad byte" in proc.stdout
