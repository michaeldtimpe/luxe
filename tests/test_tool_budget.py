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
   it DOWN, not up. That is a benchmark-path behaviour change, so it shipped
   opt-in — and was PROMOTED to default-ON on the maintain/bench path on
   2026-08-12 (`acceptance/toolbudget_ab_2026_08_12/REPORT.md`) with `=0` as
   the exact-string opt-out. `tools/fs.py` itself stays off by default (`None`
   = the fixed constants); the default lives at the maintain call site, and
   chat remains opt-in ("1").
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from luxe import maintain
from luxe.config import load_config
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


class TestTheBenchmarkPathCanReachTheSwitch:
    """`LUXE_TOOL_BUDGET_CTX` on the benchmark/maintain path.

    Until this wiring, `set_read_budget`'s only caller was the chat REPL, so
    `LUXE_TOOL_BUDGET_CTX=1 python -m benchmarks.maintain_suite.run …` was
    inert by construction — the flag never reached the path being measured.
    `maintain.apply_ctx_read_budget` is the bench-side call site;
    `maintain_pipeline` invokes it with the role config the run executes with.

    DEFAULT ON here since 2026-08-12 —
    `acceptance/toolbudget_ab_2026_08_12/REPORT.md` (3 reps × 10 fixtures × 2
    arms, 30/30 · 120/150 both, tokens −3.9%, zero regressions). The opt-out
    grammar is `LUXE_TRUNCATED_TURN_RETRY`'s: ONLY the exact string "0"
    disables. Chat is the deliberate asymmetry and stays opt-in ("1" only).
    """

    def test_unset_applies_the_ctx_derived_budget(self, monkeypatch):
        """The flip: an unset env is the SHIPPED default, and it must be the
        treatment arm's behaviour — same budget, from the same helper."""
        monkeypatch.delenv("LUXE_TOOL_BUDGET_CTX", raising=False)
        assert maintain.apply_ctx_read_budget(32768) == fs.budget_for_ctx(32768)
        assert fs.read_limit() == fs.budget_for_ctx(32768)
        assert fs.read_limit() < fs._MAX_FILE_SIZE

    def test_unset_is_identical_to_the_treatment_arms_explicit_one(
        self, monkeypatch
    ):
        """The A/B measured the treatment arm with the env var set to "1". The
        promoted default must be that arm exactly, not a near-miss."""
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", "1")
        with_one = maintain.apply_ctx_read_budget(32768)
        fs.set_read_budget(None)
        monkeypatch.delenv("LUXE_TOOL_BUDGET_CTX", raising=False)
        unset = maintain.apply_ctx_read_budget(32768)
        assert unset == with_one is not None

    def test_one_still_applies_the_ctx_derived_budget(self, monkeypatch):
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", "1")
        applied = maintain.apply_ctx_read_budget(32768)
        assert applied == fs.budget_for_ctx(32768)
        assert fs.read_limit() == fs.budget_for_ctx(32768)
        assert fs.read_limit() < fs._MAX_FILE_SIZE

    def test_the_exact_string_zero_is_the_only_off_switch(self, monkeypatch):
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", "0")
        assert maintain.apply_ctx_read_budget(32768) is None
        assert fs.read_limit() == fs._MAX_FILE_SIZE

    @pytest.mark.parametrize("value", ["", "true", "yes", "01", " 1", "00"])
    def test_anything_but_the_exact_string_zero_is_on(self, monkeypatch, value):
        """Same grammar as `LUXE_TRUNCATED_TURN_RETRY` (agents/flags.py): a
        near-miss of the off-switch leaves the default ON rather than silently
        disabling a benched default. Note this is NOT chat's rule — chat is
        opt-in and only the exact string "1" enables it there."""
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", value)
        assert maintain.apply_ctx_read_budget(32768) == fs.budget_for_ctx(32768)
        assert fs.read_limit() == fs.budget_for_ctx(32768)

    def test_off_does_not_touch_the_module_state_at_all(self, monkeypatch):
        """Off is "no call", not `set_read_budget(None)`: a budget set by
        something else in this process survives, so the OFF arm can never be
        the thing that changed a limit."""
        fs.set_read_budget(4096)
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", "0")
        assert maintain.apply_ctx_read_budget(32768) is None
        assert fs.read_limit() == 4096

    def test_the_budget_comes_from_the_shipped_bench_role(self, monkeypatch):
        """Requirement: the value derives from the RUN's role num_ctx, not a
        hardcoded 32768. Read the champion config the bench actually uses."""
        cfg = load_config(maintain._default_config())
        num_ctx = cfg.role("monolith").num_ctx
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", "1")
        assert maintain.apply_ctx_read_budget(num_ctx) == fs.budget_for_ctx(num_ctx)

    def test_the_pipeline_passes_the_role_num_ctx(self):
        """Pin the call site: `maintain_pipeline` must read num_ctx off the
        role config it hands to `run_single`, so an A/B of this flag measures
        the window the run really used."""
        src = inspect.getsource(maintain.maintain_pipeline)
        assert 'apply_ctx_read_budget(cfg.role("monolith").num_ctx)' in src

    def test_the_bench_arm_announces_itself(self):
        """Liveness: an ON arm must be checkable in `events.jsonl` rather than
        assumed live — that assumption is what made this flag inert."""
        src = inspect.getsource(maintain.maintain_pipeline)
        assert '"read_budget_applied"' in src

    def test_chat_keeps_its_own_wiring(self):
        """`repl.py` stays the sole authority for chat turns (it re-sets the
        budget every turn because `/ctx` moves num_ctx mid-session). The bench
        call site must not be imported into the chat turn path."""
        from luxe.chat import repl

        assert 'os.environ.get("LUXE_TOOL_BUDGET_CTX") == "1"' in inspect.getsource(repl)
        assert "apply_ctx_read_budget" not in inspect.getsource(repl)
