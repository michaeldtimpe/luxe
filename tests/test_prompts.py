"""Tests for the mono-mode prompt registry.

These tests guard the editing norm declared in `prompts.py`:
all mono prompt edits must go through the registry, NOT through scattered
literals in `single.py` or anywhere else. The duplication regression test
catches the most likely violation — someone copy-pasting the baseline
system prompt into `single.py` while editing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from luxe.agents.prompts import PROMPT_REGISTRY, PromptVariant, get


_BASELINE_OPENING = "You are a code maintenance specialist working on a single repository."


def test_baseline_entry_exists_and_has_expected_opening():
    """Sanity check that the baseline cell of the bake-off is what we
    think it is."""
    bv = get("baseline")
    assert isinstance(bv, PromptVariant)
    assert bv.system.startswith(_BASELINE_OPENING)
    assert bv.task_prefix.startswith("Begin by reading")


def test_all_documented_variants_are_registered():
    """Every variant cited in jiggly-baking-kahan.md §1 must be present.
    A typo in a variant id silently falls back to baseline if `get()`
    weren't strict — make sure it's strict and named consistently."""
    expected = {"baseline", "cot", "sot", "hads_persona", "combined"}
    assert expected <= set(PROMPT_REGISTRY)


def test_get_raises_keyerror_with_available_list_on_miss():
    with pytest.raises(KeyError) as excinfo:
        get("does_not_exist")
    msg = str(excinfo.value)
    assert "does_not_exist" in msg
    assert "available" in msg
    # Available list should include at least baseline so authors can recover.
    assert "baseline" in msg


def test_cot_task_prefix_includes_plan_directive():
    """CoT cell's value comes from the <plan> directive — verify it's there."""
    cot = get("cot")
    assert "<plan>" in cot.task_prefix
    assert "</plan>" in cot.task_prefix


def test_sot_system_includes_skeleton_first():
    sot = get("sot")
    assert "Skeleton first" in sot.system
    assert "signature" in sot.system.lower()


def test_hads_persona_uses_xml_tags():
    """HADS variant restructures the same content with role/spec/context/
    contract tags. Failure to find them means the content was inlined."""
    hads = get("hads_persona")
    for tag in ("<role>", "</role>", "<spec>", "</spec>",
                "<context>", "</context>", "<contract>", "</contract>"):
        assert tag in hads.system, f"HADS system missing {tag!r}"


def test_combined_composes_hads_sot_cot():
    """combined = HADS persona + SoT skeleton-first appendix + CoT
    <plan> task prefix. If any of those drops out, the variant becomes
    indistinguishable from a smaller cell and the bake-off result is
    uninterpretable."""
    combined = get("combined")
    # HADS structure
    assert "<spec>" in combined.system
    # SoT skeleton-first appendix
    assert "Skeleton first" in combined.system
    # CoT plan directive
    assert "<plan>" in combined.task_prefix


def test_no_baseline_system_duplication_outside_registry():
    """Regression guard: nobody else should hold a copy of the baseline
    system prompt opening line. Catches the failure mode where a future
    editor copy-pastes the prompt into single.py instead of editing the
    registry."""
    src = Path(__file__).resolve().parent.parent / "src" / "luxe"
    duplicates: list[Path] = []
    for path in src.rglob("*.py"):
        if path.name == "prompts.py":
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        if _BASELINE_OPENING in text:
            duplicates.append(path)
    assert not duplicates, (
        f"baseline system prompt duplicated outside prompts.py in: "
        f"{[str(p.relative_to(src)) for p in duplicates]}. "
        "Edit prompts.py instead — the bake-off's baseline cell reads "
        "from the registry, not from these files."
    )


def test_all_variants_are_frozen():
    """PromptVariant is frozen; mutating an entry would silently corrupt
    cross-fixture cells if the same variant were shared. Confirm
    immutability."""
    bv = get("baseline")
    with pytest.raises(Exception):
        bv.system = "different"  # type: ignore[misc]


def test_variant_systems_are_non_empty():
    """No variant should ship an empty system prompt — that would silently
    blank the model's instructions."""
    for vid, v in PROMPT_REGISTRY.items():
        assert v.system.strip(), f"variant {vid!r} has empty system prompt"
        assert v.task_prefix.strip(), f"variant {vid!r} has empty task_prefix"
