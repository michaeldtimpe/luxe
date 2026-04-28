"""Tests for src/luxe/mode_select.py — deterministic mode selection."""

from __future__ import annotations

from pathlib import Path

from luxe.mode_select import (
    ModeConfig,
    ModeDecision,
    RunMode,
    load_mode_config,
    select_mode,
    sum_source_bytes,
)


def _cfg(**kw) -> ModeConfig:
    base = ModeConfig(
        swarm_keywords=["implement", "refactor", "audit"],
        single_keywords=["review", "summarize", "explain"],
        byte_threshold=500_000,
        source_extensions=[".py", ".ts"],
        exclude_dirs=["node_modules", ".venv"],
    )
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def test_explicit_override(tmp_path: Path):
    d = select_mode("anything", tmp_path, override="single", cfg=_cfg())
    assert d.mode == RunMode.SINGLE
    assert "explicit" in d.reason

    d = select_mode("anything", tmp_path, override="swarm", cfg=_cfg())
    assert d.mode == RunMode.SWARM


def test_swarm_keyword_in_goal(tmp_path: Path):
    d = select_mode("Implement OAuth login flow", tmp_path, cfg=_cfg())
    assert d.mode == RunMode.SWARM
    assert d.keyword == "implement"


def test_single_keyword_in_goal(tmp_path: Path):
    d = select_mode("Review the auth module for security bugs", tmp_path, cfg=_cfg())
    assert d.mode == RunMode.SINGLE
    assert d.keyword == "review"


def test_keyword_match_is_case_insensitive(tmp_path: Path):
    d = select_mode("REFACTOR the storage layer", tmp_path, cfg=_cfg())
    assert d.mode == RunMode.SWARM


def test_swarm_keyword_wins_when_both_present(tmp_path: Path):
    # "review" is single, "implement" is swarm — swarm checked first
    d = select_mode("Review the existing code and implement a fix", tmp_path, cfg=_cfg())
    assert d.mode == RunMode.SWARM
    assert d.keyword == "implement"


def test_byte_threshold_swarm(tmp_path: Path):
    # 600 KB of source in fewer than 50 files → SWARM (not SINGLE per file count)
    big = tmp_path / "src" / "huge.py"
    big.parent.mkdir()
    big.write_text("x = 1\n" * 100_000)  # ~600 KB
    d = select_mode("update something", tmp_path, cfg=_cfg())
    assert d.mode == RunMode.SWARM
    assert d.source_bytes is not None
    assert d.source_bytes > 500_000


def test_byte_threshold_single(tmp_path: Path):
    # Many files but small total bytes → SINGLE
    for i in range(60):
        (tmp_path / f"f{i}.py").write_text("x = 1\n")
    d = select_mode("update something", tmp_path, cfg=_cfg())
    assert d.mode == RunMode.SINGLE
    assert d.source_bytes is not None
    assert d.source_bytes < 500_000


def test_byte_threshold_excludes_node_modules(tmp_path: Path):
    # 600 KB inside node_modules should NOT count
    nm = tmp_path / "node_modules" / "lib"
    nm.mkdir(parents=True)
    (nm / "huge.ts").write_text("export const x = 1;\n" * 30_000)
    (tmp_path / "small.ts").write_text("export const y = 2;\n")
    d = select_mode("update something", tmp_path, cfg=_cfg())
    assert d.mode == RunMode.SINGLE


def test_byte_threshold_only_counts_source_extensions(tmp_path: Path):
    # 600 KB of .json should NOT count
    (tmp_path / "huge.json").write_text("a" * 600_000)
    d = select_mode("update something", tmp_path, cfg=_cfg())
    assert d.mode == RunMode.SINGLE


def test_load_mode_config_default():
    cfg = load_mode_config()
    assert cfg.byte_threshold == 500_000
    assert "implement" in cfg.swarm_keywords
    assert "review" in cfg.single_keywords
    assert ".py" in cfg.source_extensions
    assert "node_modules" in cfg.exclude_dirs


def test_sum_source_bytes_basic(tmp_path: Path):
    (tmp_path / "a.py").write_text("a" * 100)
    (tmp_path / "b.ts").write_text("b" * 200)
    cfg = _cfg()
    assert sum_source_bytes(tmp_path, cfg) == 300
