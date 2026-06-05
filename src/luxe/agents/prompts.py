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
    "- Hallucinating CVE ids: if you cite a CVE / GHSA / advisory id, "
    "you MUST first call `cve_lookup` for that package and cite ids "
    "EXACTLY as they appear in the response's `id` or `aliases` fields. "
    "Don't translate between schemes (GHSA ↔ CVE), don't combine the "
    "tool's data with training-data recall, don't invent ids the "
    "response doesn't contain. The grader checks shape; real-world "
    "auditors check factuality.\n\n"
    "Approach: identify findings ONE AT A TIME. For each candidate item, "
    "(1) call cve_lookup with the package name and ecosystem; (2) pick "
    "the most relevant finding from the response; (3) document it as a "
    "concrete entry (name, real id(s) from `id`/`aliases`, fixed "
    "version, one-sentence rationale grounded in the response's "
    "`summary`). Three concrete findings is enough; you don't need to "
    "enumerate every item. Commit the deliverable file before stopping.\n\n"
) + _BASELINE_TASK_PREFIX


# SWE-bench bug-fix directive — addresses the smoke-run failure mode where
# the model creates reproducer scripts (`repo_root/test_sep.py`, `astropy/
# timeseries/test_bug.py`) instead of editing existing source. Same prose-
# mode/demonstrate-don't-act bias BFCL exposed (43/70 simple_python
# failures = no_tool_call_emitted). Also enforces one-tool-per-response to
# defend against the parallel-call cliff (49% PASS on parallel_multiple).
_SWEBENCH_TASK_PREFIX = (
    "This is a SWE-bench bug-fix task. Your deliverable is a patch to "
    "EXISTING source files within the package source tree. Only edits "
    "to package source files are graded; new files and test edits are "
    "ignored.\n\n"
    "Core constraints:\n"
    "1. Modify existing package source only. Do NOT create any new "
    "files in the repository.\n"
    "2. Treat reproducer snippets in the bug report as search context "
    "to locate the buggy code. Rely on static analysis — reading code "
    "and grepping — rather than executing reproducers.\n"
    "3. Focus edits on the core package logic. Do NOT modify or add "
    "tests; the grader provides its own test suite.\n"
    "4. Invoke ONE tool per response. Do not emit parallel tool calls.\n\n"
    "If you cannot confidently locate the bug after initial search, "
    "continue exploring (read additional files, trace call sites). Do "
    "not guess at an edit.\n\n"
    "Linear protocol (single pass):\n"
    "  (1) read bug report → identify likely module/function\n"
    "  (2) call grep or find_symbol to locate the code\n"
    "  (3) read the function and surrounding context\n"
    "  (4) make a minimal edit via edit_file\n"
    "  (4.5) verify the change is consistent with call sites and "
    "surrounding logic\n"
    "  (5) (optional) run existing tests via bash\n"
    "  (6) final report\n\n"
    "Open with a brief `## Plan` section (≤150 words), then "
    "IMMEDIATELY call grep or find_symbol. Keep subsequent reasoning "
    "concise and technical.\n\n"
) + _BASELINE_TASK_PREFIX


# Counterexample-heuristic clause — to be A/B-tested against the base
# swebench_bugfix prompt on the n=10 stratified probe. Targets the
# astropy-12907 trajectory: model traces the bug report's simple snippet,
# concludes the code is correct, never tests the failing variant. The
# clause names the contradiction (trace OK + report shows wrong output)
# as a falsification signal and prescribes constructing the failing
# variant. General debugging heuristic — not 12907-specific.
_SWEBENCH_COUNTEREXAMPLE_CLAUSE = (
    "If your trace of a snippet from the bug report yields the expected "
    "result but the report shows a different output, that contradiction "
    "is the signal: the bug lives in a code path the simple input does "
    "not exercise. Construct the more complex / nested / edge-case "
    "variant described in the report and trace it through the same "
    "functions before deciding the code is correct.\n\n"
)

