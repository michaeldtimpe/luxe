"""Oversized `read_file` reads — refusal wording and the windowed path.

Founding instance, session 0e524f033300 (2026-08-11): a chat turn asked about
a 442,195-byte `auth.log`. `read_file` refused it with

    File too large (442195 bytes, limit 262144)

which names a limit and no way forward, and — the part that actually cost the
turn — the SIZE GATE RAN BEFORE `offset`/`limit` WERE CONSULTED, so the model
retried with `limit=30` and got the same refusal. Its own tool description had
promised "use offset/limit for large files". After two refusals it stopped
using tools, wrote from what one grep had returned, and ran into the 8,192-
token cap; the truncated-turn retry then spent two more full generations on it.

So: a windowed read is honoured at any file size (bounded by `_MAX_READ_BYTES`
on the way out), and the unwindowed refusal states the exact call to make next.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.tools import fs


@pytest.fixture(autouse=True)
def set_root(tmp_repo: Path):
    fs.set_repo_root(tmp_repo)
    yield
    fs._REPO_ROOT = None


def _big_log(tmp_repo: Path, lines: int = 6000) -> Path:
    """A file comfortably past _MAX_FILE_SIZE, shaped like the auth.log that
    triggered this (long, uniform, line-oriented)."""
    p = tmp_repo / "auth.log"
    p.write_text("".join(
        f"Aug 11 18:{i % 60:02d}:00 host sshd[{i}]: Failed password for root "
        f"from 10.0.0.{i % 256} port {2000 + i} ssh2\n"
        for i in range(lines)))
    assert p.stat().st_size > fs._MAX_FILE_SIZE
    return p


class TestUnwindowedRefusal:
    def test_it_names_the_call_to_make_instead_of_just_the_limit(self, tmp_repo):
        _big_log(tmp_repo)
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "auth.log"})
        assert out == ""
        assert err is not None
        # The whole point: a next action, not a wall.
        assert "read_file(" in err
        assert "offset=0" in err and "limit=500" in err
        assert "auth.log" in err
        # And the alternative that avoids paging entirely.
        assert "grep(" in err

    def test_it_still_reports_the_size_and_limit(self, tmp_repo):
        p = _big_log(tmp_repo)
        _, err = fs.READ_ONLY_FNS["read_file"]({"path": "auth.log"})
        assert f"{p.stat().st_size:,}" in err
        assert f"{fs._MAX_FILE_SIZE:,}" in err

    def test_it_counts_lines_when_the_file_is_cheap_to_scan(self, tmp_repo):
        _big_log(tmp_repo, lines=6000)
        _, err = fs.READ_ONLY_FNS["read_file"]({"path": "auth.log"})
        assert "6,000 lines" in err

    def test_line_counting_is_skipped_for_very_large_files(self, tmp_repo,
                                                           monkeypatch):
        """The count costs a full scan; past the threshold the message is just
        as actionable without it."""
        monkeypatch.setattr(fs, "_LINE_COUNT_MAX_BYTES", 1024)
        _big_log(tmp_repo)
        _, err = fs.READ_ONLY_FNS["read_file"]({"path": "auth.log"})
        assert "lines)" not in err
        assert "read_file(" in err  # still actionable


class TestWindowedRead:
    def test_limit_is_honoured_past_the_size_gate(self, tmp_repo):
        """The regression that cost the founding turn: `limit` was ignored
        because the size gate ran first."""
        _big_log(tmp_repo)
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "auth.log", "limit": 30})
        assert err is None
        assert len(out.splitlines()) == 30
        assert out.startswith("1\t")

    def test_offset_numbers_lines_from_the_window_start(self, tmp_repo):
        _big_log(tmp_repo)
        out, err = fs.READ_ONLY_FNS["read_file"](
            {"path": "auth.log", "offset": 100, "limit": 5})
        assert err is None
        lines = out.splitlines()
        assert len(lines) == 5
        assert lines[0].startswith("101\t")
        assert lines[-1].startswith("105\t")
        assert "sshd[100]" in lines[0]

    def test_a_window_past_eof_is_empty_not_an_error(self, tmp_repo):
        _big_log(tmp_repo, lines=6000)
        out, err = fs.READ_ONLY_FNS["read_file"](
            {"path": "auth.log", "offset": 99_000, "limit": 10})
        assert err is None
        assert out == ""

    def test_output_is_capped_even_when_limit_is_huge(self, tmp_repo):
        """`limit` is the model's number and can be absurd; the byte ceiling is
        ours. Without it, `limit=10**9` reinstates the context flood the size
        gate existed to prevent."""
        _big_log(tmp_repo)
        out, err = fs.READ_ONLY_FNS["read_file"](
            {"path": "auth.log", "limit": 10 ** 9})
        assert err is None
        assert len(out) < fs._MAX_READ_BYTES + 500  # cap + the continue note
        assert "[truncated at" in out
        assert "read_file(" in out  # says how to continue

    def test_the_truncation_note_resumes_where_it_stopped(self, tmp_repo):
        _big_log(tmp_repo)
        out, _ = fs.READ_ONLY_FNS["read_file"]({"path": "auth.log", "limit": 10 ** 9})
        body = [ln for ln in out.splitlines() if "\t" in ln]
        last_no = int(body[-1].split("\t", 1)[0])
        # Line numbers are 1-based, offsets are 0-based: the next window starts
        # at the line after the last one returned.
        assert f"offset={last_no}" in out

    def test_a_small_file_is_unchanged_by_any_of_this(self, tmp_repo):
        """The benchmark path reads files under the limit; those must behave
        exactly as before."""
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "src/main.py"})
        assert err is None
        assert out.startswith("1\t")
        assert "[truncated" not in out


class TestBinarySniffStaysBounded:
    def test_a_large_binary_is_refused_without_being_read_whole(self, tmp_repo,
                                                               monkeypatch):
        """The sniff used to be `read_bytes()[:8192]` — the whole file, then a
        slice. Harmless while the size gate rejected everything big first;
        now that a window can be requested at any size, it would have loaded
        the file into memory to decide it was binary."""
        p = tmp_repo / "blob.bin"
        p.write_bytes(b"\x00\xff" * (400 * 1024))

        real_read_bytes = Path.read_bytes

        def _forbidden(self, *a, **kw):  # pragma: no cover - fails the test
            raise AssertionError("read_bytes() on the whole file")

        monkeypatch.setattr(Path, "read_bytes", _forbidden)
        try:
            out, err = fs.READ_ONLY_FNS["read_file"](
                {"path": "blob.bin", "limit": 10})
        finally:
            monkeypatch.setattr(Path, "read_bytes", real_read_bytes)
        assert out == ""
        assert "binary" in err.lower()


class TestHostileArgs:
    """Found 2026-08-11 by fuzzing the rewritten reader.

    The list slicing this replaced silently absorbed negatives — `lines[-1:]`
    meant "the last line", `lines[:-5]` meant "all but the last five". Neither
    is a sensible reading of "start line" / "max lines", but both RETURNED.
    `itertools.islice` raises ValueError instead, and that escaped the
    `except OSError`, so a model passing `offset=-1` got a raw
    `ValueError: Stop argument for islice()...` out of a tool that
    tools.sdd says must never raise.
    """

    @pytest.mark.parametrize("args", [
        {"offset": -1}, {"limit": -5}, {"offset": -3, "limit": -1},
        {"offset": -10 ** 9}, {"limit": -10 ** 9},
    ])
    def test_negative_indices_do_not_raise(self, tmp_repo, args):
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "src/main.py", **args})
        assert err is None
        assert out.startswith("1\t")          # clamped to the top of the file

    def test_a_negative_limit_is_no_limit_not_a_window(self, tmp_repo):
        """So an oversized file still gets the actionable refusal rather than
        being served a nonsense window."""
        _big_log(tmp_repo)
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "auth.log", "limit": -5})
        assert out == ""
        assert "read_file(" in err

    @pytest.mark.parametrize("args", [
        {"limit": [3]}, {"offset": {"a": 1}}, {"limit": "lots"},
        {"offset": None}, {"limit": None}, {"offset": "2"},
    ])
    def test_wrong_types_degrade_instead_of_raising(self, tmp_repo, args):
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "src/main.py", **args})
        assert err is None


class TestOneEnormousLine:
    """A single line larger than the whole byte budget — minified JS, a
    one-line JSON dump, an embedded base64 blob.

    The first version of the truncation note advised `offset=0, limit=1`: the
    exact call that had just returned nothing, because the one line it wanted
    did not fit. A model following that advice repeats it forever. No window
    can help here, so the tool has to say so and name a different tool.
    """

    def _minified(self, tmp_repo: Path) -> Path:
        p = tmp_repo / "bundle.min.js"
        p.write_text("z" * (fs._MAX_READ_BYTES + 5000))
        return p

    def test_it_refuses_instead_of_advising_the_same_call(self, tmp_repo):
        self._minified(tmp_repo)
        out, err = fs.READ_ONLY_FNS["read_file"](
            {"path": "bundle.min.js", "limit": 1})
        assert out == ""
        assert err is not None
        assert "offset=" not in err          # the loop that would never end
        assert "grep(" in err                # a tool that can actually help

    def test_it_names_the_line_and_the_likely_cause(self, tmp_repo):
        self._minified(tmp_repo)
        _, err = fs.READ_ONLY_FNS["read_file"]({"path": "bundle.min.js", "limit": 1})
        assert "Line 1" in err
        assert "minified" in err

    def test_it_reports_the_line_the_window_landed_on(self, tmp_repo):
        """Not always line 1 — the oversized line can be anywhere."""
        p = tmp_repo / "mixed.js"
        p.write_text("short\n" + "y" * (fs._MAX_READ_BYTES + 100) + "\n")
        _, err = fs.READ_ONLY_FNS["read_file"](
            {"path": "mixed.js", "offset": 1, "limit": 1})
        assert "Line 2" in err

    def test_a_partial_window_still_gets_the_resume_hint(self, tmp_repo):
        """Non-regression: when SOME lines fit, the normal continue-from-here
        note must still appear."""
        p = tmp_repo / "mixed.js"
        p.write_text("short\n" + "y" * (fs._MAX_READ_BYTES + 100) + "\n")
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "mixed.js", "limit": 2})
        assert err is None
        assert "[truncated at" in out
        assert "offset=1" in out


class TestMalformedArgs:
    def test_a_non_numeric_limit_degrades_to_unwindowed(self, tmp_repo):
        """Tool args come from the model. A junk `limit` must not raise out of
        a tool fn — tools.sdd: errors return, never raise."""
        _big_log(tmp_repo)
        out, err = fs.READ_ONLY_FNS["read_file"](
            {"path": "auth.log", "limit": "lots"})
        assert out == ""
        assert "read_file(" in err

    def test_a_non_numeric_offset_reads_from_the_start(self, tmp_repo):
        out, err = fs.READ_ONLY_FNS["read_file"](
            {"path": "src/main.py", "offset": "top"})
        assert err is None
        assert out.startswith("1\t")
