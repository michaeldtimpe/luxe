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
   2026-08-12 (`acceptance/toolbudget_ab_2026_08_12/REPORT.md`) and on the CHAT
   path on 2026-08-24 (`acceptance/chat_bigread_2026_08_24/REPORT.md`), both
   with `=0` as the exact-string opt-out. `tools/fs.py` itself stays off by
   default (`None` = the fixed constants); the defaults live at the two call
   sites, which stay separate.
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
    fs.set_large_file_notes(False)


def _oversized(tmp_repo: Path, name: str = "auth.log") -> Path:
    p = tmp_repo / name
    p.write_text("y" * (fs._MAX_FILE_SIZE + 5000))
    return p


def _large_but_readable(tmp_repo: Path, name: str = "self.md",
                         fraction: float = 0.9) -> Path:
    """A file past `_LARGE_FILE_FRACTION` of the active `read_limit()` but at
    or under it — the exact session `168f1825a1fd` shape (257,988 B against a
    262,144 B cap, `acceptance/chat_bigread_2026_08_24/EVIDENCE.md` finding
    6)."""
    size = int(fs.read_limit() * fraction)
    p = tmp_repo / name
    p.write_text("y" * size)
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


class TestLargeButReadableFilesGetANudge:
    """The second, LOWER threshold added 2026-08-24
    (`acceptance/chat_bigread_2026_08_24/PLAN.md` 1.5, `EVIDENCE.md` finding
    6, session `168f1825a1fd`). `_oversize_note` used to fire only past
    `read_limit()` — the refusal cap — so a 257,988 B file at 0.98x the
    262,144 B cap listed as a bare name and the model read it whole. This
    class covers the merely-large bracket: `_LARGE_FILE_FRACTION` (0.5) of
    whatever `read_limit()` currently is.

    The bracket is gated on `fs.set_large_file_notes()`, DEFAULT OFF, added
    after coordinator review found the maintain_suite fixture cache holds
    files in this exact band (`nothing-ever-happens/docs/dashboard.jpg`,
    `neon-rain/assets/fonts/Finlandica-VariableFont_wght.ttf`) — an
    unconditional bracket would have changed benchmark-path `list_dir`
    output with no bench evidence. Every test that expects the new note
    turns the toggle on explicitly; `set_root`'s teardown resets it."""

    def test_a_file_past_the_large_threshold_but_under_the_cap_gets_a_note(
        self, tmp_repo
    ):
        fs.set_large_file_notes(True)
        _large_but_readable(tmp_repo)
        out, err = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        assert err is None
        line = next(ln for ln in out.splitlines() if ln.startswith("self.md"))
        assert "large" in line
        assert "KB" in line

    def test_the_large_note_never_claims_refusal(self, tmp_repo):
        """Distinct wording is the whole point — a large-but-readable file
        must not tell the model it will be refused when read_file will
        happily serve it whole."""
        fs.set_large_file_notes(True)
        _large_but_readable(tmp_repo)
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        line = next(ln for ln in out.splitlines() if ln.startswith("self.md"))
        assert "too large to read whole" not in line
        assert "large" in line

    def test_the_large_note_still_points_at_limit_and_grep(self, tmp_repo):
        fs.set_large_file_notes(True)
        _large_but_readable(tmp_repo)
        out, _ = fs.READ_ONLY_FNS["glob"]({"pattern": "*.md"})
        assert "limit=" in out
        assert "grep" in out

    def test_a_binary_file_in_the_large_band_gets_no_note_at_all(
        self, tmp_repo
    ):
        """A JPEG/font in this size band is read_file-unreadable regardless
        of size — "consider read_file limit=/grep" would be actively wrong
        advice, so the toggle-on bracket must skip it rather than reword it."""
        fs.set_large_file_notes(True)
        limit = fs.read_limit()
        size = int(limit * 0.9)
        p = tmp_repo / "dashboard.jpg"
        p.write_bytes(b"\xff\xd8\xff\x00" + b"\x00" * (size - 4))
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        line = next(ln for ln in out.splitlines()
                    if ln.startswith("dashboard.jpg"))
        assert line == "dashboard.jpg"

    def test_a_file_actually_over_the_cap_keeps_the_refusal_note_verbatim(
        self, tmp_repo
    ):
        """The past-limit bracket must be untouched by this change — pinned
        already in TestListingsAnnounceOversizedFiles, reasserted here beside
        the new bracket so the exact string can be compared."""
        fs.set_large_file_notes(True)
        _oversized(tmp_repo)
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        line = next(ln for ln in out.splitlines() if ln.startswith("auth.log"))
        size_kb = (fs._MAX_FILE_SIZE + 5000) / 1024
        assert line == (
            f"auth.log  ({size_kb:,.0f} KB — too large to read whole; "
            f"use read_file limit= or grep)"
        )

    def test_a_file_well_under_the_large_threshold_stays_bare(self, tmp_repo):
        fs.set_large_file_notes(True)
        p = tmp_repo / "small.py"
        p.write_text("x" * 100)
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        line = next(ln for ln in out.splitlines() if ln.startswith("small.py"))
        assert line == "small.py"

    @pytest.mark.parametrize("budget", [4096, 16384, fs._MAX_FILE_SIZE])
    def test_the_two_thresholds_never_contradict_across_budgets(
        self, tmp_repo, budget
    ):
        """For any budget the file is annotated with AT MOST one of the two
        notes, and which one matches what read_file will actually do — the
        invariant `_oversize_note`'s docstring claims, and the one
        test_tool_budget.py:118-125 already pins for the refusal bracket
        alone. This exercises both brackets together at several budgets so
        the thresholds are checked never to invert. Toggle ON: this test is
        specifically about the two brackets' relationship, not the toggle."""
        fs.set_large_file_notes(True)
        fs.set_read_budget(budget)
        limit = fs.read_limit()
        half = int(limit * fs._LARGE_FILE_FRACTION)
        sizes = {"under.bin": half - 1, "large.bin": half + 1,
                  "over.bin": limit + 1}
        for name, size in sizes.items():
            (tmp_repo / name).write_bytes(b"z" * size)

        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        lines = {ln.split("  (")[0]: ln for ln in out.splitlines()}

        assert "large" not in lines["under.bin"]
        assert "too large" not in lines["under.bin"]

        assert "large" in lines["large.bin"]
        assert "too large to read whole" not in lines["large.bin"]

        assert "too large to read whole" in lines["over.bin"]
        # the refusal note must win outright — never both messages at once
        assert lines["over.bin"].count("large") == 1


