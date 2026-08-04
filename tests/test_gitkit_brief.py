"""`luxe init` — the per-repo orientation brief in `.luxe/memory.md`.

The load-bearing property is not the prose: it's that a user's own notes in
that file survive every re-init byte-for-byte. No model is involved — the
single `run_single` pass is monkeypatched, gitkit-test style.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from luxe.config import PipelineConfig, RoleConfig
from luxe.gitkit import brief as brief_mod
from luxe.memory import project as project_mem


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


class _Result:
    def __init__(self, text):
        self.final_text = text
        self.wall_s = 0.1
        self.steps = 1
        self.tool_calls_total = 0


class _Backend:
    def __init__(self, *a, **k):
        self.base_url = "http://x"
        self.model = "Champ"

    def unload_all_loaded(self, **k):
        return {}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    (r / "src").mkdir(parents=True)
    (r / "pyproject.toml").write_text("[project]\nname='proj'\n")
    (r / "src" / "app.py").write_text("def main():\n    return 1\n")
    subprocess.run(["git", "init", "-q", "."], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=r, check=True)
    return r


@pytest.fixture
def cfg() -> PipelineConfig:
    return PipelineConfig(models={"monolith": "Champ"},
                          roles={"monolith": RoleConfig(model_key="monolith")})


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def _run(repo, cfg, text, **kw):
    seen: dict = {}

    def fake_run_single(backend, role, **kwargs):
        seen.update(kwargs)
        return _Result(text)

    res = brief_mod.run_init(repo, cfg, console=_console(),
                             run_single_fn=fake_run_single,
                             backend=_Backend(), **kw)
    res._seen = seen        # type: ignore[attr-defined]
    return res


class TestWriteSemantics:
    def test_writes_a_fenced_block(self, repo, cfg):
        res = _run(repo, cfg, "## What this is\nA thing.")
        assert res.ok and res.written == repo / ".luxe" / "memory.md"
        text = res.written.read_text()
        assert "<!-- luxe:brief begin" in text and "<!-- luxe:brief end -->" in text
        assert "A thing." in text
        assert project_mem.read_block(text, "brief").startswith("## What this is")

    def test_curated_text_survives_a_reinit_byte_for_byte(self, repo, cfg):
        _run(repo, cfg, "first draft")
        mem = repo / ".luxe" / "memory.md"
        curated = ("# My notes\n\nNever run the migration on Fridays.\n"
                   "Weird unicode: ✓ — ¡ok!\n")
        mem.write_text(curated + "\n" + mem.read_text())

        _run(repo, cfg, "second draft")
        after = mem.read_text()
        assert after.startswith(curated), "curated bytes were altered"
        assert "second draft" in after and "first draft" not in after

    def test_reinit_replaces_in_place_not_appends(self, repo, cfg):
        _run(repo, cfg, "one")
        _run(repo, cfg, "two")
        text = (repo / ".luxe" / "memory.md").read_text()
        assert text.count("<!-- luxe:brief begin") == 1
        assert text.count("<!-- luxe:brief end -->") == 1

    def test_trailing_curated_text_below_the_block_survives(self, repo, cfg):
        _run(repo, cfg, "one")
        mem = repo / ".luxe" / "memory.md"
        mem.write_text(mem.read_text() + "\n## Appendix\nkeep me\n")
        _run(repo, cfg, "two")
        assert "## Appendix\nkeep me" in mem.read_text()

    def test_never_touches_facts_jsonl(self, repo, cfg):
        project_mem.add_fact(repo, "an auto fact")
        before = (project_mem.project_store_dir(repo) / "facts.jsonl").read_bytes()
        _run(repo, cfg, "brief")
        after = (project_mem.project_store_dir(repo) / "facts.jsonl").read_bytes()
        assert before == after

    def test_dry_run_writes_nothing(self, repo, cfg):
        res = _run(repo, cfg, "draft", dry_run=True)
        assert res.ok and res.written is None
        assert not (repo / ".luxe" / "memory.md").exists()


class TestCap:
    def test_cap_is_enforced_in_python(self, repo, cfg):
        res = _run(repo, cfg, "x" * 9000)
        assert res.truncated
        assert len(res.text) <= brief_mod.MAX_BRIEF_CHARS
        assert "truncated" in res.text

    def test_short_briefs_are_untouched(self):
        body, truncated = brief_mod.cap_brief("## Short\nfine")
        assert body == "## Short\nfine" and not truncated

    def test_cap_prefers_a_line_boundary(self):
        text = "\n".join("line %d" % i for i in range(500))
        body, truncated = brief_mod.cap_brief(text, limit=200)
        assert truncated and len(body) <= 200


class TestGuards:
    def test_no_project_is_refused_with_a_fix(self, tmp_path, cfg):
        bare = tmp_path / "nothing"
        bare.mkdir()
        res = brief_mod.run_init(bare, cfg, console=_console(),
                                 run_single_fn=lambda *a, **k: _Result("x"))
        assert not res.ok and "no project" in res.error
        assert "pyproject.toml" in res.error

    def test_empty_model_output_is_an_error_not_an_empty_block(self, repo, cfg):
        res = _run(repo, cfg, "   ")
        assert not res.ok and "no brief" in res.error
        assert not (repo / ".luxe" / "memory.md").exists()

    def test_context_never_includes_claude_md(self, repo, cfg):
        """memory.sdd discipline extends to every new context builder."""
        (repo / "CLAUDE.md").write_text("SECRET-CLAUDE-INSTRUCTIONS")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "c"], cwd=repo, check=True)
        res = _run(repo, cfg, "brief")
        assert "SECRET-CLAUDE-INSTRUCTIONS" not in res._seen["extra_context"]

    def test_the_pass_is_read_only_and_grounded(self, repo, cfg):
        res = _run(repo, cfg, "brief")
        goal = res._seen["goal"]
        assert "GIT_BRIEF" not in goal          # the text, not the symbol name
        assert "project brief" in goal.lower()
        assert "do not write" in goal.lower()
        assert "<repo_map>" in res._seen["extra_context"]


class TestMemoryIntegration:
    def test_brief_reaches_the_project_memory_block(self, repo, cfg):
        _run(repo, cfg, "## What this is\nthe orientation")
        mem = project_mem.load_memory(repo)
        block = project_mem.render_block(mem)
        assert "the orientation" in block
        assert block.startswith("<project_memory>")

    def test_curated_text_sorts_ahead_of_the_brief_in_the_render(self, repo, cfg):
        _run(repo, cfg, "the brief body")
        mem_file = repo / ".luxe" / "memory.md"
        mem_file.write_text("CURATED FIRST\n\n" + mem_file.read_text())
        block = project_mem.render_block(project_mem.load_memory(repo))
        assert block.index("CURATED FIRST") < block.index("the brief body")


class TestSpliceePrimitive:
    def test_appends_at_end_of_file_when_absent(self, tmp_path):
        (tmp_path / ".luxe").mkdir()
        (tmp_path / ".luxe" / "memory.md").write_text("user line\n")
        project_mem.splice_block(tmp_path, "brief", "body")
        text = (tmp_path / ".luxe" / "memory.md").read_text()
        assert text.startswith("user line\n")
        assert text.index("user line") < text.index("<!-- luxe:brief begin")

    def test_creates_the_file_with_a_header_when_absent(self, tmp_path):
        p = project_mem.splice_block(tmp_path, "brief", "body")
        text = p.read_text()
        assert "luxe project memory" in text and "body" in text

    def test_two_blocks_coexist_independently(self, tmp_path):
        project_mem.splice_block(tmp_path, "brief", "BRIEF-A")
        project_mem.splice_block(tmp_path, "notes", "NOTES-A")
        project_mem.splice_block(tmp_path, "brief", "BRIEF-B")
        text = project_mem.repo_memory_file(tmp_path).read_text()
        assert "NOTES-A" in text and "BRIEF-B" in text and "BRIEF-A" not in text
        assert project_mem.read_block(text, "notes") == "NOTES-A"

    def test_read_block_returns_none_when_absent(self):
        assert project_mem.read_block("nothing here", "brief") is None


class TestPreambleRecovery:
    """The champion narrates before it complies (CLAUDE.md's load-bearing
    finding). Recover deterministically instead of prompting harder."""

    def test_leading_monologue_is_sliced_off(self):
        raw = ("Now I have enough information to write the brief. "
               "Let me compile it.\n\n## What this is\nA thing.\n")
        assert brief_mod.strip_preamble(raw).startswith("## What this is")

    def test_a_bogus_title_above_the_sections_is_dropped(self):
        raw = "# PROJECT BRIEF\n\n## Stack\nPython\n"
        assert brief_mod.strip_preamble(raw).startswith("## Stack")

    def test_unheaded_text_is_left_alone(self):
        assert brief_mod.strip_preamble("just prose") == "just prose"

    def test_falls_back_to_the_first_heading_when_no_section_matches(self):
        raw = "chatter\n\n## Something Else\nbody\n"
        assert brief_mod.strip_preamble(raw).startswith("## Something Else")

    def test_run_init_applies_it(self, repo, cfg):
        res = _run(repo, cfg, "Let me compile it.\n\n## Layout\nsrc/ owns it\n")
        assert res.text.startswith("## Layout")