# Surgically insert the clause before the "Linear protocol" header in
# the base swebench prompt. The asserts catch silent drift if the base
# prompt's structure ever changes — better to fail at import time than
# to ship a no-op variant.
assert "Linear protocol (single pass):\n" in _SWEBENCH_TASK_PREFIX, (
    "swebench prompt structure changed; counterexample-clause insert "
    "point is no longer present"
)
_SWEBENCH_COUNTEREXAMPLE_TASK_PREFIX = _SWEBENCH_TASK_PREFIX.replace(
    "Linear protocol (single pass):\n",
    _SWEBENCH_COUNTEREXAMPLE_CLAUSE + "Linear protocol (single pass):\n",
)
assert _SWEBENCH_COUNTEREXAMPLE_CLAUSE in _SWEBENCH_COUNTEREXAMPLE_TASK_PREFIX


# forge-hybrid Phase 3 (B2) — respond-terminal protocol clause. Pairs with
# the LUXE_RESPOND_TERMINAL=1 lever (src/luxe/tools/respond.py). The B1
# disambiguation arm (tool exposed, prompt unchanged) showed 0/14 organic
# adoption — the champion doesn't discover the respond tool from its
# presence alone. B2 adds explicit guidance so we can distinguish whether
# the prompt encouraging termination is what changes behavior, vs the
# structured tool surface itself.
#
# The clause inserts into the Linear protocol at step (6), replacing
# "final report" with an explicit "call respond(message=...) with a brief
# summary of the change". Watchdogs in the loop catch premature / no-write
# / passive-surrender / compaction-phantom shapes.
_SWEBENCH_RESPOND_CLAUSE = (
    "When the edit is complete and you have verified it, call "
    "`respond(message=...)` with a brief summary of the change. This "
    "terminates the loop cleanly. Do NOT call `respond` before writing "
    "the deliverable — the watchdog will reject premature calls.\n\n"
)
assert "  (6) final report\n\n" in _SWEBENCH_TASK_PREFIX, (
    "swebench prompt structure changed; respond-clause insert point "
    "is no longer present"
)
_SWEBENCH_RESPOND_TASK_PREFIX = _SWEBENCH_TASK_PREFIX.replace(
    "  (6) final report\n\n",
    "  (6) call `respond(message=...)` with a brief summary of the change\n\n"
    + _SWEBENCH_RESPOND_CLAUSE,
)
assert _SWEBENCH_RESPOND_CLAUSE in _SWEBENCH_RESPOND_TASK_PREFIX


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
    "swebench_bugfix": PromptVariant(
        system=_BASELINE_SYSTEM,
        task_prefix=_SWEBENCH_TASK_PREFIX,
    ),
    "swebench_bugfix_counterexample": PromptVariant(
        system=_BASELINE_SYSTEM,
        task_prefix=_SWEBENCH_COUNTEREXAMPLE_TASK_PREFIX,
    ),
    "swebench_bugfix_respond": PromptVariant(
        system=_BASELINE_SYSTEM,
        task_prefix=_SWEBENCH_RESPOND_TASK_PREFIX,
    ),
}


# Read-only-mode framing for `luxe chat`. Lives here (registry = single source
# of truth for prompt strings; chat.sdd forbids prompt strings in the chat
# module). Injected by ChatSession.build_extra_context ONLY when write mode is
# off, so the model stops reporting "luxe can't create/edit files" and instead
# points the user at /write. Benchmark/maintain never see this (they pass
# extra_context="").
READ_ONLY_CHAT_HINT = (
    "This is an interactive read-only chat turn: the write_file, edit_file, and "
    "bash tools are intentionally withheld right now. luxe fully supports them — "
    "they are gated off by default and the user enables them on demand. If "
    "carrying out this request needs creating, editing, or running files, do "
    "whatever read-only analysis you can and then tell the user to type /write "
    "to turn on write tools. Never claim luxe lacks the ability to create or "
    "edit files; it has write_file (creates files and parent directories), "
    "edit_file, and bash once write mode is on."
)


