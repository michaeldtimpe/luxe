"""The git tools cut output silently and took model-supplied refs as options.

**The cut was silent and mid-line.** `_run_git` returned `proc.stdout[:32768]`.
Verified: `git_diff(ref="HEAD~40")` on this repo returned exactly 32,768
characters, ending in the middle of a hunk, with `err=None` — a partial diff
that reads as the whole diff, ending in a record the model cannot parse.

**A ref could be an option.** `ref`/`n` go straight into argv, so
`git_diff(ref="--output=/tmp/x.patch")` made git WRITE A FILE outside the repo
from a read-only tool, and returned `("", None)` — the write reported as an
empty diff. `--end-of-options` makes everything after it a revision or path,
whatever it looks like; `git_log`'s `n` is spliced into `-{n}` and is coerced
with `int()` for the same reason.

Both fixes have to leave ordinary output alone, so the byte-identity of a
normal diff/log/show is asserted directly against raw git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.tools import fs, git as git_tools


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True)
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "a.txt").write_text("first\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "one")
    (tmp_path / "a.txt").write_text("first\nsecond\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "two")
    (tmp_path / "a.txt").write_text("first\nsecond\nthird\n")
    fs.set_repo_root(tmp_path)
    yield tmp_path
    fs._REPO_ROOT = None


def _big(repo: Path, line_fmt: str, n: int = 8000) -> None:
    """A tracked-enough big file: `git add -N` is what makes a new file show
    up in `git diff` at all (the same intent-to-add trick pr.py uses)."""
    (repo / "big.txt").write_text("".join(line_fmt.format(i) for i in range(n)))
    _git(repo, "add", "-N", ".")


class TestTruncationIsAnnounced:
    def test_a_long_diff_says_it_was_cut(self, repo):
        _big(repo, "line {}\n")
        out, err = git_tools.TOOL_FNS["git_diff"]({})
        assert err is None
        assert "truncated at 32,768 bytes" in out

    def test_the_note_offers_a_way_around_it(self, repo):
        _big(repo, "line {}\n")
        out, _ = git_tools.TOOL_FNS["git_diff"]({})
        assert "path argument" in out.splitlines()[-1]

    def test_it_does_not_end_mid_line(self, repo):
        """Fixed-width lines, so a cut one is visible by length alone."""
        _big(repo, "x" * 200 + "{:04d}\n")
        out, _ = git_tools.TOOL_FNS["git_diff"]({})
        body = [ln for ln in out.splitlines() if ln.startswith("+x")]
        assert body and all(len(ln) == 205 for ln in body), "a line was cut"

    def test_output_under_the_cap_is_byte_identical(self, repo):
        out, err = git_tools.TOOL_FNS["git_diff"]({})
        assert err is None
        assert out == _git(repo, "diff")

    def test_exactly_at_the_cap_is_untouched(self):
        exact = "x" * 32768
        assert git_tools._truncated(exact, 32768) == exact

    def test_one_byte_over_is_announced(self):
        got = git_tools._truncated("a\n" * 20, 10)
        assert got.startswith("a\na\na\na\na\n")
        assert "truncated at 10 bytes" in got


class TestOptionShapedRefs:
    def test_a_ref_cannot_write_a_file(self, repo, tmp_path):
        target = tmp_path / "escaped.patch"
        out, err = git_tools.TOOL_FNS["git_diff"]({"ref": f"--output={target}"})
        assert not target.exists(), "a read-only tool wrote outside the repo"
        assert err is not None, "the failure must be reported, not returned empty"

    def test_a_ref_cannot_smuggle_a_flag_into_show(self, repo, tmp_path):
        target = tmp_path / "escaped2.patch"
        out, err = git_tools.TOOL_FNS["git_show"]({"ref": f"--output={target}"})
        assert not target.exists()
        assert err is not None

    def test_end_of_options_precedes_a_diff_ref(self, repo):
        seen = []
        real = subprocess.run

        def _spy(cmd, **kw):
            seen.append(list(cmd))
            return real(cmd, **kw)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(subprocess, "run", _spy)
            git_tools.TOOL_FNS["git_diff"]({"ref": "HEAD~1", "path": "a.txt"})
        argv = seen[0]
        assert argv[argv.index("HEAD~1") - 1] == "--end-of-options"
        assert argv[argv.index("a.txt") - 1] == "--"

    def test_a_normal_diff_against_a_ref_is_byte_identical(self, repo):
        out, err = git_tools.TOOL_FNS["git_diff"]({"ref": "HEAD~1"})
        assert err is None
        assert out == _git(repo, "diff", "HEAD~1")

    def test_a_normal_show_is_byte_identical(self, repo):
        fmt = "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%s%n%b"
        out, err = git_tools.TOOL_FNS["git_show"]({"ref": "HEAD"})
        assert err is None
        assert out == _git(repo, "show", "HEAD", "--stat", fmt)

    def test_a_path_filtered_diff_is_byte_identical(self, repo):
        out, _ = git_tools.TOOL_FNS["git_diff"]({"path": "a.txt"})
        assert out == _git(repo, "diff", "--", "a.txt")


class TestGitLogN:
    def test_n_must_be_an_integer(self, repo):
        out, err = git_tools.TOOL_FNS["git_log"]({"n": "5 --output=/tmp/x"})
        assert out == ""
        assert err and "must be an integer" in err

    def test_a_numeric_string_is_accepted(self, repo):
        out, err = git_tools.TOOL_FNS["git_log"]({"n": "1"})
        assert err is None
        assert len(out.strip().splitlines()) == 1

    def test_the_default_is_byte_identical(self, repo):
        out, err = git_tools.TOOL_FNS["git_log"]({})
        assert err is None
        assert out == _git(repo, "log", "-20", "--oneline", "--no-decorate")


class TestFailuresStillSurface:
    def test_an_unknown_ref_is_an_error_not_an_empty_diff(self, repo):
        out, err = git_tools.TOOL_FNS["git_diff"]({"ref": "no-such-ref"})
        assert out == ""
        assert err
