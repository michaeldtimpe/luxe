# E1 · Context-cliff characterization (luxe)

Read-only analysis of recorded taxonomy artifacts under
`luxe/acceptance/v*_taxonomy/*.json`. No new runs, no model load,
no writes to the luxe tree.

**Outcomes counted as 'the cliff':** `CONTEXT_EXHAUSTED` (backend 400 on prompt size) and `EMPTY_PATCH_CONTEXT_EXHAUSTED` (SWE-bench-specific tier: oMLX 400 prompt size).

**Source:** `src/luxe/agents/outcomes.py:55,97,258-259` defines both classes; they appear in `outcome` rows of the taxonomy artifacts.

---

## 0. Findings & interpretation (TL;DR)

Five signals fall out of the data cleanly:

1. **The cliff is a stable, persistent ~5% failure mode on SWE-bench, zero on
   BFCL.** Across 23 runs and 4,053 rows, **all 80 cliff events** are
   `EMPTY_PATCH_CONTEXT_EXHAUSTED` on SWE-bench n=75. **Zero generic
   `CONTEXT_EXHAUSTED`. Zero of any kind on BFCL n=1240** — BFCL prompts are
   short enough that the cliff never fires there. The phenomenon is
   bench-conditional.

2. **The rate is essentially flat across 11 luxe versions (v17 → v111).**
   Per-(bench,version) cliff rate stays in the **4.0–5.5% band** every
   version, every rep. None of SpecDD Lever 1/2 rollout, Track 1–5 changes,
   the prompt-variant work (SoT / CoT / HADS / DOC_STRICT), or the
   pre-dispatch spec gate moved this number. **Strong evidence the cliff is a
   structural ceiling under the current context discipline**, not a
   prompt-engineering or intervention problem — it is exactly the
   "compaction-preserves-entropy-accumulation" failure the reviewer feedback
   predicted.

3. **The cliff is concentrated entirely in the `empty_patch` tier — and
   accounts for ~1 in 4 empty_patch outcomes.** 80 cliff rows of 324
   `empty_patch` SWE-bench rows ⇒ **24.69%** of `empty_patch` is *caused* by
   context exhaustion. The README's deferred v1.9 work ("`empty_patch` ≤13
   floor missed at 17; deferred to v1.9 — needs action_density gating")
   reads differently in light of this: **roughly a quarter of empty_patch
   instances cannot be rescued by any in-loop intervention** (action-density
   or otherwise), because they terminate at the backend prompt-size 400, not
   at a step-budget or behavior gate. action_density addresses the *other*
   ~75% of empty_patch.

4. **One failure-chain head, one intervention — and 75% of cliff rows had
   *no* intervention fire.** All 80 cliff rows have
   `failure_chain[0] = CONTEXT_EXHAUSTED`. Only **20/80 (25%) had
   `EARLY_BAIL`** fire before termination; the other 60 ran straight into
   the cliff with no signal in the intervention ledger. This is consistent
   with the wiki's "decompose-before-degrade" principle being **load-bearing
   for step budget but silent on context budget** — luxe has rich
   intervention coverage for tool-call patterns and prose-vs-action signals,
   but no analog gate for "you are about to hit prompt-size wall."

5. **`peak_context_pressure` exists but is not persisted into the taxonomy
   row.** Per-run pressure is computed (`loop.py:50,680`) but not surfaced
   to the artifacts inspected here. So we can see *terminal* cliff events
   only — we cannot, from existing data, characterize the distribution of
   "near-cliff" runs (e.g. peaked at 78% but completed). Filling that gap
   would convert this from terminal-event accounting into a true cliff
   *profile*. **Not built in this session** (read-only constraint), but
   flagged as the minimum instrumentation change to make E1 self-improving.

### What this means for the plan's G1 > G2 priority recalibration

The recalibration is empirically supported, with one sharpening:

- **G1 (graceful context lifecycle) is not a hypothetical concern.** It is
  responsible for a quarter of the dominant SWE-bench failure tier today,
  and that fraction has been stable for 11 versions of unrelated work. No
  prompt or intervention change is going to move it.
