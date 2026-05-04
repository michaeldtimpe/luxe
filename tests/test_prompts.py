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


def test_cot_task_prefix_uses_markdown_not_xml():
    """CoT v2 uses a `## Plan` markdown header instead of `<plan>` XML tags.
    Smoke probe (2026-04-30) showed Qwen3 collided XML tags with its native
    tool-call format and emitted `</parameter></function></tool_call>` in
    place of `</plan>`, dropping tool_calls_total to zero. Regression guard:
    no XML plan tags in the directive."""
    cot = get("cot")
    assert "## Plan" in cot.task_prefix
    # The XML form must NOT come back — it's the failure mode we fixed.
    assert "<plan>" not in cot.task_prefix
    assert "</plan>" not in cot.task_prefix


def test_cot_task_prefix_marks_plan_as_scaffolding():
    """The plan-as-deliverable trap was the second CoT v1 failure: the
    model treated the plan AS the response and stopped. v2 explicitly
    frames the plan as scaffolding and adds a prose cap."""
    cot = get("cot")
    pf = cot.task_prefix
    assert "scaffolding" in pf or "NOT the deliverable" in pf
    # Anti-deliberation guard.
    assert "200 words" in pf or "tool call" in pf


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
    """combined = HADS persona + SoT skeleton-first appendix + CoT plan
    directive (v2 markdown form). If any drops out, the variant becomes
    indistinguishable from a smaller cell and the bake-off result is
    uninterpretable."""
    combined = get("combined")
    # HADS structure
    assert "<spec>" in combined.system
    # SoT skeleton-first appendix
    assert "Skeleton first" in combined.system
    # CoT v2 plan directive — markdown header, NOT XML
    assert "## Plan" in combined.task_prefix
    assert "<plan>" not in combined.task_prefix


def test_hads_spec_orders_actions_before_prose():
    """HADS v2 reorders the <spec> as strict FIRST/THEN/ONLY-AFTER steps
    so the model can't deliberate before its first tool call. Smoke probe
    (2026-04-30) showed v1 spent 471s churning self-talk without ever
    calling a tool; this test guards the FIRST/THEN structure."""
    hads = get("hads_persona")
    spec = hads.system
    assert "FIRST" in spec
    assert "THEN" in spec
    assert "ONLY AFTER" in spec
    # The key anti-deliberation line.
    assert "BEFORE producing" in spec or "before producing" in spec.lower()


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


# --- task-type overlays (Branch B) --

def test_get_overlay_returns_none_for_empty_string():
    """Empty string is the no-overlay sentinel that RoleConfig defaults
    to; must not raise."""
    from luxe.agents.prompts import get_overlay
    assert get_overlay("") is None


def test_get_overlay_returns_none_for_unknown_id():
    """Unknown overlay ids return None rather than raising — overlays
    are opt-in (unlike PromptVariants which surface typos via KeyError).
    A missing overlay just falls through to role defaults."""
    from luxe.agents.prompts import get_overlay
    assert get_overlay("does_not_exist") is None


def test_implement_via_cot_overlay_is_registered():
    """The Branch B sweep variant references implement_via_cot — must
    exist in TASK_OVERLAYS or the bench cell can't load."""
    from luxe.agents.prompts import TASK_OVERLAYS, get_overlay
    assert "implement_via_cot" in TASK_OVERLAYS
    overlay = get_overlay("implement_via_cot")
    assert overlay is not None
    assert overlay.by_task["implement"] == "cot"
    assert overlay.by_task["bugfix"] == "cot"


def test_document_strict_variant_is_registered():
    """Phase v1.1 B1: document_strict variant must exist for the
    document_strict_only overlay to resolve. Has baseline system
    (no persona change) + a stricter task_prefix demanding tool calls
    and component completeness."""
    from luxe.agents.prompts import PROMPT_REGISTRY, get
    assert "document_strict" in PROMPT_REGISTRY
    v = get("document_strict")
    # Baseline-system parity — overlay should not change the agent's persona,
    # only its task-completion expectations.
    assert v.system == PROMPT_REGISTRY["baseline"].system
    # Task prefix must contain the "MUST call edit_file/write_file" directive
    # (the load-bearing piece — this targets the lpe-typing under-engagement
    # pattern where the model adds 1 line of imports and stops).
    assert "edit_file" in v.task_prefix
    assert "write_file" in v.task_prefix
    # Task prefix must mention component-completeness — the second piece of
    # the gating intent (don't just satisfy ONE half of a multi-deliverable goal).
    assert "EVERY component" in v.task_prefix or "every component" in v.task_prefix


