"""loop.py must RE-EXPORT guardrails' constants, never redefine them.

The 2026-05-26 guardrail extraction (commit 4581d38) copied loop.py's
intervention constants into `agents/guardrails.py` instead of moving them, so
two byte-identical sets coexisted: the guards read guardrails' copy, the loop
tests imported loop.py's. The 2026-08-04 consolidation deleted loop.py's
copies and imports the names instead.

These tests pin that state. `is` (not `==`) is the assertion that matters — an
equal-but-distinct object means a third copy has crept back in and the two can
silently drift again.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from luxe.agents import guardrails, loop

# Every intervention threshold / nudge body loop.py re-exports.
RE_EXPORTED = [
    "_ACTION_DENSITY_GATE_MAX_TOOLS",
    "_ACTION_DENSITY_GATE_MESSAGE",
    "_ACTION_DENSITY_GATE_MIN_STEP",
    "_ACTION_DENSITY_GATE_MIN_TOKENS",
    "_ACTION_DENSITY_GATE_MIN_TURNS_AFTER_BAIL",
    "_BREADTH_PROBE_ESCALATION_COUNT",
    "_CONVERGENCE_HIGH_THRESHOLD",
    "_CONVERGENCE_LOW_THRESHOLD",
    "_EARLY_BAIL_MESSAGE",
    "_EARLY_BAIL_MESSAGE_BREADTH_PROBE",
    "_EARLY_BAIL_MESSAGE_COMMIT_IMPERATIVE",
    "_EARLY_BAIL_MESSAGE_MODES",
    "_EARLY_BAIL_MESSAGE_NO_ABSTAIN",
    "_EARLY_BAIL_MESSAGE_SOFT_ANCHOR",
    "_EARLY_BAIL_MIN_READS",
    "_EARLY_BAIL_MIN_STEP",
    "_HABITUATION_EXIT_MIN_KINDS",
    "_HABITUATION_EXIT_MIN_STEP",
    "_MAX_CONSECUTIVE_REPEAT_STEPS",
    "_POST_WRITE_IDLE_MAX",
    "_PROSE_BURST_MAX_STEP",
    "_PROSE_BURST_MESSAGE",
    "_PROSE_BURST_MIN_DELTA",
    "_WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE",
    "_WRITE_PRESSURE_MESSAGE",
    "_WRITE_PRESSURE_MIN_STEP",
    "_WRITE_PRESSURE_MIN_TOKENS",
    "_WRITE_PRESSURE_MIN_TOOLS",
    "_v1105_synthesis_looping_signature",
]


@pytest.mark.parametrize("name", RE_EXPORTED)
def test_loop_reexports_guardrails_object(name: str) -> None:
    """loop.X is guardrails.X — the same object, not a copy."""
    assert hasattr(loop, name), f"loop.py no longer exposes {name}"
    assert hasattr(guardrails, name), f"guardrails.py no longer defines {name}"
    assert getattr(loop, name) is getattr(guardrails, name), (
        f"{name} is a SECOND copy in loop.py — re-export it from guardrails "
        "instead, or the two will drift."
    )


def _module_level_assignments(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def test_loop_defines_none_of_them() -> None:
    """A re-export must not be shadowed by a local definition in loop.py."""
    loop_path = pathlib.Path(loop.__file__)
    defined = _module_level_assignments(loop_path)
    redefined = sorted(defined & set(RE_EXPORTED))
    assert not redefined, (
        f"loop.py redefines {redefined} at module level — guardrails.py is the "
        "sole home for intervention thresholds and nudge bodies."
    )


def test_guardrails_defines_all_of_them() -> None:
    """guardrails.py is the sole home, so it must actually define them."""
    defined = _module_level_assignments(pathlib.Path(guardrails.__file__))
    missing = sorted(set(RE_EXPORTED) - defined)
    assert not missing, f"guardrails.py no longer defines {missing}"
