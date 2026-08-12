"""grep's two silent failures, found 2026-08-12.

**1. It returned "(no matches)" for everything under a piped stdin.**

`_grep` invokes ripgrep with no path argument. In that form rg decides between
"search the working directory" and "read stdin" by inspecting stdin — and an
inherited OPEN PIPE sends it down the stdin branch, where it hits EOF, finds
nothing and exits 1. `_grep` reports that as "(no matches)". Silently, for
every search.

That is the state of every NON-INTERACTIVE luxe: a benchmark launched from a
script, CI, and the headless chat form the README documents
(`printf 'msg\\n/quit\\n' | luxe chat`). An interactive TTY session was fine,
which is why it survived: the failure mode is invisible in exactly the
configuration a human watches.

**2. Both of its caps were silent.**

`--max-count=150` (per file) and a `[:32768]` byte slice reshaped results with
no trace. Asked to count something with 1,286 matches, the model received
exactly 150 lines and nothing to suggest they were partial — a wrong answer
wearing the shape of a complete one.

**3. It reported every FAILURE as "(no matches)" too (2026-08-12, second
pass).**

ripgrep's exit codes are 0=matched, 1=no matches, 2+=error, and all three
funnelled into `_grep_result(proc.stdout)` — which sees the empty string an
error leaves behind and says "(no matches)". Two verified cases: the pattern
`"(unclosed"` (a regex the model can plainly fix, if it is told) and the
pattern `"-i"`, which rg consumed as a FLAG because nothing ended its option
parsing. A timeout, meanwhile, escaped as an exception where tools.sdd says a
tool returns `(str, str | None)` and never raises. The pure-python fallback
for rg-less hosts had the ORIGINAL silent-cap bug one file over: it stopped
dead at 150 results with no note and no byte bound at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe.tools import fs


@pytest.fixture(autouse=True)
def set_root(tmp_repo: Path):
    fs.set_repo_root(tmp_repo)
    yield
    fs._REPO_ROOT = None


class TestStdinDoesNotSwallowResults:
    def test_the_subprocess_gets_devnull_not_the_inherited_stdin(self,
                                                                 tmp_repo,
                                                                 monkeypatch):
        """The fix itself. Asserted on the CALL rather than the result, because
        the failure only reproduces when the test runner's own stdin happens to
        be a pipe — which is not something a test should depend on."""
        seen = {}
        real = subprocess.run

        def _spy(cmd, **kw):
            if cmd and cmd[0] == "rg":
                seen.update(kw)
            return real(cmd, **kw)

        monkeypatch.setattr(subprocess, "run", _spy)
        fs.READ_ONLY_FNS["grep"]({"pattern": "anything"})
        assert seen.get("stdin") is subprocess.DEVNULL

    def test_a_match_is_found_with_stdin_piped(self, tmp_repo):
        """End to end under the condition that used to fail. `tmp_repo` has
        source in it; grep must see it whatever this process's stdin is."""
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "greet"})
        assert err is None
        assert out != "(no matches)"
        assert "greet" in out

    def test_no_explicit_path_arg_is_added(self, tmp_repo, monkeypatch):
        """Passing "." would also fix it, but prefixes every result with "./"
        — a format change the benchmark and the golden request would see."""
        seen = []
        real = subprocess.run

        def _spy(cmd, **kw):
            if cmd and cmd[0] == "rg":
                seen.append(list(cmd))
            return real(cmd, **kw)

        monkeypatch.setattr(subprocess, "run", _spy)
        out, _ = fs.READ_ONLY_FNS["grep"]({"pattern": "greet"})
        assert seen and seen[0][-1] == "greet", seen
        assert "." not in seen[0]
        assert not out.startswith("./")