def test_document_strict_only_overlay_is_registered():
    """The doc-task overlay variant references document_strict_only —
    must exist in TASK_OVERLAYS. Maps document → document_strict only;
    other task types fall through to role-level defaults (no leakage of
    the strict directive into implement/manage/review tasks)."""
    from luxe.agents.prompts import TASK_OVERLAYS, get_overlay
    assert "document_strict_only" in TASK_OVERLAYS
    overlay = get_overlay("document_strict_only")
    assert overlay is not None
    assert overlay.by_task == {"document": "document_strict"}
    # Crucially: implement, bugfix, manage, review are NOT in by_task —
    # the overlay must miss for them and fall through to role default.
    assert "implement" not in overlay.by_task
    assert "manage" not in overlay.by_task


def test_document_strict_only_overlay_fires_only_on_document_tasks():
    """Resolve sanity check: with document_strict_only active, document
    routes to document_strict; every other task type routes to the
    role-level defaults. Defends against accidental leakage of the
    strict directive (which would regress Phase 1's finding that
    structural prompts hurt non-doc tasks)."""
    from luxe.agents.prompts import resolve_prompt_ids
    # Document → overlay fires.
    sys_id, task_id = resolve_prompt_ids(
        "document",
        system_prompt_id="baseline",
        task_prompt_id="baseline",
        task_overlay_id="document_strict_only",
    )
    assert sys_id == "document_strict"
    assert task_id == "document_strict"
    # Implement, bugfix, manage, review → fall through to role default.
    for task_type in ("implement", "bugfix", "manage", "review"):
        sys_id, task_id = resolve_prompt_ids(
            task_type,
            system_prompt_id="baseline",
            task_prompt_id="baseline",
            task_overlay_id="document_strict_only",
        )
        assert sys_id == "baseline", f"{task_type} leaked to overlay"
        assert task_id == "baseline", f"{task_type} leaked to overlay"


def test_manage_strict_variant_is_registered():
    """Phase v1.1 B2: manage_strict variant must exist for the
    manage_strict_only overlay to resolve. Targets the nothing-ever-
    happens-manage-deps-audit stuck-loop pattern (model reads
    requirements.txt repeatedly, hits the 2-repeat loop detector,
    no diff produced). Has baseline system + a stricter task_prefix
    naming the two failure modes (re-reading + reading-without-writing)."""
    from luxe.agents.prompts import PROMPT_REGISTRY, get
    assert "manage_strict" in PROMPT_REGISTRY
    v = get("manage_strict")
    # Baseline-system parity — overlay shouldn't change agent persona.
    assert v.system == PROMPT_REGISTRY["baseline"].system
    # Task prefix must call out the two failure modes by name.
    assert "loop detector" in v.task_prefix or "loop" in v.task_prefix.lower()
    assert "edit_file" in v.task_prefix
    assert "write_file" in v.task_prefix
    # Approach guidance — enumerate one-at-a-time.
    assert "ONE AT A TIME" in v.task_prefix


def test_manage_strict_only_overlay_is_registered():
    """The manage-task overlay variant references manage_strict_only —
    must exist in TASK_OVERLAYS. Maps manage → manage_strict only;
    other task types fall through to role-level defaults (no leakage
    into doc/implement/etc.)."""
    from luxe.agents.prompts import TASK_OVERLAYS, get_overlay
    assert "manage_strict_only" in TASK_OVERLAYS
    overlay = get_overlay("manage_strict_only")
    assert overlay is not None
    assert overlay.by_task == {"manage": "manage_strict"}
    # Crucially: implement, document, bugfix, review NOT in by_task —
    # the strict directive must not leak to other task types.
    assert "implement" not in overlay.by_task
    assert "document" not in overlay.by_task