class TestTheLargeFileToggleDefaultsOff:
    """Coordinator-review follow-up (2026-08-24): the fraction-of-cap bracket
    alone is NOT benchmark-path-neutral — the maintain_suite fixture cache
    holds files between half and the whole of the 262,144 B default cap
    (`nothing-ever-happens/docs/dashboard.jpg`,
    `neon-rain/assets/fonts/Finlandica-VariableFont_wght.ttf`), and
    `nothing-ever-happens` backs doc fixtures whose task lists `docs/`
    directly. `set_large_file_notes` must default OFF and, off, must leave
    `_oversize_note` byte-identical to the code before the bracket existed."""

    def test_the_toggle_defaults_off(self):
        assert fs.large_file_notes_enabled() is False

    def test_toggle_off_a_128_to_256kb_file_is_byte_identical_to_before(
        self, tmp_repo
    ):
        """The exact fixture-cache shape: a file well past half the default
        256 KB cap but under it. With the toggle at its default (off), this
        must render exactly like a bare name — no note of either kind."""
        assert fs.large_file_notes_enabled() is False
        p = tmp_repo / "dashboard.jpg"
        p.write_bytes(b"\xff\xd8\xff" + b"z" * (200 * 1024))
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        line = next(ln for ln in out.splitlines()
                    if ln.startswith("dashboard.jpg"))
        assert line == "dashboard.jpg"

    def test_toggle_off_glob_is_also_unaffected(self, tmp_repo):
        p = tmp_repo / "Finlandica-VariableFont_wght.ttf"
        p.write_bytes(b"z" * (220 * 1024))
        out, _ = fs.READ_ONLY_FNS["glob"]({"pattern": "*.ttf"})
        assert "large" not in out
        assert out.strip() == "Finlandica-VariableFont_wght.ttf"

    @pytest.mark.parametrize("toggle", [False, True])
    def test_the_refusal_bracket_is_identical_in_both_toggle_states(
        self, tmp_repo, toggle
    ):
        """The toggle governs the large bracket ONLY — a file that
        `read_file` will actually refuse must get the same verbatim message
        whether or not large-file notes are enabled."""
        fs.set_large_file_notes(toggle)
        _oversized(tmp_repo)
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        line = next(ln for ln in out.splitlines() if ln.startswith("auth.log"))
        size_kb = (fs._MAX_FILE_SIZE + 5000) / 1024
        assert line == (
            f"auth.log  ({size_kb:,.0f} KB — too large to read whole; "
            f"use read_file limit= or grep)"
        )

    def test_set_large_file_notes_true_then_false_restores_the_default(
        self, tmp_repo
    ):
        fs.set_large_file_notes(True)
        assert fs.large_file_notes_enabled() is True
        fs.set_large_file_notes(False)
        assert fs.large_file_notes_enabled() is False


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
    disables. Chat runs the same grammar since 2026-08-24, but off its own
    evidence and its own call site (see `TestChatDefaultsTheBudgetOn`).
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
        disabling a benched default. Chat's call site spells the same rule —
        pinned separately in `TestChatDefaultsTheBudgetOn`."""
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
        budget every turn because `/ctx` moves num_ctx mid-session). The two
        paths now spell the SAME opt-out grammar, but they are still two call
        sites: the bench one must not be imported into the chat turn path, or a
        chat turn would inherit maintain's once-per-pipeline scope."""
        from luxe.chat import repl

        assert (
            'os.environ.get("LUXE_TOOL_BUDGET_CTX", "1") != "0"'
            in inspect.getsource(repl)
        )
        assert "apply_ctx_read_budget" not in inspect.getsource(repl)