TERSE_HINT = (
    "Respond tersely: report only what changed and the result, in as few words as "
    "possible. No preamble, no restating the request or the plan, no end-of-turn "
    "recap or 'Final report' — when work is done, say so in one line. Don't "
    "re-summarize file contents you just wrote or re-run checks that already "
    "passed. Prefer recording status with the update_ledger tool over writing it "
    "as prose. This applies to YOUR prose ONLY — never abbreviate tool inputs or "
    "outputs, never shorten error messages or stack traces, and never skip a read, "
    "a test, or a safety confirmation the task needs in order to save words."
)

# Read-only planning preamble (/plan): the model drafts a plan instead of editing.
PLAN_HINT = (
    "Planning mode: produce a concise, actionable implementation PLAN for the "
    "request — do NOT write or edit code yet. Read/search as needed to ground it. "
    "Structure the plan as: Context (why), Steps (ordered), Files to change, and "
    "Verification (how to test). Keep it tight and skimmable; this plan may be "
    "saved to a file and/or handed to the autonomous runner to execute."
)


# -- gitkit read-only repo-analysis directives (src/luxe/gitkit) ------------
# These ride in the per-report `goal` (like PLAN_HINT) — single-source-of-truth
# rule keeps the strings here, never inlined in the gitkit module (gitkit.sdd
# Forbids prompt strings). All three are read-only, single-pass analyses; a
# `<repo_health>` / `<github_metadata>` data block is injected via extra_context.

# Shared discipline for every gitkit report. The first real-world test showed the
# model emitting its exploration monologue AS the final message (and truncating
# mid-thought) — a "mode" failure where it treats the last turn as more thinking.
# This clause forces the final message to be the report ONLY; the runner also
# slices from the first `# ` header as a deterministic safety net.
_GITKIT_REPORT_DISCIPLINE = (
    "Do your investigation with tools and reasoning during the run, but your "
    "FINAL message must be the finished report ONLY — markdown, nothing else. No "
    "exploration narrative, no numbered 'I looked at … / what if …' musings, no "
    "chain-of-thought, no preamble or sign-off. Decide your conclusions first, "
    "then write them once, concisely. Do not write or edit any files."
)

GIT_SUMMARY_HINT = (
    "Produce a REPOSITORY SUMMARY & RISK ASSESSMENT for this project. "
    + _GITKIT_REPORT_DISCIPLINE + "\n\n"
    "Begin the report with EXACTLY this shape (so it is skimmable at a glance):\n"
    "  # Repository summary & risk assessment\n"
    "  **Use-risk: low|medium|high** — <≤15-word reason>\n\n"
    "Then these sections, grounded in files you read (cite paths) and the injected "
    "<repo_health> / <github_metadata> data; use deps_audit / cve_lookup for "
    "dependency exposure:\n"
    "- **Purpose** — what the project is and does.\n"
    "- **Stack & languages** — primary languages/frameworks (reflect the "
    "files/LOC/language mix in <repo_health>).\n"
    "- **Dependencies & their risk** — key deps and any known-vulnerable ones.\n"
    "- **Health & size** — activity cadence, recency, contributors, and merged/"
    "open PR + issue + release activity, citing <repo_health> / <github_metadata>; "
    "note when GitHub data was unavailable.\n"
    "- **Security posture** — SECURITY.md, advisories, secrets handling.\n"
    "- **Use-risk verdict** — restate the rating with a short rationale."
)

# Folded into the review/refactor reports (gitsummary is no longer a deep kind):
# a compact orientation section the audit/plan reports gain for free from the
# survey + health data they already gather.
_SUMMARY_SECTION = (
    "\n\nImmediately AFTER the title + count line (and BEFORE the findings/steps), "
    "include a brief `## Repository summary & risk` section — at most ~6 lines, "
    "grounded in the files you read and the injected <repo_health>/<github_metadata>: "
    "a one-line **Use-risk: low|medium|high** verdict with a ≤15-word reason, then "
    "**Purpose**, **Stack & languages** (reflect the <repo_health> LOC/language mix), "
    "**Dependencies & risk** (flag known-vulnerable deps), and **Health & size** "
    "(activity/recency/contributors, citing <repo_health>/<github_metadata>; note when "
    "GitHub data was unavailable). Keep it tight — orientation, not the main body."
)