def test_manage_strict_only_overlay_fires_only_on_manage_tasks():
    """Resolve sanity check: with manage_strict_only active, manage
    routes to manage_strict; every other task type routes to role-
    level defaults."""
    from luxe.agents.prompts import resolve_prompt_ids
    # Manage → overlay fires.
    sys_id, task_id = resolve_prompt_ids(
        "manage",
        system_prompt_id="baseline",
        task_prompt_id="baseline",
        task_overlay_id="manage_strict_only",
    )
    assert sys_id == "manage_strict"
    assert task_id == "manage_strict"
    # Implement, bugfix, document, review → fall through to role default.
    for task_type in ("implement", "bugfix", "document", "review"):
        sys_id, task_id = resolve_prompt_ids(
            task_type,
            system_prompt_id="baseline",
            task_prompt_id="baseline",
            task_overlay_id="manage_strict_only",
        )
        assert sys_id == "baseline", f"{task_type} leaked to overlay"
        assert task_id == "baseline", f"{task_type} leaked to overlay"


def test_swebench_strict_only_overlay_is_registered():
    """The swebench-task overlay variant references swebench_strict_only —
    must exist in TASK_OVERLAYS. Maps bugfix → swebench_bugfix only;
    other task types fall through. Activated via configs/single_64gb_
    swebench.yaml; the default config is unaffected."""
    from luxe.agents.prompts import PROMPT_REGISTRY, TASK_OVERLAYS, get_overlay
    assert "swebench_strict_only" in TASK_OVERLAYS
    overlay = get_overlay("swebench_strict_only")
    assert overlay is not None
    assert overlay.by_task == {"bugfix": "swebench_bugfix"}
    # Variant must be registered or resolve_prompt_ids will return an id
    # that get_prompt() then KeyErrors on.
    assert "swebench_bugfix" in PROMPT_REGISTRY
    # Crucially: implement, document, manage, review NOT in by_task —
    # the SWE-bench directive must not leak to other task types or it
    # contaminates the maintain_suite if someone accidentally points it
    # at this config.
    for other in ("implement", "document", "manage", "review", "summarize"):
        assert other not in overlay.by_task


def test_swebench_strict_only_overlay_fires_only_on_bugfix_tasks():
    """Resolve sanity check: with swebench_strict_only active, bugfix
    routes to swebench_bugfix; every other task type routes to role-
    level defaults."""
    from luxe.agents.prompts import resolve_prompt_ids
    sys_id, task_id = resolve_prompt_ids(
        "bugfix",
        system_prompt_id="baseline",
        task_prompt_id="baseline",
        task_overlay_id="swebench_strict_only",
    )
    assert sys_id == "swebench_bugfix"
    assert task_id == "swebench_bugfix"
    for task_type in ("implement", "document", "manage", "review", "summarize"):
        sys_id, task_id = resolve_prompt_ids(
            task_type,
            system_prompt_id="baseline",
            task_prompt_id="baseline",
            task_overlay_id="swebench_strict_only",
        )
        assert sys_id == "baseline", f"{task_type} leaked to overlay"
        assert task_id == "baseline", f"{task_type} leaked to overlay"


def test_swebench_bugfix_variant_contains_anti_reproducer_directives():
    """Surface-level guard against accidental edits that drop the key
    directives. The smoke run we're defending against was specifically
    'model creates reproducer scripts'; the prompt MUST forbid new
    files and MUST require single-tool-per-response (parallel-cliff
    defense). Doesn't lock exact wording, just key tokens."""
    from luxe.agents.prompts import get
    variant = get("swebench_bugfix")
    prefix = variant.task_prefix
    # Anti-new-files (anti-reproducer):
    assert "Do NOT create any new files" in prefix
    # Parallel-call-cliff defense:
    assert "ONE tool per response" in prefix
    # Test-edit guard:
    assert "Do NOT modify or add\ntests" in prefix or "Do NOT modify or add tests" in prefix
    # Tool-call hint biases toward search:
    assert "grep or find_symbol" in prefix


