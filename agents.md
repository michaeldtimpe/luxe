# luxe — Agent Reference

> **Authority boundary.** Normative constraints live in the `.sdd` chain
> (`src/luxe/luxe.sdd`, `src/luxe/agents/agents.sdd`,
> `src/luxe/tools/tools.sdd`, `benchmarks/maintain_suite/maintain_suite.sdd`).
> This file *summarizes the currently active implementation architecture*,
> not policy. File:line references are advisory snapshots and may drift
> between cycles — verify against current code when in doubt.

## Live architecture (mono-only)

- **Entry**: `src/luxe/agents/single.py:run_single` (≈95–151) assembles the
  full tool surface (`_build_full_tool_surface` ≈27–92), resolves prompts
  via `resolve_prompt_ids`, appends the SDD chain via `_build_sdd_block`
  (≈154–186), and calls `run_agent`. MCP tools are appended unconditionally
  after the native surface.

- **Loop**: `src/luxe/agents/loop.py:run_agent` is the inner tool-call
  loop. Per step it: samples `context_pressure` (≈690), runs the env-gated
  pre-step interventions, calls `elide_old_tool_results` (≈1223) before
  the model call, invokes `backend.chat` (≈1226), validates + dispatches
  tool calls and appends results (≈1343–1484), and checks exit gates
  (post-write-idle ≈1486, stuck-loop ≈1506, habituation ≈1139,
  prose-burst clean-exit ≈1113, max-steps ≈1523).

- **Tools**: native registry assembled in `single.py` — read-only fs
  (`read_file`, `list_dir`), BM25 search (`bm25_search`), AST symbols
  (`find_symbol`), mutation fs (`write_file`, `edit_file` with honesty
  guards + SpecDD Forbids enforced at write-time in `tools/fs.py`), git,
  shell, analysis tools (language-specific linters/type-checkers), and
  task-gated `cve_lookup` (only when `task_type == "manage"`). Dispatch
  goes through `src/luxe/tools/base.py:dispatch_tool`.

- **Prompts**: `PROMPT_REGISTRY` in `src/luxe/agents/prompts.py` is the
  single source of truth (variants: baseline, cot, sot, hads_persona,
  combined, document_strict, manage_strict, swebench_bugfix,
  swebench_bugfix_counterexample). `TaskOverlay` maps `task_type` →
  `PromptVariant` id; `resolve_prompt_ids` is the pure resolver. The
  SDD chain block is appended after the variant's `task_prefix`.

- **Interventions** (all default-OFF, opt-in via env var, bias-not-lock):
  `WRITE_PRESSURE` (`LUXE_WRITE_PRESSURE`),
  `EARLY_BAIL` (`LUXE_EARLY_BAIL`, with band-response variants
  selected via `LUXE_EARLY_BAIL_MODE` and the
  `LUXE_EARLY_BAIL_COMMIT_ONLY` refined-port diagnostic — default-OFF
  per commit `122831d`, where the early_bail family was REFUTED for
  edit-quality; see `lessons.md` 2026-05-26),
  `PROSE_BURST` (`LUXE_PROSE_BURST`),
  `ACTION_DENSITY_GATE` (`LUXE_ACTION_DENSITY_GATE`).
  The pre-dispatch **spec gate** (≈1296–1328) is always on; it enforces
  `spec.expects_zero_calls` and minimum-tool-calls invariants from the
  SpecDD Lever 1 fixture contracts. `src/luxe/agents/outcomes.py` is the
  Track 5 observability primitive (Outcome / Intervention / FailureClass
  enums).

- **Optional stages** (default-off / byte-identical when disabled):
  - `LUXE_REFLECT` — verify+repair stage (`src/luxe/agents/reflect.py`);
    see `agents.sdd` § "Reflection / verify stage invariants" and §
    "Phase 2 repair invariants".
  - `LUXE_ADAPTIVE_POLICY` — convergence-score-based intervention
    intensity modulation; bias-not-lock, slew-rate-limited via
    `LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP`. See `agents.sdd`
    § "Stage 3 / v1.11 adaptive-policy invariants".
  - `LUXE_LOAD_PRIORS` — cohort-priors reader
    (`src/luxe/agents/cohort_priors.py`); **log-only in v1.11**,
    promotion deferred to v1.11.1+.

## Retired (Forbidden by `luxe.sdd`)

`src/swarm/**`, `src/micro/**`, `src/phased/**` — the Architect / Worker
(Read/Code/Analyze) / Validator / Synthesizer chain is retired. Do not add
feature flags to bring them back. `tools/fs.py` role-path guard actively
blocks writes that match the role-name vocabulary, and the SDD chain
surfaces the Forbids in the task prompt before the first write attempt.

## Where to read next

- `src/luxe/luxe.sdd` — root invariants (mono-only, temp=0, pinned
  work-dir, excluded model families, champion-model pin).
- `src/luxe/agents/agents.sdd` — agent loop + prompt registry + reflect
  + v1.11 adaptive-policy invariants.
- `src/luxe/tools/tools.sdd` — honesty-guard + Forbids enforcement order.
- `benchmarks/maintain_suite/maintain_suite.sdd` — bench rules
  (`vacuous_test` gating, `--keep-loaded`, sidecar regrade).
- `CLAUDE.md` — onboarding doc, single-champion policy, opt-in modes,
  project-specific gotchas.
- `README.md` — user-facing overview and current `--version`.
- `RESUME.md` — active state and current cycle.
- `lessons.md` — postmortems for every historical surprise.
- `docs/research/` — read-only audits (agentic-patterns 2026-05-25,
  forge-overlap, hermes-harvest); see `docs/research/README.md` for
  authority labels.
