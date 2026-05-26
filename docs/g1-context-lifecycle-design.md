# G1 — Graceful context lifecycle (design doc, not implementation)

**Status:** planning artifact. No code change. No Track/Lever submission.
No re-bench commitment. A future cycle picks a lever from the menu below
and gates it behind the existing default-off opt-in pattern.

**Empirical basis:** the 2026-05-25 agentic-patterns audit
(`docs/research/`). Specifically:
- `docs/research/e1-context-cliff-report.md` — the cliff incidence.
- `docs/research/a-empty-patch-breakdown.md` — the cliff/non-cliff split.
- `docs/research/f-intervention-effectiveness.md` — why the existing
  intervention machinery is silent on the cliff slice.

---

## 1. Problem statement

The SWE-bench `EMPTY_PATCH_CONTEXT_EXHAUSTED` outcome accounts for **80
of 324 (24.69%) of the `empty_patch` tier** and **~5% of the full bench**.
The rate is **flat across 11 luxe versions** (v17 → v111); none of the
prompt-variant work, the SpecDD Lever rollout, the Track 1–5 changes,
nor any of the existing interventions moved it. BFCL is unaffected.

The intervention machinery sees the **non-cliff** empty_patch slice (71%
of those rows have at least one intervention fire) but is **almost silent
on the cliff slice** (only 25% have any intervention fire — and only
`EARLY_BAIL` ever appears). The cliff terminates the run on a backend
prompt-size 400, not on any in-loop predicate.

This is a **structural context-budget ceiling**, not an instruction-
following or prompt-engineering problem. The existing compaction primitive
(`elide_old_tool_results`) does its job up to ~70% pressure and then has
no further signal to give. The next ~25% of empty_patch wins live behind
some form of graceful context lifecycle.

---

## 2. Substrate map (existing primitives)

File:line references are advisory snapshots as of commit `649e9dc`.

### Token estimation and pressure

- `src/luxe/context.py:9–10` — `estimate_tokens` (4-char/token).
- `src/luxe/context.py:28–31` — `context_pressure(messages, ctx_limit)`
  returns a float 0.0–1.0 ratio. `ctx_limit` is `role_cfg.num_ctx`, set
  per-config in `configs/single_64gb.yaml`.

### Elision (the only existing context lever)

- `src/luxe/context.py:34–62` — `elide_old_tool_results` truncates with a
  deterministic stub (`[elided: {tool_name} → {size_bytes} bytes]`) when
  pressure ≥ 0.7. Keeps last 4 tool messages intact. **Strategy: drop +
  stub.** No summarization, no hierarchical state, no semantic memory.

- `src/luxe/agents/loop.py:≈1223` — elide is called **every step** before
  `backend.chat`. The threshold gating lives inside the elision function,
  not at the call site.

### Cliff classification (backend-reactive)

- `src/luxe/agents/outcomes.py:55, 97, 256–261` — `CONTEXT_EXHAUSTED` and
  `EMPTY_PATCH_CONTEXT_EXHAUSTED` are classified from the `abort_reason`
  text on a backend 400. There is **no proactive pressure-threshold trip
  in the loop** — the loop sends messages, oMLX rejects oversize, the
  loop catches the exception and re-classifies. The cliff is detected
  after the fall.

### Run state / event ledger (candidate phase-closure substrate)

- `src/luxe/run_state.py` — three persistent artifacts: `run.json`
  (`RunSpec`, immutable), `pr_state.json` (step ledger, updated per
  PR step), `events.jsonl` (append-only event log).
- The `PRState.steps` list is the closest existing analog to a "phase
  closure" boundary; `events.jsonl` already captures step-wise snapshots
  by `kind`. Nothing currently truncates or summarizes either.

### Convergence and exit gates (none triggered by pressure)

- `src/luxe/agents/loop.py` — exit gates at ≈1486 (post-write-idle),
  ≈1506 (stuck-loop), ≈1139 (habituation), ≈1113 (prose-burst clean),
  ≈1523 (max-steps). All are **action-pattern** gates; none consult
  `context_pressure`.

### Pre-dispatch spec gate (the shape a budget pre-flight would take)

- `src/luxe/agents/loop.py:≈1296–1328` — the SpecDD Lever 1 pre-dispatch
  gate. Validates `spec.expects_zero_calls`; on violation, drops the
  call, injects a decline reprompt, continues the loop. **A "budget
  pre-flight" hook in the same shape is a small change** — same
  checkpoint, same skip-and-reprompt pattern, gated on a new predicate
  or env var.

### Backend prompt-size limit (no pre-query)

- `src/luxe/backend.py:≈204` — oMLX HTTP 400 surfaced as a `BackendError`.
  The context window is **not queryable** via `/v1/models`. The loop can
  only react.

### Persisted telemetry (now includes peak_context_pressure)

After commit `649e9dc`, `Diagnostics.peak_context_pressure` and the
`single_mode` event payload carry per-run peak pressure. This is the
substrate any future lever would use to characterize *near*-cliff runs
without rerunning the bench.

---

## 3. Lever menu

Six candidate strategies, mapped to specific tie-in points. **Not
ranked.** A future cycle picks one and ships it behind a default-off
env flag.

