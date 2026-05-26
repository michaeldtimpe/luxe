# Agentic Patterns — Exploration Plan (luxe)

## Context

This started as an audit of the agentic-patterns wiki
(`https://veso.ai/research/agentic-patterns/`) mapped onto the luxe project
(`/Users/michaeltimpe/Downloads/luxe` — the working dir
`agentic-patterns-luxe-research/` is an empty scratch folder where this doc
will be saved). luxe is itself an agentic system (an MLX-only autonomous
repo-maintainer), so the wiki's patterns map directly onto its architecture.

This revision converts the audit into an **exploration plan**: *how we would
investigate each element further*. **We are not making changes or running
experiments now.** Every track below is read-only investigation against the
existing source and recorded artifacts; implementation/experiments are
explicitly deferred. The deliverable is this document, saved to the working
folder for later (`./agentic-patterns-exploration-plan.md`).

Reviewer feedback has been folded in. The four substantive recalibrations:
1. **P5 is a softer match than first presented** — reclassified below.
2. **Context lifecycle (G1) outranks cost alerts (G2)** in strategic priority.
3. **Instruction/`.sdd` semantic-drift** added as a real (if subtle) concern.
4. **Meta-thesis:** luxe is converging on a *systems-reliability* philosophy,
   not a *cognitive-architecture* one — used as a lens throughout.

---

## Recalibrations from review (what changed and why)

- **P2 is the deepest finding, restated sharper.** luxe's `.sdd` + write-time
  guards are *architectural containment*, not prompt engineering — invariant
  checking outside the model, at the correct abstraction boundary. The systems
  that stay stable over long autonomous runs converge on exactly this (hard
  tool constraints, deterministic enforcement, narrow write surfaces,
  externalized invariants) rather than on orchestration topology. This matters
  more than whether the system uses MCP or a graph executor.

- **P5 downgraded ✅ → ◐ (softer match).** The wiki's "coordinate through
  shared state" implies explicit inter-agent coordination semantics, durable
  shared memory, recoverable synchronization boundaries, and multi-actor
  consistency. luxe's mono-loop + checkpoints satisfies the *intent
  operationally* but is a different architectural category. Reclassify as
  **"single-agent serialized coordination via durable run state."** This is a
  *feature*, not a weakness — it avoids distributed-agent pathologies entirely.
  The earlier write-up over-credited P5 parity.

- **G1 > G2 in priority.** "70% compacts, 80% hard backend 400" is not an
  ergonomics nit — it is a central scaling cliff for long-horizon agents. Once
  runs become iterative / repair-oriented / benchmark-loop-driven, context
  exhaustion becomes a *primary* failure mode. Crucially, **compaction alone is
  insufficient — it preserves entropy accumulation.** Systems that scale develop
  hierarchical summaries, scratchpad eviction, state distillation, explicit
  phase closure, and resumable task state. luxe's Track/Lever/Phase structure is
  already implicitly moving this way. Cost-threshold alerts (G2), by contrast,
  are operational telemetry — useful but tactical.

- **G4 has a subtler edge.** Doc length (363 ln vs <200) is the least
  interesting part. The real risk is **semantic drift between the instruction
  docs (`CLAUDE.md`/`AGENTS.md`) and the `.sdd` chain** as both grow — rules
  that silently contradict or override each other. Worth a periodic consistency
  check even if neither doc is individually too long.

- **What's strongest, confirmed.** P2 (architectural containment); P8 +
  benchmark culture (Track/Lever/Phase, SWE-bench gating, preserved negative
  results, the "do NOT generalize to reasoning-ceiling-proven" hygiene) — luxe
  behaves like a *research program*, not just a codebase; and the
  "decompose-before-degrade" healthy failure model (bounded steps, explicit
  exits, spec gating) vs. the common silent degradation into longer prompts /
  recursive retries / planner nesting.