class TestTruncationIsAnnounced:
    def _many(self, tmp_repo: Path, n: int) -> None:
        (tmp_repo / "big.log").write_text("".join(f"line {i} MATCHME\n"
                                                  for i in range(n)))

    def test_hitting_the_per_file_cap_says_so(self, tmp_repo):
        self._many(tmp_repo, 500)
        out, _ = fs.READ_ONLY_FNS["grep"]({"pattern": "MATCHME"})
        assert "cap" in out
        assert "NOT the total count" in out

    def test_the_note_names_the_capped_file(self, tmp_repo):
        self._many(tmp_repo, 500)
        out, _ = fs.READ_ONLY_FNS["grep"]({"pattern": "MATCHME"})
        assert "big.log" in out.splitlines()[-1]

    def test_the_note_offers_a_way_to_get_the_real_count(self, tmp_repo):
        """Naming the problem without a next step is the dead end this whole
        day has been about."""
        self._many(tmp_repo, 500)
        out, _ = fs.READ_ONLY_FNS["grep"]({"pattern": "MATCHME"})
        last = out.splitlines()[-1]
        assert "Narrow" in last and "bash" in last

    def test_a_result_under_the_cap_is_unannotated(self, tmp_repo):
        """Byte-identical to before for the ordinary case, which is what keeps
        this off the benchmark's back except where it was already wrong."""
        self._many(tmp_repo, 5)
        out, _ = fs.READ_ONLY_FNS["grep"]({"pattern": "MATCHME"})
        assert "[" not in out
        assert len(out.splitlines()) == 5

    def test_no_matches_is_unchanged(self, tmp_repo):
        out, _ = fs.READ_ONLY_FNS["grep"]({"pattern": "zzz-not-present-zzz"})
        assert out == "(no matches)"

    def test_the_byte_cap_is_announced_and_cuts_on_a_line_boundary(self):
        """A result sliced mid-line hands the model a corrupt final record."""
        wide = "".join(f"f.py:{i}:{'x' * 400}\n" for i in range(200))
        got = fs._grep_result(wide)
        assert "output truncated" in got
        body = [ln for ln in got.splitlines() if ln.startswith("f.py:")]
        assert all(len(ln) > 400 for ln in body), "a line was cut in half"

    def test_the_two_notes_combine(self):
        wide = "".join(f"f.py:{i}:{'x' * 400}\n" for i in range(400))
        got = fs._grep_result(wide)
        assert "output truncated" in got
        assert "cap" in got


def _fake_rg(monkeypatch, *, rc: int, stdout: str = "", stderr: str = ""):
    """Replace the rg call with a fixed CompletedProcess, so the exit-code
    contract is tested without depending on a ripgrep version's diagnostics."""
    real = subprocess.run

    def _spy(cmd, **kw):
        if cmd and cmd[0] == "rg":
            return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
        return real(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", _spy)


class TestAFailedSearchIsNotNoMatches:
    def test_exit_2_is_an_error_not_a_result(self, tmp_repo, monkeypatch):
        _fake_rg(monkeypatch, rc=2, stderr="regex parse error: unclosed group")
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "(unclosed"})
        assert out == ""
        assert err and "unclosed group" in err

    def test_the_error_names_the_exit_code_when_rg_says_nothing(self, tmp_repo,
                                                                monkeypatch):
        _fake_rg(monkeypatch, rc=3)
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "x"})
        assert out == ""
        assert err == "grep failed (rg exit 3)"

    def test_exit_1_still_means_no_matches(self, tmp_repo, monkeypatch):
        """1 is not an error — the byte-identical path must stay put."""
        _fake_rg(monkeypatch, rc=1)
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "x"})
        assert (out, err) == ("(no matches)", None)

    def test_an_unclosed_group_really_does_exit_2(self, tmp_repo):
        """Unfaked, against the host's real rg: the reported reproduction."""
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "(unclosed"})
        assert out != "(no matches)"
        assert err is not None

    def test_a_timeout_comes_back_as_a_tool_error(self, tmp_repo, monkeypatch):
        """tools.sdd: all tool errors return as a tuple; never raise."""
        def _boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        monkeypatch.setattr(subprocess, "run", _boom)
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "x"})
        assert out == ""
        assert err and "timed out" in err