GIT_REVIEW_HINT = (
    "Perform a read-only bug & security REVIEW of this codebase and report only "
    "SERIOUS, code-grounded findings. " + _GITKIT_REPORT_DISCIPLINE + "\n\n"
    "Begin the report with EXACTLY this shape:\n"
    "  # Bug & security review\n"
    "  **Findings: N (C critical, H high, M medium, L low)**\n\n"
    "Then list findings grouped by severity, highest first. Confirm-or-dismiss "
    "discipline: confirm each suspected issue in the actual code (use grep, "
    "find_symbol, security_scan) or DROP it — NEVER list considered-then-dismissed "
    "items, speculative/generic/'best-practice' risks, or lint / style / type-"
    "annotation nits. Every finding MUST include: severity (Critical/High/Medium/"
    "Low), file path, line number, the offending code as evidence, the impact, and "
    "a suggested fix. If nothing serious qualifies, the summary line is "
    "**Findings: 0** followed by one short paragraph naming what you checked."
    + _SUMMARY_SECTION
)

# Appended to both refactor hints. When a `<prior_findings>` block (the findings
# of a same-commit gitreview) is injected, the refactor must not undo those fixes
# nor re-litigate the bugs — it uses them to PRIORITIZE structural work.
_PRIOR_FINDINGS_CLAUSE = (
    "\n\nIf a `<prior_findings>` block is present, it lists bugs/security issues a "
    "prior review already found in THIS commit. Treat them as known: do NOT re-report "
    "them as refactor steps, and ensure NO step would undo or obscure one of those "
    "fixes. You MAY reference them (by file:line) to justify prioritizing a refactor "
    "that makes the risky area safer or easier to fix."
)

GIT_REFACTOR_HINT = (
    "Propose a read-only structural REFACTOR PLAN for this codebase. "
    + _GITKIT_REPORT_DISCIPLINE + "\n\n"
    "Begin the report with EXACTLY this shape:\n"
    "  # Refactor plan\n"
    "  **Refactor steps: N** — <≤15-word headline of the biggest win>\n\n"
    "Then an ORDERED list of steps. Focus STRICTLY on structure: coupling, "
    "cohesion, module boundaries, duplication, dead code, testability, ownership. "
    "Do NOT report correctness or security defects except where one materially "
    "blocks a refactor step. For each step give: what to change (cite files/"
    "symbols), the rationale, the risk level, and how to verify it is safe (tests "
    "to run / behavior to preserve)."
    + _SUMMARY_SECTION
    + _PRIOR_FINDINGS_CLAUSE
)


# --- gitkit DEEP MODE (staged map-reduce for large repos) ------------------
# Deep mode runs multiple sequential read-only run_single passes (survey →
# per-chunk → synthesis) orchestrated by gitkit/deep.py. These hints are the
# single source for that orchestration's directives (gitkit.sdd Forbids inline
# prompts). Each pass is still ONE mono call; the front-end does the staging.

# Stage 0 — kind-agnostic survey: build an architectural hypothesis that frames
# every downstream chunk. Output is free-form markdown notes (NOT the final
# report), so no fixed header is required here.
GIT_SURVEY_HINT = (
    "You are SURVEYING a repository to build an ARCHITECTURAL HYPOTHESIS that "
    "will frame a staged, file-by-file deep analysis to follow. Read the framing "
    "files listed below (entrypoints, routing, CI, container/deploy, auth & "
    "config) plus anything they point to, and use the injected <repo_health> / "
    "<github_metadata> data. Do NOT attempt a full bug/refactor pass now and do "
    "NOT read every file — form the map.\n\n"
    "Output ONLY concise markdown survey notes (no preamble, no final-report "
    "headers) covering: what the project IS and does; its architecture and the "
    "main modules/layers; entrypoints and request/data flow; the key domain "
    "entities; cross-cutting concerns (authn/authz, input/webhook validation, "
    "secrets, persistence, external calls); and where the highest RISK or "
    "refactor surface likely sits. Be specific and cite file paths. Keep it "
    "tight — this is a map, not a report."
)

