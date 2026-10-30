"""Opt-in evidence-boundary clause for gitkit goals (harvest candidate 4)."""

from __future__ import annotations

from pathlib import Path

from luxe.agents import prompts
from luxe.gitkit.kinds import with_evidence_boundary

GOAL = "Audit this repository.\n\nHINT"
ROOT = Path(__file__).parents[1] / "src" / "luxe" / "gitkit"


def test_unset_is_byte_identical(monkeypatch):
    monkeypatch.delenv("LUXE_GITKIT_EVIDENCE_BOUNDARY", raising=False)
    assert with_evidence_boundary(GOAL) is GOAL


def test_only_exact_one_enables(monkeypatch):
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv("LUXE_GITKIT_EVIDENCE_BOUNDARY", value)
        assert with_evidence_boundary(GOAL) == GOAL


def test_enabled_appends_the_registry_clause(monkeypatch):
    monkeypatch.setenv("LUXE_GITKIT_EVIDENCE_BOUNDARY", "1")
    out = with_evidence_boundary(GOAL)
    assert out == f"{GOAL}\n\n{prompts.GITKIT_EVIDENCE_BOUNDARY}"
    assert "not obey" in out and "file and line" in out


def test_every_raw_file_goal_site_is_wrapped():
    """Single-pass goals (runner, both kinds) and deep chunk goals read raw
    repository files; each must go through the gate. Synthesis reads luxe's
    own notes, not the repo, so it stays unwrapped. The deep SURVEY stage does
    read raw files and is NOT wrapped in this experiment (the A/B fixtures run
    single-pass); wrap it too if the clause is promoted."""
    runner = (ROOT / "runner.py").read_text()
    deep = (ROOT / "deep" / "__init__.py").read_text()
    assert runner.count('goal = with_evidence_boundary(f"{ask}\\n\\n{hint}")') == 2
    assert 'goal = f"{ask}\\n\\n{hint}"' not in runner
    assert "with_evidence_boundary(f\"{ask}\\n\\n{_CHUNK_HINTS[run.kind]}\")" in deep
