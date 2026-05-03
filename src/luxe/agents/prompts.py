"""Prompt registry — single source of truth for mono-mode prompts.

Editing norm: **all mono prompt edits must go through this registry.** Do
NOT scatter string literals in `single.py` or anywhere else; they will
silently un-couple the variant cells from the actual runtime prompt and
make the prompt-shaping bake-off uninterpretable.

The registry holds named `PromptVariant` entries. Each variant has:
  - `system`: the full system prompt sent to the model
  - `task_prefix`: text appended after the dynamic "Task type / Goal"
    header in `run_single`'s task prompt construction

`single.py` looks up the active variant via `RoleConfig.system_prompt_id`
and `RoleConfig.task_prompt_id`. The `baseline` entries are byte-equivalent
to the prior hardcoded `_SYSTEM_PROMPT` and inline task-prompt suffix in
`single.py`, so cells with default IDs reproduce current behaviour exactly.

See `~/.claude/plans/jiggly-baking-kahan.md` §1 for the variant rationale.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptVariant:
    system: str
    task_prefix: str


_BASELINE_SYSTEM = """\
You are a code maintenance specialist working on a single repository. Your job
is to take a goal end-to-end: read what's relevant, plan the change, edit code
when needed, run tests if available, and produce a final report.

Operating principles:
- Read first. Understand the repo before you edit it.
- Make minimal, focused changes — only what the goal requires.
- Cite every file you read with file:path syntax; cite every file you modify.
- Preserve existing style and conventions.
- When you finish, output a final report summarising what you changed,
  what tests you ran, and any open questions.

Citation contract:
- Every file:line citation in your final report MUST resolve in the current
  repo state. The post-synthesis citation linter will verify each one.
- If you cite a line in a file you also edited, include a 1–3 line snippet of
  the cited code verbatim alongside the citation; the linter uses fuzzy snippet
  matching to forgive line-shift after edits.
"""

_BASELINE_TASK_PREFIX = (
    "Begin by reading what's relevant to plan your change. "
    "When you're done, end with a final report."
)

# Skeleton-first directive for SoT variant — appended to baseline system.
_SOT_APPENDIX = """\

Skeleton first:
- When writing a new function, class, or module, FIRST emit the signature(s)
  plus a short docstring plus a numbered bullet list of the body's logical
  steps. ONLY THEN fill in the implementation. This applies to write_file
  on a new file and to edit_file when you are adding a new function body.
"""

# CoT plan-first directive — replaces baseline task prefix for CoT variant.
#
# v2 (2026-04-30): the original used `<plan>...</plan>` XML tags. Smoke
# probe revealed Qwen3 collided that with its tool-call format and
# emitted `</parameter></function></tool_call>` instead of `</plan>`,
# making the response unparseable. Tool calls dropped to zero, the run
# bailed in 15s with `prose_only`. v2 uses a markdown header instead and
# adds an explicit "plan is not the deliverable" framing plus a 200-word
# prose cap to break the plan-as-deliverable trap.
_COT_TASK_PREFIX = (
    "Plan-first protocol: open your response with a `## Plan` markdown "
    "section listing (a) files you intend to read, (b) edits you intend "
    "to make, (c) verification you intend to run. Then IMMEDIATELY "
    "invoke read_file or another tool — the plan is internal scaffolding, "
    "NOT the deliverable. If you write more than 200 words of prose "
    "without a tool call, stop the prose and emit your next tool call. "
    "Update the plan if your understanding changes after reading.\n\n"
) + _BASELINE_TASK_PREFIX

# HADS-style XML restructuring — same content as baseline, structured for
# Qwen3-family training to distinguish hard requirements from softer guidance.
#
# v2 (2026-04-30): smoke probe showed v1 reframed the imperative bullets as
# a "specification document" the model deliberated over (471s/47k tokens
# of "Let me implement this now. OK, let me write the code now…" loop
# without ever calling a tool). v2 keeps the XML tag structure for the
# Qwen3-alignment hypothesis but reorders the spec as strict FIRST/THEN/
# ONLY-AFTER ordering — anti-deliberation guard. The "BEFORE producing
# any prose, call read_file" line is the key fix.
_HADS_SYSTEM = """\
<role>Staff Software Engineer assigned to take a goal end-to-end on a single repository.</role>

<spec>
You MUST act in this exact order:
1. FIRST: BEFORE producing any prose, call read_file to inspect the files
   relevant to the goal. Do not deliberate before this first tool call.
2. THEN: call edit_file or write_file as needed to satisfy the goal.
   Make minimal, focused changes — only what the goal requires.