class TestChatDefaultsTheBudgetOn:
    """Chat's flip to default-ON (2026-08-24).

    It shipped opt-in on 2026-08-12 with `tools.sdd` forbidding an "alignment"
    with the bench grammar until chat produced its own arm. It has one:
    `acceptance/chat_bigread_2026_08_24/REPORT.md` — planted repo (250,040 B
    markdown + 70,028 B source), m1/Qwen3.6-35B-A3B-4bit, both arms × 32768 and
    131072. OFF hung unrecoverably at BOTH windows (peak context pressure
    1064.2% / 266.0%, process group killed by the drill's timeout); ON
    completed both (60.0s / 79.9s, peak 50.6% / 39.0%, 2 refused reads each)
    and the model spent the `offset=` resume the clipped read hands it (1 / 2
    calls) — 3 extra tool calls total, not a turn traded for three timid ones.

    Behaviour is asserted through `prepare_turn`, chat's real call site, so
    these pin the shipped path and not a re-implementation of its condition.
    """

    @pytest.fixture
    def chat_turn(self, tmp_path, monkeypatch):
        """Minimal `prepare_turn` harness (mirrors tests/test_turn_core.py):
        isolated HOME, stubbed Backend, no model or network."""
        from luxe.chat import repl as repl_mod
        from luxe.chat import slots as slots_mod
        from luxe.chat.session import ChatSession
        from luxe.config import PipelineConfig, RoleConfig
        from luxe.memory import session as session_store

        class _FakeBackend:
            def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
                self.base_url, self.model = base_url, model

            def unload_all_loaded(self, *, except_for=None):
                return {}

            def thermal_guard(self, *a, **k):
                return True

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(slots_mod, "Backend", _FakeBackend)
        repo = tmp_path / "chatrepo"
        repo.mkdir()
        cfg = PipelineConfig(
            models={"monolith": "Champ"},
            roles={"monolith": RoleConfig(model_key="monolith", num_ctx=32768)},
        )
        session = ChatSession(repo_path=str(repo))
        meta = session_store.new_session(
            repo_path=str(repo), project_hash="h", slot_models={})
        session.session_id = meta.session_id
        sm = slots_mod.SlotManager(cfg)

        def run() -> int:
            prep = repl_mod.prepare_turn(
                "hello", session, sm, cfg, frozenset(), lambda m: "review")
            return prep.role_cfg.num_ctx

        return run

    def test_unset_applies_the_ctx_derived_budget(self, chat_turn, monkeypatch):
        """The flip: an unset env is now chat's SHIPPED default, and it must be
        the drill's ON arm — the same budget from the same helper."""
        monkeypatch.delenv("LUXE_TOOL_BUDGET_CTX", raising=False)
        num_ctx = chat_turn()
        assert fs.read_limit() == fs.budget_for_ctx(num_ctx)
        assert fs.read_limit() < fs._MAX_FILE_SIZE

    def test_unset_is_identical_to_the_drills_explicit_one(
        self, chat_turn, monkeypatch
    ):
        """The drill measured its ON arm with the env var set to "1". The
        promoted default must be that arm exactly, not a near-miss."""
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", "1")
        chat_turn()
        with_one = fs.read_limit()
        fs.set_read_budget(None)
        monkeypatch.delenv("LUXE_TOOL_BUDGET_CTX", raising=False)
        chat_turn()
        assert fs.read_limit() == with_one < fs._MAX_FILE_SIZE

    def test_the_exact_string_zero_is_the_only_off_switch(
        self, chat_turn, monkeypatch
    ):
        """`=0` restores the fixed constants — the pre-2026-08-24 behaviour,
        which is what the OFF arm of the drill ran."""
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", "0")
        chat_turn()
        assert fs.read_limit() == fs._MAX_FILE_SIZE

    @pytest.mark.parametrize("value", ["", "true", "yes", "01", " 1", "00"])
    def test_anything_but_the_exact_string_zero_is_on(
        self, chat_turn, monkeypatch, value
    ):
        """Same grammar as `maintain.apply_ctx_read_budget` and
        `LUXE_TRUNCATED_TURN_RETRY`: a near-miss of the off-switch leaves the
        default ON rather than silently restoring the cap that hung the turn."""
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", value)
        num_ctx = chat_turn()
        assert fs.read_limit() == fs.budget_for_ctx(num_ctx)

    def test_off_still_lands_on_a_known_limit(self, chat_turn, monkeypatch):
        """Chat's off branch DOES call `set_read_budget(None)` — unlike the
        bench path's "no call at all" — so a turn can never inherit a budget
        some other caller in the process left behind."""
        fs.set_read_budget(4096)
        monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", "0")
        chat_turn()
        assert fs.read_limit() == fs._MAX_FILE_SIZE

    def test_the_budget_is_re_set_every_turn(self, chat_turn, monkeypatch):
        """Per-turn scope is the chat-side invariant (`/ctx` moves num_ctx
        mid-session), and it is what keeps the two call sites independent."""
        monkeypatch.delenv("LUXE_TOOL_BUDGET_CTX", raising=False)
        num_ctx = chat_turn()
        fs.set_read_budget(4096)
        chat_turn()
        assert fs.read_limit() == fs.budget_for_ctx(num_ctx)