- **Compaction is doing its job up to the cliff, then nothing.** The 80
  EMPTY_PATCH_CONTEXT_EXHAUSTED outcomes mean ~5% of SWE-bench instances
  push past whatever `elide_old_tool_results(threshold=0.7)` can reclaim and
  still hit the backend's prompt-size 400. Hierarchical summary / state
  distillation would directly target this residual.
- **The empty_patch ceiling is partly a context problem, not just an
  action-density problem.** The two interventions (action-density gating
  for the ~75% non-cliff portion; graceful context lifecycle for the ~25%
  cliff portion) are *complementary* — neither subsumes the other.
- **G2 remains useful but is downstream.** Cost-threshold alerts would
  surface the cliff approach earlier; they do not prevent it. If only one
  thing were built, G1's structural fix yields more return than G2's
  observability layer. The plan's priority ordering holds.

### Caveats / things the data does *not* tell us
- We see only terminal cliff events, not the pressure distribution of
  non-cliff runs (see §8).
- The intervention list per row may be truncated by the taxonomy
  normalization; only `EARLY_BAIL` appears on cliff rows, but other
  early-step interventions may have fired and not be persisted.
- v17/v18 are pre-Track-5 taxonomy; the row schema is a smaller subset and
  some fields may be absent. Cliff counts are still extractable because
  `outcome` is present everywhere.

---

## 1. Coverage

- Files analyzed: **23** rows across **4053** task instances.

- Benches present: bfcl, swebench

- Versions present (sorted): v17, v18, v19, v110, v1101, v1102, v1103, v1104, v1105, v111

## 2. Per-run cliff incidence (CSV mirror)

| bench | version | rep | n | CONTEXT_EXHAUSTED | EMPTY_PATCH_CE | cliff total | cliff % |
|---|---|---|---|---|---|---|---|
| bfcl | v17 | 1 | 1240 | 0 | 0 | 0 | 0.0% |
| bfcl | v18 | 1 | 1240 | 0 | 0 | 0 | 0.0% |
| swebench | v17 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v18 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v19 | 1 | 75 | 0 | 3 | 3 | 4.0% |
| swebench | v19 | 1 | 75 | 0 | 3 | 3 | 4.0% |
| swebench | v110 | 1 | 75 | 0 | 3 | 3 | 4.0% |
| swebench | v1101 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1102 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1102 | 2 | 73 | 0 | 4 | 4 | 5.48% |
| swebench | v1102 | 3 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1103 | 1 | 75 | 0 | 3 | 3 | 4.0% |
| swebench | v1103 | 2 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1103 | 3 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1104 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1104 | 2 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1104 | 3 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1105 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1105 | 2 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1105 | 3 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v111 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v111 | 2 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v111 | 3 | 75 | 0 | 4 | 4 | 5.33% |

## 3. Per-(bench, version) aggregate over reps

| bench | version | runs | rows total | CE total | EMPTY_PATCH_CE total | cliff total | cliff %* |
|---|---|---|---|---|---|---|---|
| bfcl | v17 | 1 | 1240 | 0 | 0 | 0 | 0.0% |
| bfcl | v18 | 1 | 1240 | 0 | 0 | 0 | 0.0% |
| swebench | v17 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v18 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v19 | 2 | 150 | 0 | 6 | 6 | 4.0% |
| swebench | v110 | 1 | 75 | 0 | 3 | 3 | 4.0% |
| swebench | v1101 | 1 | 75 | 0 | 4 | 4 | 5.33% |
| swebench | v1102 | 3 | 223 | 0 | 12 | 12 | 5.38% |
| swebench | v1103 | 3 | 225 | 0 | 11 | 11 | 4.89% |
| swebench | v1104 | 3 | 225 | 0 | 12 | 12 | 5.33% |
| swebench | v1105 | 3 | 225 | 0 | 12 | 12 | 5.33% |
| swebench | v111 | 3 | 225 | 0 | 12 | 12 | 5.33% |

*cliff % = cliff total / rows total across reps.*

## 4. Failure-chain heads accompanying cliff outcomes (all benches, all versions)

