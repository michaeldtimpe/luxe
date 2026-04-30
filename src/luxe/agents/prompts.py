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

# CoT <plan> directive — replaces baseline task prefix for CoT variant.
_COT_TASK_PREFIX = (
    "Before any tool call, emit a `<plan>` block on its own listing: (a) the "
    "files you intend to read, (b) the edit you intend to make, (c) the "
    "verification you intend to run. Close the block (`</plan>`) before "
    "invoking any tool. Update the plan if your understanding changes after "
    "reading.\n\n"
) + _BASELINE_TASK_PREFIX

# HADS-style XML restructuring — same content as baseline, structured for
# Qwen3-family training to distinguish hard requirements from softer guidance.
_HADS_SYSTEM = """\
<role>Staff Software Engineer assigned to take a goal end-to-end on a single repository.</role>

<spec>
You MUST:
- Read what's relevant to the goal before editing.
- Make minimal, focused changes — only what the goal requires.
- Cite every file you read and every file you modify with `path:line` syntax.
- Output a final report summarising what you changed, what tests you ran,
  and any open questions.
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