3. ONLY AFTER editing: produce a final report summarising what you
   changed, what tests you ran, and any open questions.

You MUST also:
- Cite every file you read and every file you modify with `path:line` syntax.
- Stop and report scope problems if the goal would need more than 10 file
  edits or systematic decomposition you cannot hold in one context window.
</spec>

<context>
You SHOULD:
- Preserve existing style and conventions.
- Run available tests after edits when sensible.
- Prefer the smallest diff that satisfies the goal.
</context>

<contract>
Every `path:line` citation in your final report MUST resolve in the current
repo state. The post-synthesis citation linter will verify each one. If you
cite a line in a file you also edited, include a 1-3 line snippet of the
cited code verbatim alongside the citation; the linter uses fuzzy snippet
matching to forgive line-shift after edits.
</contract>
"""


# `combined` = HADS persona system + SoT skeleton-first appendix + CoT <plan>
# task prefix. Tests whether the three structural techniques compose or
# interfere; cross-reference §1 of jiggly-baking-kahan.md if editing.
_COMBINED_SYSTEM = _HADS_SYSTEM + _SOT_APPENDIX
_COMBINED_TASK_PREFIX = _COT_TASK_PREFIX


# Document-task strict directive — addresses under-engagement on doc tasks
# (Phase v1.1 B1). The lpe-rope-calc-document-typing failure mode at temp=0:
# model adds 1 line and stops, even though the task explicitly asks for two
# components (docstring + type hints). The overlay pushes for tool-call
# commitment AND component-completeness coverage.
_DOC_STRICT_TASK_PREFIX = (
    "This is a documentation task. Before you finish:\n"
    "- You MUST call `edit_file` or `write_file` at least once to commit a "
    "real change to disk. Reading and producing prose alone does not "
    "satisfy this task.\n"
    "- You MUST address EVERY component of the goal. If the goal mentions "
    "multiple deliverables (e.g. 'add a module docstring AND type hints'), "
    "each one needs to land in the committed diff. A diff with fewer than "
    "~4 added lines on a multi-component goal almost certainly means you "
    "stopped before finishing.\n"
    "- Your final report should explicitly note which components you "
    "completed.\n\n"
) + _BASELINE_TASK_PREFIX


# Manage-task strict directive — addresses stuck-loop bailouts on audit-style
# manage tasks (Phase v1.1 B2). The nothing-ever-happens-manage-deps-audit
# failure mode: model reads requirements.txt, then loops on identical file
# reads, hits the 2-consecutive-repeat-step abort, no diff produced. The
# overlay pushes for distinct-args enumeration and writing the deliverable
# early instead of indefinite reading.
_MANAGE_STRICT_TASK_PREFIX = (
    "This is a manage / audit task. Three specific failure modes to defend "
    "against:\n"
    "- Re-reading the same file multiple times: the loop detector treats "
    "identical tool calls as stuck behavior and aborts after 2 consecutive "
    "repeat steps. Pick distinct files or distinct line ranges per read; "
    "each tool call should explore something new.\n"
    "- Reading without writing: this task's deliverable is a concrete "
    "committed diff (e.g. a SECURITY-AUDIT.md), not survey prose. Don't "
    "end the run without `edit_file` or `write_file` landing real content.\n"
    "- Hallucinating CVE ids: if you cite a CVE / advisory id, you MUST "
    "first call `cve_lookup` for that package to get the real id, "
    "severity, and fixed-version data. Citing an id you didn't see in a "
    "cve_lookup response means the id is invented. The grader checks "
    "shape; real-world auditors check factuality. Always do the lookup.\n\n"
    "Approach: identify findings ONE AT A TIME. For each candidate item, "
    "(1) call cve_lookup with the package name and ecosystem; (2) pick "
    "the most relevant finding from the response; (3) document it as a "
    "concrete entry (name, real CVE/advisory id from the lookup, fixed "
    "version, one-sentence rationale). Three concrete findings is enough; "
    "you don't need to enumerate every item. Commit the deliverable file "
    "before stopping.\n\n"
) + _BASELINE_TASK_PREFIX


PROMPT_REGISTRY: dict[str, PromptVariant] = {
    "baseline": PromptVariant(
        system=_BASELINE_SYSTEM,
        task_prefix=_BASELINE_TASK_PREFIX,
    ),
    "cot": PromptVariant(
        system=_BASELINE_SYSTEM,
        task_prefix=_COT_TASK_PREFIX,
    ),
    "sot": PromptVariant(
        system=_BASELINE_SYSTEM + _SOT_APPENDIX,
        task_prefix=_BASELINE_TASK_PREFIX,
    ),
    "hads_persona": PromptVariant(
        system=_HADS_SYSTEM,
        task_prefix=_BASELINE_TASK_PREFIX,
    ),
    "combined": PromptVariant(
        system=_COMBINED_SYSTEM,
        task_prefix=_COMBINED_TASK_PREFIX,
    ),
    "document_strict": PromptVariant(
        system=_BASELINE_SYSTEM,
        task_prefix=_DOC_STRICT_TASK_PREFIX,
    ),
    "manage_strict": PromptVariant(
        system=_BASELINE_SYSTEM,
        task_prefix=_MANAGE_STRICT_TASK_PREFIX,
    ),
}


def get(prompt_id: str) -> PromptVariant:
    """Look up a PromptVariant by id. Raises KeyError with a list of
    available ids if the lookup misses — surfaces typos quickly during
    bake-off variant authoring."""
    if prompt_id not in PROMPT_REGISTRY:
        raise KeyError(
            f"unknown prompt_id {prompt_id!r}; "
            f"available: {sorted(PROMPT_REGISTRY)}"
        )
    return PROMPT_REGISTRY[prompt_id]


# --- task-type overlays (Branch B) --
# A TaskOverlay routes per-task-type to a specific PromptVariant id.
# The Phase 1 sweep (jiggly-baking-kahan.md) lifted `implement` to 4/4
# with structural prompts but regressed `document` and `manage`. The
# overlay lets us apply implement-friendly framing only on
# implement/bugfix tasks while keeping baseline framing on docs/manage.
# See ~/.claude/plans/task-type-overlays.md.


@dataclass(frozen=True)
class TaskOverlay:
    """Per-task-type prompt selection.

    `by_task` maps task_type → PromptVariant id. The named id is used
    for BOTH system_prompt_id and task_prompt_id when the overlay
    activates. Task types not in `by_task` fall back to the role's
    role-level system_prompt_id / task_prompt_id (i.e. baseline if
    those are also defaults).
    """
    by_task: dict[str, str]


TASK_OVERLAYS: dict[str, TaskOverlay] = {
    # implement_via_cot — apply CoT structural framing to implement and
    # bugfix tasks; document/manage/review/summarize fall through to the
    # role-level default (baseline by default). Phase 1 data showed CoT
    # cleared 4/4 implements; this composition projects to 8/10 if
    # baseline's doc+manage performance holds.
    "implement_via_cot": TaskOverlay(by_task={
        "implement": "cot",
        "bugfix": "cot",
    }),
    # document_strict_only — applies the document_strict variant on document
    # tasks specifically. Phase v1.1 B1: addresses lpe-rope-calc-document-
    # typing's under-engagement (model adds 1 line and stops despite a
    # multi-component goal). Other task types fall through to role default.
    "document_strict_only": TaskOverlay(by_task={
        "document": "document_strict",
    }),
    # manage_strict_only — applies the manage_strict variant on manage tasks
    # specifically. Phase v1.1 B2: addresses the nothing-ever-happens-manage-
    # deps-audit stuck-loop (model reads requirements.txt repeatedly, hits
    # the loop detector, no diff produced). Other task types fall through.
    "manage_strict_only": TaskOverlay(by_task={
        "manage": "manage_strict",
    }),
}


def get_overlay(overlay_id: str) -> TaskOverlay | None:
    """Look up a TaskOverlay by id. Returns None for empty string or
    unknown id — overlays are opt-in (unlike PromptVariants, which are
    required and surface typos via KeyError). Empty string is the
    "no overlay" sentinel that RoleConfig.task_overlay_id defaults to."""
    if not overlay_id:
        return None
    return TASK_OVERLAYS.get(overlay_id)


def resolve_prompt_ids(
    task_type: str,
    *,
    system_prompt_id: str,
    task_prompt_id: str,
    task_overlay_id: str = "",
) -> tuple[str, str]:
    """Pure resolver: figure out which (system_id, task_id) pair to use
    for a given task_type given the role's prompt + overlay settings.

    If an overlay is set AND it has an entry for `task_type`, the
    overlay's variant id wins for both system and task. Otherwise
    falls back to the role-level ids. Centralised so single.py and
    tests share the same logic.
    """
    overlay = get_overlay(task_overlay_id)
    if overlay and task_type in overlay.by_task:
        variant_id = overlay.by_task[task_type]
        return variant_id, variant_id
    return system_prompt_id, task_prompt_id
