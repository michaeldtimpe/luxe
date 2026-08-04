"""Session working notes (chat/notes.py).

The three properties that matter: it writes something useful, it never
destroys user text, and it NEVER blocks or breaks exit.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from luxe.chat import notes as notes_mod
from luxe.chat.session import ChatSession, ChatTurn
from luxe.config import PipelineConfig, RoleConfig
from luxe.memory import project as project_mem


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _resp(text: str):
    """The REAL response type. A hand-rolled stub with a `.content` attribute
    is what let `distil` read the wrong field and silently do nothing on every
    live session while these tests stayed green (2026-08-04)."""
    from luxe.backend import ChatResponse
    return ChatResponse(text=text)


class _Backend:
    def __init__(self, content="- did a thing\n- tried X, failed\n- open: Y",
                 raises=None):
        self.content = content
        self.raises = raises
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        if self.raises is not None:
            raise self.raises
        return _resp(self.content)


class _Slots:
    def __init__(self, backend, cfg):
        self.backend = backend
        self.cfg = cfg

    def backend_for(self, slot):
        return self.backend


def _cfg(**kw) -> PipelineConfig:
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")},
                          **kw)


def _session(repo: Path, turns: int = 3) -> ChatSession:
    s = ChatSession(repo_path=str(repo), session_id="abcdef123456",
                    project_kind="git")
    for i in range(turns):
        s.add_turn(ChatTurn(user=f"q{i}", assistant=f"a{i}"))
    return s


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    return r


class TestWrite:
    def test_notes_written_on_session_end(self, repo):
        b = _Backend()
        res = notes_mod.run_session_notes(_session(repo), _Slots(b, _cfg()),
                                          _cfg(), _console())
        assert res.written == repo / ".luxe" / "memory.md"
        text = res.written.read_text()
        assert "<!-- luxe:notes begin" in text
        assert "did a thing" in text and "tried X, failed" in text

    def test_the_distillation_input_is_the_fold_not_the_raw_transcript(self, repo):
        b = _Backend()
        notes_mod.run_session_notes(_session(repo, turns=8), _Slots(b, _cfg()),
                                    _cfg(), _console())
        user_msg = b.calls[0][1]["content"]
        assert "<session_transcript>" in user_msg
        assert "[user]" in user_msg and "[assistant]" in user_msg

    def test_the_prompt_comes_from_the_registry(self, repo):
        from luxe.agents.prompts import SESSION_NOTES_HINT
        b = _Backend()
        notes_mod.run_session_notes(_session(repo), _Slots(b, _cfg()),
                                    _cfg(), _console())
        assert b.calls[0][0]["content"] == SESSION_NOTES_HINT

    def test_entry_is_dated_and_stamped_with_the_session(self, repo):
        b = _Backend()
        res = notes_mod.run_session_notes(_session(repo), _Slots(b, _cfg()),
                                          _cfg(), _console())
        block = project_mem.read_block(res.written.read_text(), "notes")
        assert block.startswith("### ") and "abcdef12" in block.splitlines()[0]

    def test_one_line_is_printed_naming_the_disable_switch(self, repo):
        console = _console()
        notes_mod.run_session_notes(_session(repo), _Slots(_Backend(), _cfg()),
                                    _cfg(), console)
        out = console.file.getvalue()
        assert "session notes" in out and "notes: false" in out


class TestRollingWindow:
    def test_evicts_at_entry_six(self, repo):
        cfg = _cfg()
        for i in range(7):
            b = _Backend(content=f"- entry number {i}")
            notes_mod.run_session_notes(_session(repo), _Slots(b, cfg), cfg,
                                        _console())
        block = project_mem.read_block(
            (repo / ".luxe" / "memory.md").read_text(), "notes")
        assert block.count("### ") == notes_mod.MAX_ENTRIES
        assert "entry number 6" in block          # newest kept
        assert "entry number 0" not in block      # oldest evicted

    def test_newest_first(self, repo):
        cfg = _cfg()
        for i in range(3):
            notes_mod.run_session_notes(_session(repo),
                                        _Slots(_Backend(f"- e{i}"), cfg), cfg,
                                        _console())
        block = project_mem.read_block(
            (repo / ".luxe" / "memory.md").read_text(), "notes")
        assert block.index("- e2") < block.index("- e1") < block.index("- e0")

    def test_roll_respects_the_character_ceiling(self):
        old = [f"### old {i}\n{'x' * 400}" for i in range(4)]
        out = notes_mod.roll(old, "### new\nfresh")
        assert len(out) <= notes_mod.MAX_BLOCK_CHARS
        assert "fresh" in out

    def test_roll_keeps_the_new_entry_even_when_it_alone_busts_the_budget(self):
        out = notes_mod.roll(["### old\nx"], "### new\n" + "y" * 5000)
        assert out.startswith("### new")

    def test_entry_is_capped(self, repo):
        b = _Backend(content="z" * 5000)
        res = notes_mod.run_session_notes(_session(repo), _Slots(b, _cfg()),
                                          _cfg(), _console())
        block = project_mem.read_block(res.written.read_text(), "notes")
        assert len(block) <= notes_mod.MAX_ENTRY_CHARS + 80   # + the ### header


class TestPreservesUserText:
    def test_curated_text_and_the_brief_block_both_survive(self, repo):
        project_mem.splice_block(repo, "brief", "THE BRIEF BODY")
        mem = repo / ".luxe" / "memory.md"
        mem.write_text("# Hand-written\nkeep me exactly\n\n" + mem.read_text())
        before = mem.read_text()

        notes_mod.run_session_notes(_session(repo),
                                    _Slots(_Backend(), _cfg()), _cfg(),
                                    _console())
        after = mem.read_text()
        assert after.startswith("# Hand-written\nkeep me exactly")
        assert "THE BRIEF BODY" in after
        assert project_mem.read_block(after, "brief") == "THE BRIEF BODY"
        assert len(after) > len(before)

    def test_repeated_writes_keep_exactly_one_notes_block(self, repo):
        cfg = _cfg()
        for _ in range(3):
            notes_mod.run_session_notes(_session(repo),
                                        _Slots(_Backend(), cfg), cfg, _console())
        text = (repo / ".luxe" / "memory.md").read_text()
        assert text.count("<!-- luxe:notes begin") == 1


class TestSkipPaths:
    def test_no_project_skips(self, tmp_path):
        s = ChatSession(repo_path="", project_kind="none")
        res = notes_mod.run_session_notes(s, _Slots(_Backend(), _cfg()),
                                          _cfg(), _console())
        assert res.written is None and "no project" in res.skipped

    def test_short_session_skips(self, repo):
        res = notes_mod.run_session_notes(_session(repo, turns=1),
                                          _Slots(_Backend(), _cfg()), _cfg(),
                                          _console())
        assert res.written is None and "answered turns" in res.skipped

    def test_config_off_skips(self, repo):
        cfg = _cfg(notes=False)
        res = notes_mod.run_session_notes(_session(repo),
                                          _Slots(_Backend(), cfg), cfg,
                                          _console())
        assert res.written is None and "notes: false" in res.skipped
        assert not (repo / ".luxe").exists()

    def test_backend_down_skips_silently(self, repo):
        b = _Backend(raises=OSError("connection refused"))
        console = _console()
        res = notes_mod.run_session_notes(_session(repo), _Slots(b, _cfg()),
                                          _cfg(), console)
        assert res.written is None and "OSError" in res.skipped
        assert console.file.getvalue() == ""     # silent — quitting isn't news

    def test_ctrl_c_during_distillation_does_not_propagate(self, repo):
        b = _Backend(raises=KeyboardInterrupt())
        res = notes_mod.run_session_notes(_session(repo), _Slots(b, _cfg()),
                                          _cfg(), _console())
        assert res.written is None and "KeyboardInterrupt" in res.skipped

    def test_system_exit_still_propagates(self, repo):
        b = _Backend(raises=SystemExit(3))
        with pytest.raises(SystemExit):
            notes_mod.run_session_notes(_session(repo), _Slots(b, _cfg()),
                                        _cfg(), _console())

    def test_empty_model_output_writes_nothing(self, repo):
        res = notes_mod.run_session_notes(_session(repo),
                                          _Slots(_Backend(content="  "), _cfg()),
                                          _cfg(), _console())
        assert res.written is None and not (repo / ".luxe").exists()

    def test_unwritable_repo_is_a_silent_skip(self, repo, monkeypatch):
        monkeypatch.setattr(notes_mod, "write_notes",
                            lambda *a, **k: (_ for _ in ()).throw(
                                PermissionError("read-only fs")))
        res = notes_mod.run_session_notes(_session(repo),
                                          _Slots(_Backend(), _cfg()), _cfg(),
                                          _console())
        assert res.written is None and "PermissionError" in res.skipped


class TestOnDemand:
    def test_note_bypasses_the_config_toggle_and_turn_floor(self, repo):
        cfg = _cfg(notes=False)
        res = notes_mod.run_session_notes(_session(repo, turns=1),
                                          _Slots(_Backend(), cfg), cfg,
                                          _console(), on_demand=True)
        assert res.written is not None

    def test_note_still_refuses_without_a_project(self):
        s = ChatSession(repo_path="", project_kind="none")
        console = _console()
        res = notes_mod.run_session_notes(s, _Slots(_Backend(), _cfg()),
                                          _cfg(), console, on_demand=True)
        assert res.written is None
        assert "no project" in console.file.getvalue()


class TestInjection:
    def test_notes_reach_the_next_session_s_project_memory(self, repo):
        notes_mod.run_session_notes(_session(repo),
                                    _Slots(_Backend("- shipped the parser"),
                                           _cfg()), _cfg(), _console())
        block = project_mem.render_block(project_mem.load_memory(repo))
        assert "shipped the parser" in block


class TestBackendContract:
    """Pin the response contract itself — the stub-shape bug above cost a
    whole feature silently."""

    def test_distil_reads_the_real_chatresponse_field(self, repo):
        from luxe.backend import ChatResponse

        class B:
            def chat(self, messages, **kw):
                return ChatResponse(text="- a real bullet")

        assert notes_mod.distil(_session(repo), B()) == "- a real bullet"

    def test_chatresponse_has_no_content_field(self):
        """If this ever fails, `distil` needs revisiting — not this test."""
        from luxe.backend import ChatResponse
        assert not hasattr(ChatResponse(), "content")
        assert hasattr(ChatResponse(), "text")


class TestBulletRecovery:
    """The champion narrates a "thinking process" before complying. Recover
    the answer deterministically instead of prompting harder (CLAUDE.md)."""

    _TRACE = """Here's a thinking process:

