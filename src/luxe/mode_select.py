"""Deterministic mode selection — single vs swarm.

The CLI must pick a mode BEFORE the architect runs (the architect is a swarm
stage), so we cannot look at decomposed subtasks to decide. Instead we use:

  1. Goal-keyword pre-classifier (deterministic substring match).
  2. Source-byte fallback if no keyword matches. NOT file count — one 3000-line
     file in a 49-file repo can blow the single-model context window.

Configurable via configs/mode.yaml without code changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


class RunMode(str, Enum):
    SINGLE = "single"
    SWARM = "swarm"
    # Micro: PipelineOrchestrator with execution_mode="microloop". Not picked
    # by `select_mode()` automatically — only via explicit `--mode micro`,
    # primarily for the benchmark comparison harness.
    MICRO = "micro"
    # Phased: high-quality two-tier orchestration where a 32B Instruct
    # architect plans + reviews and a 14B Coder executes atomic tasks. The
    # architect explicitly checkpoints the work between phases; if a task
    # exhausts its retry budget the run gracefully aborts with a report
    # rather than ship broken or hallucinated code. Quality > speed.
    PHASED = "phased"


@dataclass
class ModeConfig:
    swarm_keywords: list[str] = field(default_factory=list)
    single_keywords: list[str] = field(default_factory=list)
    byte_threshold: int = 500_000
    source_extensions: list[str] = field(default_factory=list)
    exclude_dirs: list[str] = field(default_factory=list)


def load_mode_config(path: str | Path | None = None) -> ModeConfig:
    if path is None:
        path = Path(__file__).parent.parent.parent / "configs" / "mode.yaml"
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    return ModeConfig(
        swarm_keywords=[k.lower() for k in raw.get("swarm_keywords", [])],
        single_keywords=[k.lower() for k in raw.get("single_keywords", [])],
        byte_threshold=int(raw.get("byte_threshold", 500_000)),
        source_extensions=[e.lower() for e in raw.get("source_extensions", [])],
        exclude_dirs=list(raw.get("exclude_dirs", [])),
    )


@dataclass
class ModeDecision:
    mode: RunMode
    reason: str
    keyword: str | None = None
    source_bytes: int | None = None


def sum_source_bytes(repo_root: Path, cfg: ModeConfig) -> int:
    """Sum the bytes of source files in repo_root, excluding generated dirs."""
    extensions = {e if e.startswith(".") else f".{e}" for e in cfg.source_extensions}
    excludes = set(cfg.exclude_dirs)
    total = 0
    for root, dirs, files in __import__("os").walk(repo_root):
        dirs[:] = [d for d in dirs if d not in excludes and not d.startswith(".")
                   or d in {".github"}]  # keep .github but skip .git/.venv/etc
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in extensions:
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return total


def select_mode(
    goal: str,
    repo_root: Path | str,
    override: str | None = None,
    cfg: ModeConfig | None = None,
) -> ModeDecision:
    """Pick a run mode using the deterministic algorithm in §2 of the plan.

    Returns a ModeDecision with the chosen mode and the reasoning so the CLI
    can surface why it picked what it picked.
    """
    if override and override != "auto":
        return ModeDecision(
            mode=RunMode(override),
            reason=f"explicit --mode {override}",
        )

    if cfg is None:
        cfg = load_mode_config()

    g = goal.lower()
    for kw in cfg.swarm_keywords:
        if kw in g:
            return ModeDecision(mode=RunMode.SWARM, reason="swarm-keyword in goal", keyword=kw)
    for kw in cfg.single_keywords:
        if kw in g:
            return ModeDecision(mode=RunMode.SINGLE, reason="single-keyword in goal", keyword=kw)

    src_bytes = sum_source_bytes(Path(repo_root), cfg)
    if src_bytes > cfg.byte_threshold:
        return ModeDecision(
            mode=RunMode.SWARM,
            reason=f"source size {src_bytes} > threshold {cfg.byte_threshold}",
            source_bytes=src_bytes,
        )
    return ModeDecision(
        mode=RunMode.SINGLE,
        reason=f"source size {src_bytes} ≤ threshold {cfg.byte_threshold}",
        source_bytes=src_bytes,
    )