class TestOptionShapedPatterns:
    def test_a_dash_pattern_is_a_pattern_not_a_flag(self, tmp_repo):
        """`grep("-i")` returned "(no matches)" for a file that contains it."""
        (tmp_repo / "flags.txt").write_text("run with -i for insensitive\n")
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "-i"})
        assert err is None
        assert "flags.txt" in out

    def test_the_pattern_sits_behind_a_double_dash(self, tmp_repo, monkeypatch):
        seen = []
        real = subprocess.run

        def _spy(cmd, **kw):
            if cmd and cmd[0] == "rg":
                seen.append(list(cmd))
            return real(cmd, **kw)

        monkeypatch.setattr(subprocess, "run", _spy)
        fs.READ_ONLY_FNS["grep"]({"pattern": "greet"})
        assert seen[0][-2:] == ["--", "greet"]

    def test_the_double_dash_leaves_ordinary_output_byte_identical(self, tmp_repo):
        """The whole point of `--` over sanitising the pattern: rg treats
        `-- PATTERN` and `PATTERN` the same, so results don't move."""
        with_dashdash, _ = fs.READ_ONLY_FNS["grep"]({"pattern": "greet"})
        raw = subprocess.run(
            ["rg", "--no-heading", "-n", "--max-count=150", "greet"],
            capture_output=True, text=True, cwd=tmp_repo,
            stdin=subprocess.DEVNULL)
        assert with_dashdash == fs._grep_result(raw.stdout)

    def test_the_glob_filter_still_applies(self, tmp_repo):
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "greet", "glob": "*.md"})
        assert err is None
        assert out == "(no matches)"


class TestThePurePythonFallbackAnnouncesItsCaps:
    """Hosts without ripgrep take `_grep_python`. It had the silent-cap bug
    the rg path was just fixed for."""

    def _no_rg(self, monkeypatch):
        real = subprocess.run

        def _spy(cmd, **kw):
            if cmd and cmd[0] == "rg":
                raise FileNotFoundError("rg")
            return real(cmd, **kw)

        monkeypatch.setattr(subprocess, "run", _spy)

    def test_it_is_reached_when_rg_is_missing(self, tmp_repo, monkeypatch):
        self._no_rg(monkeypatch)
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "greet"})
        assert err is None
        assert "greet" in out

    def test_hitting_the_result_cap_says_so(self, tmp_repo, monkeypatch):
        self._no_rg(monkeypatch)
        (tmp_repo / "big.log").write_text("".join(f"line {i} MATCHME\n"
                                                  for i in range(500)))
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "MATCHME"})
        assert err is None
        assert "NOT the total count" in out
        assert "Narrow" in out.splitlines()[-1]

    def test_it_is_byte_capped_like_the_rg_path(self, tmp_repo, monkeypatch):
        self._no_rg(monkeypatch)
        (tmp_repo / "wide.log").write_text("".join(f"{'x' * 4000} MATCHME\n"
                                                   for i in range(60)))
        out, _ = fs.READ_ONLY_FNS["grep"]({"pattern": "MATCHME"})
        assert len(out) < fs._GREP_MAX_BYTES + 500
        assert "output truncated" in out

    def test_a_small_result_is_unannotated(self, tmp_repo, monkeypatch):
        self._no_rg(monkeypatch)
        out, _ = fs.READ_ONLY_FNS["grep"]({"pattern": "greet"})
        assert "[" not in out

    def test_no_matches_is_unchanged(self, tmp_repo, monkeypatch):
        self._no_rg(monkeypatch)
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "zzz-absent-zzz"})
        assert (out, err) == ("(no matches)", None)

    def test_an_invalid_pattern_is_still_reported(self, tmp_repo, monkeypatch):
        self._no_rg(monkeypatch)
        out, err = fs.READ_ONLY_FNS["grep"]({"pattern": "(unclosed"})
        assert out == ""
        assert err and "Invalid pattern" in err