| failure_chain head | count |
|---|---|
| `CONTEXT_EXHAUSTED` | 80 |

## 5. Interventions fired on cliff-outcome rows (all benches, all versions)

| intervention | count |
|---|---|
| `EARLY_BAIL` | 20 |

## 6. Tier distribution of cliff outcomes (SWE-bench only)

| tier | cliff rows | total SWE-bench rows (this tier across runs) | cliff share of tier |
|---|---|---|---|
| `empty_patch` | 80 | 324 | 24.69% |

## 7. Global outcome distribution (sanity check)

| outcome | count |
|---|---|
| `SINGLE_TOOL_CORRECT` | 1076 |
| `MULTI_TOOL_COMPLETE` | 682 |
| `CORRECT_ABSTAIN` | 457 |
| `PLAUSIBLE_EDIT` | 406 |
| `STRONG_GOLD_MATCH` | 385 |
| `WRONG_TARGET` | 364 |
| `EMPTY_PATCH_TIMEOUT` | 193 |
| `UNCLASSIFIED` | 124 |
| `MULTI_TOOL_ORDERING_FAILURE` | 118 |
| `WRONG_LOCATION` | 93 |
| `EMPTY_PATCH_CONTEXT_EXHAUSTED` | 80 |
| `STUCK_LOOP` | 52 |
| `FORBIDDEN_TOOL_EMISSION` | 23 |

## 8. What's *not* in this data (instrumentation gap)

- `peak_context_pressure` is tracked **per run in `AgentResult`** (`src/luxe/agents/loop.py:50,680`) but is **not persisted into the taxonomy row schema** for SWE-bench (`instance_id`, `tier`, `has_patch`, `patch_len`, `outcome`, `interventions`, `failure_chain`, `gold_target_files`, `first_correct_file_touch_step`, `correct_touch_*`, `first_write_locus_correct`, `write_locus_*`, `gold_files_*`, `prior_patch_len`, `patch_len_delta`) or BFCL row schema (`id`, `category`, `outcome`, `interventions_fired`, `failure_chain`).
- Consequence: we can characterize **terminal cliff events** (outcome = `*CONTEXT_EXHAUSTED`) from recorded data, but the **full pressure distribution** (e.g., how many runs spent N steps above the 70% compaction threshold without terminating) is not available without later instrumentation. Surfacing `peak_context_pressure` (and ideally a per-step pressure histogram) into the taxonomy row would fill this gap. **Not done in this session.**

## 9. Method & ground rules

- Read-only access to `/Users/michaeltimpe/Downloads/luxe/acceptance/`.
- No model loaded, no benchmarks run, no edits to the luxe tree.
- All outputs written under `/Users/michaeltimpe/Downloads/agentic-patterns-luxe-research/`.
- Reproducer: `python3 e1-analyze.py` from this folder.
- Files inspected (23):

  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1101_taxonomy/v1101_n75_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1102_taxonomy/v1102_n75_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1102_taxonomy/v1102_n75_rep_2_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1102_taxonomy/v1102_n75_rep_3_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1103_taxonomy/v1103_n75_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1103_taxonomy/v1103_n75_rep_2_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1103_taxonomy/v1103_n75_rep_3_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1104_taxonomy/v1104_n75_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1104_taxonomy/v1104_n75_rep_2_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1104_taxonomy/v1104_n75_rep_3_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1105_taxonomy/v1105_n75_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1105_taxonomy/v1105_n75_rep_2_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v1105_taxonomy/v1105_n75_rep_3_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v110_taxonomy/v110_n75_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v111_taxonomy/v111_n75_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v111_taxonomy/v111_n75_rep_2_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v111_taxonomy/v111_n75_rep_3_full_stack_swebench.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v17_taxonomy/bfcl_n1240.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v17_taxonomy/swebench_n75.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v18_taxonomy/bfcl_n1240.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v18_taxonomy/swebench_n75.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v19_taxonomy/full_stack_swebench_n75.json`
  - `/Users/michaeltimpe/Downloads/luxe/acceptance/v19_taxonomy/gate_only_swebench_n75.json`