1.  **Analyze User Input:**
   - The user provided a short transcript.
   - No code was written.

2.  **Apply Constraints:**
   - Output ONLY 3 to 6 markdown bullets.
   - Under 900 characters total.

- Read `calc.py`; nothing was changed this session.
- Discussed that `add` would concatenate if `b` were a string.
- Open: no test covers the string case.
"""

    def test_the_reasoning_trace_is_dropped(self):
        out = notes_mod.extract_bullets(self._TRACE)
        assert out.startswith("- Read `calc.py`")
        assert "thinking process" not in out
        assert "Apply Constraints" not in out
        assert out.count("\n") == 2

    def test_a_clean_reply_is_untouched(self):
        clean = "- one\n- two\n- three"
        assert notes_mod.extract_bullets(clean) == clean

    def test_wrapped_continuation_lines_are_kept(self):
        text = "- a bullet that\n  wraps onto a second line\n- another"
        assert notes_mod.extract_bullets(text) == text

    def test_prose_only_replies_are_not_blanked(self):
        assert notes_mod.extract_bullets("just prose, no bullets") == \
            "just prose, no bullets"

    def test_distil_applies_it(self, repo):
        from luxe.backend import ChatResponse

        class B:
            def chat(self, messages, **kw):
                return ChatResponse(text=TestBulletRecovery._TRACE)

        out = notes_mod.distil(_session(repo), B())
        assert out.startswith("- Read `calc.py`") and "thinking" not in out