# Stage 2 — per-chunk analysis. Per-kind goal in the SAME markdown report shape
# the single-pass uses (the champion reliably produces and concludes this; an
# earlier JSON-only contract made it ramble past the token cap without ever
# emitting structure). Severity is PROVISIONAL — the synthesis re-rates globally.
# The runner slices from the required header, so any leading monologue is dropped;
# keep findings COMPACT so the aggregate fits the synthesis window.
GIT_REVIEW_CHUNK_HINT = (
    "Perform a read-only bug & security REVIEW of ONLY the files listed below and "
    "report only SERIOUS, code-grounded findings. " + _GITKIT_REPORT_DISCIPLINE
    + "\n\nBegin the report with EXACTLY this shape:\n"
    "  # Bug & security review\n"
    "  **Findings: N (C critical, H high, M medium, L low)**\n\n"
    "Then findings grouped by severity, highest first. Severities are PROVISIONAL "
    "— a later whole-repo synthesis re-rates them. Confirm-or-dismiss discipline: "
    "confirm each suspected issue in the actual code (grep, find_symbol, "
    "security_scan) or DROP it — NEVER list considered-then-dismissed items, "
    "speculative/generic/'best-practice' risks, or lint/style/type nits. Every "
    "finding MUST be ONE tight entry: severity, file path, line number, the "
    "offending code as evidence, the impact, and a suggested fix — a few lines "
    "each, not an essay. If nothing serious qualifies in THESE files, the line is "
    "**Findings: 0** followed by one short sentence naming what you checked."
)

GIT_REFACTOR_CHUNK_HINT = (
    "Identify ONLY STRUCTURAL issues in the files listed below — coupling, "
    "cohesion, module boundaries, duplication, dead code, testability, ownership. "
    + _GITKIT_REPORT_DISCIPLINE
    + "\n\nBegin the report with EXACTLY this shape:\n"
    "  # Refactor plan\n"
    "  **Refactor steps: N** — <≤15-word headline of the biggest win here>\n\n"
    "Then an ordered list of structural steps for THESE files: what to change "
    "(cite files/symbols), the rationale, the risk level, and how to verify. "
    "Priorities are PROVISIONAL — a later synthesis re-orders globally. Do NOT "
    "report correctness or security defects unless one materially blocks a "
    "refactor. Keep each step tight."
)

# Stage 3 — holistic synthesis over the AGGREGATE notes (NOT raw files). Emits
# the consolidated report in the required gitkit shape; re-rates severity
# globally and merges same-root-cause findings.
_DEEP_SYNTH_COMMON = (
    "You are SYNTHESIZING a final report from STRUCTURED NOTES gathered over a "
    "staged, chunk-by-chunk pass of a repository (provided below as the survey "
    "map + per-chunk findings/entities/modules). Work ONLY from these notes — do "
    "not re-read the repo. " + _GITKIT_REPORT_DISCIPLINE + "\n\n"
    "Two consolidation rules are mandatory:\n"
    "1. MERGE DUPLICATES — findings that describe the SAME root cause become ONE "
    "consolidated finding that lists all its evidence locations (never count the "
    "same issue twice across chunks).\n"
    "2. RE-RATE SEVERITY GLOBALLY — the per-chunk severities are PROVISIONAL; "
    "promote or demote each using whole-repo context (e.g. an internal admin-only "
    "endpoint behind a VPN drops from Critical to Medium).\n"
    "3. BE HONEST ABOUT COVERAGE — if the notes include a non-empty "
    "`unparsed_chunks` list, those areas could NOT be analyzed (truncated/empty "
    "output); state this explicitly in the report (e.g. a short 'Not analyzed' "
    "note listing them) rather than implying the repo is clean.\n"
)

GIT_REVIEW_SYNTH_HINT = (
    _DEEP_SYNTH_COMMON + "\n"
    "Begin the report with EXACTLY this shape:\n"
    "  # Bug & security review\n"
    "  **Findings: N (C critical, H high, M medium, L low)**\n\n"
    "Then findings grouped by (re-rated) severity, highest first. Every finding "
    "MUST include: severity, file path, line number, the offending code as "
    "evidence, the impact, and a suggested fix. If nothing serious survives "
    "consolidation, the line is **Findings: 0** followed by one short paragraph "
    "naming what was checked."
    + _SUMMARY_SECTION
)