def test_swebench_strict_counterexample_only_overlay_is_registered():
    """The counterexample-heuristic A/B variant: bugfix → swebench_
    bugfix_counterexample. Used by configs/single_64gb_swebench_
    counterexample.yaml; default swebench config remains on the base
    variant."""
    from luxe.agents.prompts import PROMPT_REGISTRY, TASK_OVERLAYS, get_overlay
    assert "swebench_strict_counterexample_only" in TASK_OVERLAYS
    overlay = get_overlay("swebench_strict_counterexample_only")
    assert overlay is not None
    assert overlay.by_task == {"bugfix": "swebench_bugfix_counterexample"}
    assert "swebench_bugfix_counterexample" in PROMPT_REGISTRY
    for other in ("implement", "document", "manage", "review", "summarize"):
        assert other not in overlay.by_task


def test_swebench_bugfix_counterexample_extends_base_with_falsification_clause():
    """The +heuristic variant must contain everything the base variant
    does (anti-reproducer, single-tool, linear protocol) plus the
    falsification clause. Catches the silent-no-op case where the base
    prompt's structure changes and the .replace() call no-ops."""
    from luxe.agents.prompts import get
    base = get("swebench_bugfix").task_prefix
    var = get("swebench_bugfix_counterexample").task_prefix
    # Variant strictly extends base — every base directive still present:
    assert "Do NOT create any new files" in var
    assert "ONE tool per response" in var
    assert "Linear protocol (single pass):" in var
    assert "grep or find_symbol" in var
    # Falsification clause itself:
    assert "yields the expected result" in var
    assert "construct the more complex" in var.lower() or "Construct the more complex" in var
    # Variant is strictly longer:
    assert len(var) > len(base)


def test_resolve_prompt_ids_no_overlay_returns_role_defaults():
    """When no overlay is set, role-level system_prompt_id /
    task_prompt_id win regardless of task_type."""
    from luxe.agents.prompts import resolve_prompt_ids
    sys_id, task_id = resolve_prompt_ids(
        "implement",
        system_prompt_id="baseline",
        task_prompt_id="baseline",
        task_overlay_id="",
    )
    assert sys_id == "baseline"
    assert task_id == "baseline"


def test_resolve_prompt_ids_overlay_hits_for_matching_task_type():
    """implement_via_cot maps `implement` → cot. With that overlay
    active, an implement task picks up cot for both system and task."""
    from luxe.agents.prompts import resolve_prompt_ids
    sys_id, task_id = resolve_prompt_ids(
        "implement",
        system_prompt_id="baseline",
        task_prompt_id="baseline",
        task_overlay_id="implement_via_cot",
    )
    assert sys_id == "cot"
    assert task_id == "cot"


def test_resolve_prompt_ids_overlay_misses_falls_back_to_role():
    """implement_via_cot has no `document` entry — a document task with
    that overlay active must fall through to role-level defaults."""
    from luxe.agents.prompts import resolve_prompt_ids
    sys_id, task_id = resolve_prompt_ids(
        "document",
        system_prompt_id="baseline",
        task_prompt_id="baseline",
        task_overlay_id="implement_via_cot",
    )
    assert sys_id == "baseline"
    assert task_id == "baseline"


def test_resolve_prompt_ids_unknown_overlay_acts_as_no_overlay():
    """Typo'd overlay id resolves to None and falls through to role
    defaults — surfaces as the role's prompts being used (no error)."""
    from luxe.agents.prompts import resolve_prompt_ids
    sys_id, task_id = resolve_prompt_ids(
        "implement",
        system_prompt_id="sot",  # role-level non-default
        task_prompt_id="baseline",
        task_overlay_id="typo_does_not_exist",
    )
    # Unknown overlay → no override → role defaults win.
    assert sys_id == "sot"
    assert task_id == "baseline"


def test_task_overlay_is_frozen():
    """TaskOverlay must be immutable so a runtime mutation can't change
    behaviour mid-sweep."""
    from luxe.agents.prompts import TaskOverlay
    overlay = TaskOverlay(by_task={"implement": "cot"})
    with pytest.raises(Exception):
        overlay.by_task = {"implement": "sot"}  # type: ignore[misc]
