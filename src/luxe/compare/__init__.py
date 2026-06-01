"""Side-by-side single-task comparison for the chat front-end.

Give one task, get two outputs to compare — three modes:
  1. luxe-enhanced champion vs bare champion (substrate ablation)
  2. two config/prompt variants of the champion
  3. champion vs a second MLX model

Reuses the benchmark `Variant` + `make_overlay` machinery. Sides run
SEQUENTIALLY (two weight-sets can't coexist in oMLX). See compare.sdd.
"""

from __future__ import annotations

__all__ = [
    "CompareSide",
    "CompareResult",
    "SideResult",
    "run_compare",
    "build_sides",
    "interactive_compare",
]

from luxe.compare.run_pair import (
    CompareResult,
    CompareSide,
    SideResult,
    build_sides,
    interactive_compare,
    run_compare,
)
