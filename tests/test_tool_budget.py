"""Tool-output sizing, and telling the model the limits BEFORE it hits them.

Two problems, one root cause: the model learned every limit by failing a call.

1. Listings returned BARE NAMES. `glob` said "auth.log" and nothing else, so
   the only way to discover the file was unreadable was to call `read_file`
   and be refused — one full model round-trip per discovery. Session
   0e524f033300 spent two refused reads on a 442 KB log and then abandoned
   tools entirely; a later probe spent ten calls learning a 320 KB bundle was
   one line. Both sizes were knowable at listing time.

2. The 256 KB read cap predates the `/ctx` tiers and is sized as though every
   session ran at 256K. In real tokens (~1.67 chars/token on code) one
   max-size read is 480% of the DEFAULT 32K window and still 60% of the
   LARGEST window luxe can open — so "scale it with ctx" resolves to scaling
   it DOWN, not up. That is a benchmark-path behaviour change, so it is
   opt-in and off by default.
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
    fs.set_read_budget(None)


def _oversized(tmp_repo: Path, name: str = "auth.log") -> Path:
    p = tmp_repo / name
    p.write_text("y" * (fs._MAX_FILE_SIZE + 5000))
    return p


class TestListingsAnnounceOversizedFiles:
    def test_list_dir_flags_a_file_read_file_would_refuse(self, tmp_repo):
        _oversized(tmp_repo)
        out, err = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        assert err is None
        line = next(ln for ln in out.splitlines() if ln.startswith("auth.log"))
        assert "too large to read whole" in line
        assert "KB" in line

    def test_glob_flags_it_too(self, tmp_repo):
        """`glob` is usually the FIRST call — catching it here saves the most."""
        _oversized(tmp_repo)
        out, _ = fs.READ_ONLY_FNS["glob"]({"pattern": "*.log"})
        assert "too large to read whole" in out

    def test_the_note_names_the_two_ways_forward(self, tmp_repo):
        _oversized(tmp_repo)
        out, _ = fs.READ_ONLY_FNS["glob"]({"pattern": "*.log"})
        assert "limit=" in out
        assert "grep" in out

    def test_a_tree_with_nothing_oversized_is_byte_identical(self, tmp_repo):
        """The benchmark path only sees this change where it would have helped.
        Most repos have no file past the limit, and those listings must be
        unchanged to the byte."""
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        assert "(" not in out
        assert "too large" not in out

    def test_normal_files_beside_an_oversized_one_stay_bare(self, tmp_repo):
        _oversized(tmp_repo)
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        others = [ln for ln in out.splitlines() if not ln.startswith("auth.log")]
        assert others, "fixture should have other entries"
        assert all("too large" not in ln for ln in others)

    def test_directories_are_never_annotated(self, tmp_repo):
        (tmp_repo / "pkg").mkdir(exist_ok=True)
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        assert "pkg/" in out
        assert "pkg/  (" not in out

    def test_a_file_that_vanishes_mid_listing_is_skipped_silently(self, tmp_repo):
        """`stat` races `iterdir` all the time — a temp file deleted between
        the two must yield no annotation, not an exception that takes the
        whole listing down."""
        assert fs._oversize_note(tmp_repo / "never-existed.log") == ""

    def test_an_unstattable_entry_is_skipped_silently(self, tmp_repo,
                                                      monkeypatch):
        gone = tmp_repo / "gone.log"
        gone.write_text("x" * (fs._MAX_FILE_SIZE + 10))
        # A REAL vanished-file error: pathlib.is_dir() only swallows OSErrors
        # whose errno it recognises (ENOENT/ENOTDIR/EBADF/ELOOP), so a bare
        # OSError() with no errno would re-raise and test the wrong thing.
        real_stat = Path.stat

        def _boom(self, *a, **kw):
            if self.name == "gone.log":
                raise FileNotFoundError(2, "vanished")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", _boom)
        out, err = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        assert err is None
        assert "gone.log" in out
        assert "too large" not in out

    def test_the_annotation_tracks_the_active_budget(self, tmp_repo):
        """Lower the budget and files that were fine become flagged — the note
        must never disagree with what `read_file` will actually do."""
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        assert "too large" not in out
        fs.set_read_budget(16)
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        assert "too large" in out


class TestTheCtxDerivedBudget:
    @pytest.mark.parametrize("num_ctx,expected_kb", [
        (32768, 13), (65536, 27), (131072, 53), (262144, 107),
    ])
    def test_the_budget_is_a_quarter_of_the_window(self, num_ctx, expected_kb):
        assert round(fs.budget_for_ctx(num_ctx) / 1024) == expected_kb

    def test_a_tiny_window_gets_the_floor(self, tmp_repo):
        """8K * 25% is ~3 KB — too small to read an ordinary source file, so
        the budget stops shrinking rather than making the tool useless."""
        assert fs.budget_for_ctx(8192) == fs.READ_BUDGET_FLOOR

    def test_every_tier_lands_below_the_old_fixed_cap(self):
        """The finding that answers "should we increase it": 256 KB is larger
        than a quarter of even the biggest window luxe can open."""
        assert fs.budget_for_ctx(262144) < fs._MAX_FILE_SIZE

    @pytest.mark.parametrize("bad", [0, -1, -10 ** 9])
    def test_a_degenerate_window_falls_back_to_the_constant(self, bad):
        assert fs.budget_for_ctx(bad) == fs._MAX_FILE_SIZE


class TestTheOverrideIsOffByDefault:
    def test_the_default_limit_is_the_historical_constant(self):
        assert fs.read_limit() == fs._MAX_FILE_SIZE

    def test_setting_none_restores_it(self):
        fs.set_read_budget(4096)
        assert fs.read_limit() == 4096
        fs.set_read_budget(None)
        assert fs.read_limit() == fs._MAX_FILE_SIZE

    @pytest.mark.parametrize("bad", [0, -5])
    def test_a_nonpositive_override_is_ignored(self, bad):
        fs.set_read_budget(bad)
        assert fs.read_limit() == fs._MAX_FILE_SIZE

    def test_the_budget_actually_governs_reads(self, tmp_repo):
        """End to end: a file that reads fine at the default is refused under a
        small budget, and the refusal quotes the ACTIVE limit."""
        p = tmp_repo / "mid.py"
        p.write_text("z" * 20000)
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "mid.py"})
        assert err is None

        fs.set_read_budget(fs.budget_for_ctx(8192))     # 8 KB floor
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "mid.py"})
        assert out == ""
        assert "8,192" in err
        assert "read_file(" in err                       # still actionable

    def test_a_window_still_works_under_a_small_budget(self, tmp_repo):
        p = tmp_repo / "mid.py"
        p.write_text("line\n" * 5000)
        fs.set_read_budget(fs.budget_for_ctx(8192))
        out, err = fs.READ_ONLY_FNS["read_file"]({"path": "mid.py", "limit": 20})
        assert err is None
        assert len(out.splitlines()) == 20
