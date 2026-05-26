# luxe — Agent Reference

> This file used to describe the retired swarm pipeline. The live architecture
> is **mono-only**: a single capable model runs the whole task with the full
> tool surface, bounded by `max_steps`. The authoritative architectural
> contracts live in the `.sdd` chain — read those before editing anything
> under the corresponding subtree.

## Live architecture (mono-only)

- **Entry:** `src/luxe/agents/single.py` — assembles the full tool surface and
  the prompt block, then calls `run_agent`.
- **Loop:** `src/luxe/agents/loop.py:run_agent` — the inner tool-call loop.
  Per-step it: samples `context_pressure` (elides old tool results at >0.7),
  runs pre-step interventions, calls `backend.chat`, validates and dispatches
  tool calls, appends results, checks exit gates.
- **Tools:** native registry in `single.py:_build_full_tool_surface` →
  `tools/fs.py` (read/write/edit with honesty guards + SpecDD Forbids),
  `tools/search.py` (BM25), `tools/symbols.py` (AST), `tools/git.py`,
  `tools/shell.py`, analysis tools. Dispatch via `tools/base.py:dispatch_tool`.
- **Prompts:** `src/luxe/agents/prompts.py` `PROMPT_REGISTRY` is the single
  source of truth. `TaskOverlay` + `resolve_prompt_ids` route `task_type` to
  a `PromptVariant`. The `.sdd` chain block is appended after the variant's
  `task_prefix` (`single.py:_build_sdd_block`).
- **Interventions** (in-loop, all bias-not-lock): `WRITE_PRESSURE`,
  `EARLY_BAIL`, `PROSE_BURST`, `ACTION_DENSITY_GATE`, pre-dispatch spec gate.
  `outcomes.py` is the Track 5 observability primitive.
- **Optional stages, default-off / byte-identical when disabled:** reflect /
  verify (`LUXE_REFLECT`), adaptive policy (`LUXE_ADAPTIVE_POLICY`), cohort
  priors (`LUXE_LOAD_PRIORS`, log-only in v1.11). See `agents.sdd` for the
  invariants each must satisfy.

## What's retired (do not re-introduce)

`src/swarm/**`, `src/micro/**`, `src/phased/**` modes (Architect / Worker
(Read/Code/Analyze) / Validator / Synthesizer chain) are retired and
**Forbidden** by `src/luxe/luxe.sdd`. Do not add feature flags to bring them
back; the `tools/fs.py` role-path guard actively blocks writes to those
paths.

## Where to read next

- `src/luxe/luxe.sdd` — root invariants (mono-only, temp=0, pinned work-dir,
  excluded model families).
- `src/luxe/agents/agents.sdd` — agent loop + prompt registry + reflect +
  v1.11 adaptive-policy invariants.
- `src/luxe/tools/tools.sdd` — honesty-guard + Forbids enforcement order.
- `benchmarks/maintain_suite/maintain_suite.sdd` — bench rules.
- `CLAUDE.md` — onboarding doc + project-specific gotchas + single-champion
  policy.
- `README.md` — user-facing overview and current `--version`.
- `RESUME.md` — active state and current cycle.
- `lessons.md` — postmortems for every historical surprise.