class TestChatWiresTheLargeFileNotesOn:
    """The toggle's call site (2026-08-24, PLAN.md 1.5 + 3.1 follow-up).

    `set_large_file_notes` shipped dormant — default OFF, no caller — because
    unconditionally it is NOT benchmark-path-neutral (see
    `TestTheLargeFileToggleDefaultsOff` above for the two fixture-cache files
    that sit in its band). Chat is not a benched path, so it is ON there, wired
    in `prepare_turn` beside the read budget: both are process-global module
    state, and `/ctx` moves `num_ctx` mid-session, so both are re-set per turn.
    """

    def test_the_module_default_is_still_off(self):
        """The property the bench path depends on: nothing turns this on
        unless a caller asks. Kept alongside the wiring test so a future
        "just default it on" edit fails here first."""
        assert fs.large_file_notes_enabled() is False

    def test_prepare_turn_enables_it(self):
        from luxe.chat import repl

        src = inspect.getsource(repl.prepare_turn)
        assert "fs_mod.set_large_file_notes(True)" in src

    def test_it_sits_with_the_per_turn_read_budget(self):
        """Same function, same per-turn scope, same reason. If the budget
        block moves, this must move with it."""
        from luxe.chat import repl

        src = inspect.getsource(repl.prepare_turn)
        assert 'os.environ.get("LUXE_TOOL_BUDGET_CTX", "1") != "0"' in src
        assert "fs_mod.set_large_file_notes(True)" in src

    def test_the_bench_path_has_no_such_call(self):
        """`maintain.py` deliberately never enables it — that is what keeps
        the benchmark path byte-identical by construction, rather than by a
        flag someone could set."""
        assert "set_large_file_notes" not in inspect.getsource(maintain)

    def test_the_wiring_is_not_gated_on_an_env_var(self):
        """Chat is unbenched, so this needs no lever. A future flag here would
        be a promotion decision, not a refactor."""
        from luxe.chat import repl

        src = inspect.getsource(repl.prepare_turn)
        line = next(ln for ln in src.splitlines()
                    if "set_large_file_notes" in ln
                    and not ln.lstrip().startswith("#"))
        assert "environ" not in line
        assert line.strip() == "fs_mod.set_large_file_notes(True)"

    def test_on_the_toggle_annotates_the_incident_shape(self, tmp_repo):
        """End of the wire: with the toggle in the state chat sets, the file
        that lost session 168f1825a1fd is no longer a bare name."""
        fs.set_large_file_notes(True)
        _large_but_readable(tmp_repo)
        out, _ = fs.READ_ONLY_FNS["list_dir"]({"path": "."})
        line = next(ln for ln in out.splitlines() if ln.startswith("self.md"))
        assert line != "self.md"
        assert "large" in line
