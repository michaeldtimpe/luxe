"""Composable retrieval+compression pipelines for the compression benchmark.

A strategy is a JSON-serializable object with a `stages` list. Each stage
names a function in the registry and optional `params`. The runner walks
stages in order, skipping disabled ones, mutating a shared `Context`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from strategies.stages import Context, run_pipeline

__all__ = ["Context", "load_strategy", "run_pipeline"]


CONFIGS_DIR = Path(__file__).resolve().parent / "configs"


def load_strategy(name_or_path: str) -> dict[str, Any]:
    """Resolve a strategy by bare name (looks up configs/<name>.json) or
    explicit path."""
    p = Path(name_or_path)
    if not p.exists() and not name_or_path.endswith(".json"):
        p = CONFIGS_DIR / f"{name_or_path}.json"
    with p.open() as f:
        return json.load(f)