| Lever | Checkpoint | Tie-in | Risk |
|---|---|---|---|
| Graduated elision | `context.py:34–62` / `loop.py:≈1223` | Replace the single 0.7 threshold with escalating-aggressiveness bands (0.65 → 0.75 → 0.85); each band drops fewer keep-recent messages or reduces the stub more. | Low. Reuses the existing primitive. Deterministic. |
| Pre-flight budget gate | `loop.py:≈1296–1328` shape | New predicate or env-gated check inside the pre-dispatch block: if projected next-step `context_pressure ≥ θ`, skip dispatch and inject a "summarize-and-close" reprompt. | Low–medium. Tight analog to the existing spec gate. |
| Explicit phase closure | `loop.py:≈687` (step iteration) + `run_state.py` | Emit a `phase_checkpoint` event at convergence-score inflection or after the first successful write; allow the loop to declare "everything before this checkpoint can be summarized." | Medium. Requires a robust "what's the phase boundary?" signal. |
| Hierarchical summary | `loop.py:≈1223` (elide call) | Replace the stub with a deterministic per-message distillation (truncate-then-hash, or function-signature extraction for code tool results). Larger keep-recent budget downstream. | Medium–high. The "deterministic" constraint is real — see §4. |
| State distillation | `loop.py:≈690/700` | Maintain a parallel "distilled state" view of tool history; pressure calculation factors in the distilled size, not raw. | High. Cross-cuts the existing context_pressure abstraction. |
| Resumable task state | `run_state.py` event ledger | At phase boundary, checkpoint `messages` + `tool_history` to disk; loop can reset its in-memory view to a smaller reconstruction. | High. Largest complexity jump; touches the resume primitive. |

---

## 4. Invariant constraints (any lever must respect)

From `src/luxe/luxe.sdd` and `src/luxe/agents/agents.sdd`:

- **`temp=0.0` in production fixture configs.** No LLM-based summarizer
  is acceptable unless explicitly designated and gated; preference for
  **deterministic compressors** (hash-based, structural extraction,
  truncation). An LLM-summarizer lever would have to opt in *via the
  same disable-equivalence pattern as `LUXE_REFLECT`* and prove that
  byte-identicality holds when off.
- **Disable-equivalence.** Any new lever ships behind an env flag
  (`LUXE_CONTEXT_*` style) with a byte-identical default-off path,
  matching the existing reflect / adaptive-policy / cohort-priors
  pattern (see `CLAUDE.md` § Opt-in modes).
- **Mono-only.** No per-task routing, no model fan-out. Levers operate
  on the single champion (`Qwen3.6-35B-A3B-6bit`).
- **Bias-not-lock.** If a lever modulates an intervention's intensity
  rather than the elision itself, it must respect the v1.11 invariants
  — see `agents.sdd` § "Stage 3 / v1.11 adaptive-policy invariants".
- **Bench-as-truth.** Any lever proposal must include how it would be
  gated (env-var name + default), and how a null result would be
  recorded — without re-running a bench you can't separate noise from
  the cliff slice (~4 rows / 75 = ±5% per single rep).

---

## 5. Open questions for whichever cycle picks this up

1. **Which lever first?** Three plausible openings:
   - **Graduated elision** is the cheapest experiment. Same primitive,
     small diff, deterministic by construction.
   - **Pre-flight budget gate** is the closest behavioral analog to the
     existing pre-dispatch spec gate; the harness shape already exists.
   - **Hierarchical summary** has the highest theoretical upside but
     blocks on a deterministic compressor that's good enough to recover
     genuinely useful state. Most likely to need a vendored heuristic
     (AST-shape extraction? tool-result schema compression?).

2. **Does graduated elision change the cliff rate?** Could be tested with
   a 75-rep SWE-bench A/B using the new `peak_context_pressure`
   instrumentation to characterize near-cliff runs even when the cliff
   doesn't fire.

3. **Is there a "phase boundary" signal already in the data?** The
   convergence-score deque and `pr_state.json` step ledger may already
   carry enough signal to mark "everything before here is summarizable"
   without a new gate.

4. **What's the actual oMLX context window?** Confirming the budget
   reference would let a pre-flight gate be exact rather than heuristic.

5. **Does the existing elision underreport pressure?** If elide replaces
   N bytes with a 40-byte stub, `peak_context_pressure` continues to
   compute against the *current* (post-elision) message size. A
   "near-cliff but elided-out" run may show low peak pressure but be
   one step away from re-accumulating. Worth measuring before tuning
   thresholds.

---

## 6. Explicit non-goals

- This doc does **not pick a lever**, propose a build, or commit to a
  re-bench.
- It does **not re-litigate** the deliberate divergences from the wiki's
  cognitive-architecture direction — single-agent serialized
  coordination, native-tools-not-MCP, mono-only, no model routing — those
  remain valid principled choices per the audit.
- It does **not propose** instrumentation beyond what already shipped in
  commit `649e9dc` (`peak_context_pressure` persisted).
- It does **not** specify the eventual env-var name; the implementing
  cycle owns naming.

---

## 7. References

- `docs/research/e1-context-cliff-report.md` — cliff incidence.
- `docs/research/a-empty-patch-breakdown.md` — cliff vs non-cliff slices.
- `docs/research/f-intervention-effectiveness.md` — why the intervention
  stack is silent on the cliff slice.
- `docs/research/agentic-patterns-exploration-plan.md` — full audit
  scope, including E2–E5 tracks that are out of scope for G1.
- `CLAUDE.md` § Opt-in modes — the pattern any new lever follows.
- `src/luxe/agents/agents.sdd` — disable-equivalence and bias-not-lock
  invariants for opt-in stages.