- **Meta-thesis (the lens for everything below).** luxe is converging toward
  *bounded autonomy, deterministic enforcement, benchmarked iteration,
  controlled decomposition, operational stability* — a systems-reliability
  philosophy — rather than the industry's *better planners / memory graphs /
  agent societies / recursive reflection* cognitive-architecture direction.
  Infrastructure that survives historically evolves in the first direction. Any
  exploration track is judged by whether it strengthens reliability primitives,
  not whether it adds cognitive machinery.

---

## Recalibrated scorecard

| # | Wiki principle | Status | Note |
|---|----------------|--------|------|
| P1 | Persistent Instruction File | ✅ | + watch doc/`.sdd` drift (G4) |
| P2 | Enforce Safety Outside the Prompt | ✅✅✅ | architectural containment — the differentiator |
| P3 | Budget Context Window | ◐ | compacts at 70%, but hard 80% cliff → top exploration (E1) |
| P4 | Build Tools on MCP | ◐ deliberate | native registry; MCP opt-in — valid choice |
| P5 | Coordinate Through Shared State | ◐ softer | single-agent serialized coordination via durable run state |
| P6 | Decompose Before Degrades | ✅ | healthy failure model |
| P7 | Track Cost Per Task | ◐ | accounting yes; alerts (E4) tactical, routing deliberate |
| P8 | Add Complexity Weekly | ✅✅ | research-program maturity |

Layers: Instruction ✅ · Settings ✅ (minimal by design) · Tool Registry ✅
(native) · Execution Loop ✅ · Extensions ✅. Design principles: Convergence
Through Constraints ✅ (luxe is a case study) · System-Specific Weighting ✅
(domain-substrate: deterministic extraction first). Legend: ✅ applied · ◐
partial / deliberate divergence.

---

## Exploration tracks (read-only, ordered by strategic value)

Each track lists: the **question**, **what to read/measure (no changes)**, the
**data that already exists vs. would need later instrumentation**, and what
counts as a **finding**.

### E1 · Context lifecycle & the scaling cliff (P3) — highest priority
- **Question:** How often, and under what conditions, do real runs cross 70%
  (compaction) and 80% (hard `CONTEXT_EXHAUSTED` 400)? Does truncation-only
  compaction preserve entropy (i.e., does the model keep re-deriving evicted
  context)? What is the realistic maximum task horizon today?
- **Read (no changes):** `src/luxe/context.py` (`estimate_tokens` ~9-10,
  `context_pressure` ~28-31, `elide_old_tool_results` ~34-62);
  `src/luxe/agents/loop.py:50,680` (`peak_context_pressure`) and the elide call
  site (~1326); `src/luxe/agents/outcomes.py:55,97,258-259`
  (`CONTEXT_EXHAUSTED`, `EMPTY_PATCH_CONTEXT_EXHAUSTED`).
- **Data that already exists:** the per-version taxonomy artifacts
  (`acceptance/v110_taxonomy/…`, `v1104`, `v1105`, `v111…` — each a `{rows:[…75
  dicts]}` with per-instance `outcome`, `interventions`, `failure_chain`).
  Count `CONTEXT_EXHAUSTED` / `EMPTY_PATCH_CONTEXT_EXHAUSTED` frequency across
  versions and reps to characterize cliff incidence on SWE-bench n=75 — entirely
  from recorded data. `peak_context_pressure` is computed in-loop per run; if it
  isn't persisted into the taxonomy rows, note that surfacing it later (not now)
  would give the full pressure *distribution* rather than just terminal events.