GIT_REFACTOR_SYNTH_HINT = (
    _DEEP_SYNTH_COMMON + "\n"
    "Begin the report with EXACTLY this shape:\n"
    "  # Refactor plan\n"
    "  **Refactor steps: N** — <≤15-word headline of the biggest win>\n\n"
    "Then an ORDERED list of steps (highest-leverage first). For each: what to "
    "change (cite files/symbols), the rationale, the risk level, and how to "
    "verify it is safe (tests to run / behavior to preserve). Focus STRICTLY on "
    "structure."
    + _SUMMARY_SECTION
    + _PRIOR_FINDINGS_CLAUSE
)


# Stage 3 cleanup — the champion narrates its consolidation reasoning into the
# report. This pass is pure TRANSCRIPTION (the lowest-judgment task, so the least
# rambly): reproduce the already-decided report cleanly, copying findings verbatim.
GIT_DEEP_FORMAT_HINT = (
    "Below is a consolidated report draft mixed with the author's working notes "
    "and reasoning. Reproduce it as a CLEAN final report and nothing else. COPY "
    "the concrete findings/sections verbatim (keep every severity, file:line, "
    "evidence, impact, and fix exactly as written); DROP all working notes, "
    "narration, 'let me…', 're-rating', 'consolidation', and any text that is not "
    "part of the finished report. Do NOT use tools, do NOT re-analyze, do NOT add "
    "new findings or commentary. Output ONLY the report, beginning at its required "
    "header."
)

# Stage 3 fallback — when the aggregate notes overflow one window, findings are
# consolidated in batches FIRST (this hint), then the survivors go to the normal
# synthesis. Emits the same JSON findings shape, not the final report.
GIT_DEEP_REDUCE_HINT = (
    "You are consolidating a BATCH of raw findings gathered chunk-by-chunk from a "
    "repository. Work ONLY from the findings provided below. MERGE findings that "
    "share a root cause into one (union their evidence locations, keep the "
    "strongest), drop exact duplicates, and keep each finding's most telling "
    "evidence (cap 3 `file:line`). Do not invent new findings. Output ONLY a "
    "single fenced ```json code block of this shape:\n"
    "```json\n"
    '{"findings": [{"title": "", "root_cause": "", "severity": "", '
    '"evidence": ["file:line"], "impact": "", "fix": ""}]}\n'
    "```"
)


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
    # swebench_strict_only — applies the swebench_bugfix variant on bugfix
    # tasks specifically. SWE-bench smoke (2026-05-04) showed the model
    # creating reproducer scripts instead of editing source; this overlay
    # forbids new files, treats reproducers as search context, enforces a
    # linear protocol, and requires one tool call per response (the latter
    # informed by the BFCL parallel-call cliff). Activated via the
    # configs/single_64gb_swebench.yaml derived config; the default
    # configs/single_64gb.yaml is unaffected.
    "swebench_strict_only": TaskOverlay(by_task={
        "bugfix": "swebench_bugfix",
    }),
    # swebench_strict_counterexample_only — A/B variant of the above that
    # routes bugfix to swebench_bugfix_counterexample (adds the
    # falsification heuristic). Activated via configs/single_64gb_swebench
    # _counterexample.yaml; the default swebench config still uses the
    # baseline overlay so the A/B is one config-flag apart.
    "swebench_strict_counterexample_only": TaskOverlay(by_task={
        "bugfix": "swebench_bugfix_counterexample",
    }),
    # swebench_strict_respond_only — forge-hybrid Phase 3 (B2) variant that
    # pairs with LUXE_RESPOND_TERMINAL=1. Adds explicit guidance telling the
    # model to call respond(message=...) at the end of the linear protocol.
    # The B1 smoke (2026-05-28) confirmed the tool alone has 0/14 organic
    # adoption — the prompt nudge is the load-bearing change.
    "swebench_strict_respond_only": TaskOverlay(by_task={
        "bugfix": "swebench_bugfix_respond",
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