- **Design-space map (describe, don't build):** hierarchical summaries,
  scratchpad eviction, state distillation, explicit phase closure, resumable
  task state — and how each interacts with luxe's existing Track/Lever/Phase
  structure and the `run_state.py` checkpoint ledger (which already gestures at
  resumable state). Note the tension with luxe's byte-identicality/determinism
  norm (any future change would need an opt-in flag).
- **Finding looks like:** a characterization of the cliff (incidence + which
  task types hit it) plus a tradeoff menu of lifecycle strategies, scored
  against the systems-reliability lens — no implementation.

### E2 · Coordination model & the P5 reclassification
- **Question:** Is "single-agent serialized coordination via durable run state"
  the precise frame? What would a true shared-state coordination substrate buy
  luxe, and at what cost (distributed-agent pathologies)? Is the resume ledger
  already a partial shared-state primitive?
- **Read:** `src/luxe/run_state.py` (~1-100: `run.json`, `pr_state.json` step
  ledger, `events.jsonl`); `single.py:_build_sdd_block ~154-185` (`.sdd` chain
  broadcast as shared read-state); `luxe.sdd ~30-33` (retired swarm/micro/phased
  Forbids).
- **Data:** existing `events.jsonl` / `pr_state.json` from real runs show the
  serialization + resume boundaries concretely.
- **Finding:** a precise taxonomy of luxe's coordination model vs the wiki's,
  arguing why serialized + durable-state is the right reliability choice here.

### E3 · Intra-run prefix/KV cache reuse (G3) — feasibility spike (read-only)
- **Question:** Does oMLX/MLX reliably expose prefix/KV cache reuse for the
  stable system prompt + early history? How large is the per-step resend cost
  today (the bottleneck if every step re-sends full context)?
- **Read:** `src/luxe/backend.py` (the oMLX integration and `GenerationTiming
  ~29-39`); estimate resend cost from existing per-run `prompt_tokens`
  accumulation (`loop.py:47-48`). Check oMLX/MLX docs for prefix-cache surface.
- **Finding:** a feasibility memo (capability confirmed/denied + rough upside),
  explicitly *not* an implementation. Contingent on backend capability.

### E4 · Cost/token observability (G2) — tactical, lower priority
- **Question:** Given the existing Track 5 taxonomy, what budget/threshold
  signals would actually be useful, and where would they slot in?
- **Read:** `agents/outcomes.py` (taxonomy to extend); `loop.py:~1206-1220`
  (`[token-progress]`); `config.py:15-16` (budgets); `run_state.py` (event sink).
- **Finding:** a spec for telemetry to add *later* (50/75/90% of a per-task
  budget, plus a `max_tokens_per_task` cap) — design only.

### E5 · Instruction / contract drift (G4)
- **Question:** Do `CLAUDE.md` / `AGENTS.md` and the `.sdd` chain ever
  contradict, overlap, or silently override each other?
- **Read:** cross-read the two instruction docs against the `.sdd` contracts
  (`luxe.sdd`, `tools.sdd`, `agents.sdd`, `maintain_suite.sdd`) for conflicting
  or duplicated rules.
- **Finding:** a lightweight consistency-check methodology (what to compare,
  what a conflict looks like) that could later be automated as a lint.

---

## Cross-cutting lenses (apply to every track)
- **Systems-reliability vs cognitive-architecture:** prefer changes that
  strengthen determinism, replayability, bounded autonomy, and invariant
  enforcement over those that add planners/memory-graphs/reflection machinery.
- **Convergence through constraints (confirmed):** treat luxe's alignment with
  the wiki as evidence the operational primitives are forced by LLM physics, not
  fashion — and weigh deviations accordingly.
- **Benchmark hygiene as an asset:** preserve negative-result discipline and the
  "do not over-generalize from small/noisy deltas" norm; any exploration that
  proposes a change should also propose how it would be gated and how a null
  result would be recorded.

---

## Method & ground rules
- **Read-only only.** Source reading + analysis of existing artifacts
  (`acceptance/**`, `events.jsonl`, `summary.json`, `RESUME.md`, `lessons.md`).
- **No model runs, no new benchmarks, no experiments, no code changes** in this
  phase.
- For each track, explicitly separate **"data already recorded"** (usable now)
  from **"would require later instrumentation"** (flagged, not built).

## Explicitly out of scope (now)
- Implementing any gap (E1–E5 are investigations, not builds).
- Re-litigating the **deliberate divergences** — no-MCP-for-core (P4),
  mono-only (P5), and no model routing (P7) remain **valid principled choices**
  and are not treated as gaps.

---

## This session's scope (chosen)
Constraints: **no agent loaded, no interference with parallel luxe development
on the m5.** All reads against the local `/Users/michaeltimpe/Downloads/luxe`
checkout are read-only; all outputs land in
`/Users/michaeltimpe/Downloads/agentic-patterns-luxe-research/`.

**Doing now:**
1. **Save this document** to the working folder as
   `agentic-patterns-exploration-plan.md` so it persists for later.
2. **Execute E1 (context-cliff characterization)** as pure data analysis on
   recorded artifacts:
   - Enumerate `acceptance/v*_taxonomy/*.json` (and any sibling SWE-bench
     taxonomy files) across versions and reps.
   - For each, count rows with `outcome ∈ {CONTEXT_EXHAUSTED,
     EMPTY_PATCH_CONTEXT_EXHAUSTED}` and cross-tab with `failure_chain` heads
     and `interventions` fired.
   - Note whether `peak_context_pressure` is present in the row schema
     (earlier inspection showed it is not in v111 SWE-bench rows; flag as
     "would require later instrumentation" if absent).
   - Output: `e1-context-cliff-report.md` in the working folder, plus a small
     CSV (`e1-context-cliff-counts.csv`) for the per-version × per-outcome
     counts.
3. **Stop.** No code changes to luxe, no benchmarks run, no model loaded.

**Deferred to future sessions:** E2 (P5 reclassification), E3 (KV/prefix cache
feasibility), E4 (cost telemetry spec), E5 (doc/`.sdd` drift). All also
read-only when picked up.

---

## Appendix — baseline audit evidence map (self-contained reference)
Key file:line evidence behind the scorecard, so this doc stands alone:
- **P1:** `CLAUDE.md`, `AGENTS.md`; durable rules in `.sdd`.
- **P2:** `.sdd` chain resolved by `spec_resolver.py` (`find_all_sdd ~127-148`,
  `resolve_chain ~197-230`, `is_forbidden ~51-82`); write-time guards in
  `tools/fs.py` (placeholder ~78-88, role-path ~91-120, mass-deletion ~123-138,
  `_check_spec_forbids ~170-241`, invoked in `_write_file`/`_edit_file`
  ~344-395); citation lint `citations.py` + build-gate `cli.py:~305-323`; CI
  `.github/workflows/luxe-tests.yml`; tests `test_tools.py`, `test_sdd.py`.
- **P3:** `context.py:28-62`; `agents/loop.py:679-680,1326`.
- **P4:** native surface `agents/single.py:27-92`; dispatch `tools/base.py:76-130`;
  MCP opt-in `mcp/` (`bridge.py ~30-45`, injected `single.py ~102-122`).
- **P5:** `single.py:_build_sdd_block ~154-185`; `run_state.py ~1-100`;
  `luxe.sdd ~30-33`.
- **P6:** `agents/loop.py:676` (max_steps), exit gates ~1091/1117/1200/1489,
  pre-dispatch gate ~1259-1280, convergence ~689-724.
- **P7:** `backend.py:29-39`; `loop.py:47-48,1206-1220`; `config.py:15-16`;
  benchmark `summary.json` (`diagnostics.avg_tokens`/`avg_wall_s`).
- **P8:** `RESUME.md`, `lessons.md`, tags v1.0→v1.10.5, `maintain_suite` ≥8/10
  gate, SpecDD Lever 1→2 shipped / Lever 3 deferred.
- **Extensions:** `agents/prompts.py` (registry), `agents/reflect.py` (opt-in
  `LUXE_REFLECT`), `agents/cohort_priors.py` (log-only), `agents/outcomes.py`
  (Track 5 taxonomy).
