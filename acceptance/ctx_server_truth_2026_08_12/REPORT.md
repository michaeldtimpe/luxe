# LUXE_CTX_SERVER_TRUTH acceptance — 2026-08-12

**Run:** 09:40→10:07, m5, `Qwen3.6-35B-A3B-6bit`, 3 reps × 10 fixtures = 30
runs, tree at `7eaceaa` (+ uncommitted parallel work, see caveats). **No env
overrides** — the treatment is the shipped default from `967124d`.
Variants: `benchmarks/maintain_suite/variants_ctx_server_truth_3rep.yaml`
(promotion criteria pinned there before launch).

## Verdict: HOLD — 30/30 · 120/150 · release gate true on all three reps

Criterion 1 met exactly. **The server-truth calibration debt is retired:
`LUXE_CTX_SERVER_TRUTH` stays default-ON.** No attribution arm was needed
(criterion 2 never triggered — zero failures).

| run | passed | score | avg wall | avg tokens |
|---|---|---|---|---|
| four pre-change references (2026-08-10/11) | 30/30 | 120/150 | ~53.4s | ~162.0k |
| **this run (server-truth ON)** | **30/30** | **120/150** | **46.8s** | **123.5k** |

Per rep: 46.0s / 45.6s / 49.0s wall; 119.9k / 124.4k / 126.2k tokens; zero
harness errors, zero bailouts, zero microstep rejects; the standard "6
fixtures draft-PR at open" profile, same as every reference.

## The mechanism, visible in telemetry

Same score, **12% less wall, 24% fewer tokens** — and the compaction events
explain it (per-run `events.jsonl`, `compaction_phase_reached`):

| corpus (30 runs) | firings | phase 1 | phase 3 | tool results dropped |
|---|---|---|---|---|
| baseline_2026_08_11 (old reading) | 75 | 69 | 6 | 1,182 |
| this run (server truth) | 111 | 90 | 21 | 507 |

Compaction fires **more often and reaches the deep phases it could never
reach before** (the commit's diagnosis: under the 2-3.7×-low estimate,
phases 2/3 sat beyond the server's real window) — yet it drops **fewer than
half the tool results**, because compacting early keeps prompts from
ballooning into the huge late-stage purges the old reading forced. Earlier,
smaller, cheaper: that is the intended correction working, not a side
effect.

`lpe-rope-calc-implement-strict-flag` remains the slow fixture (~165-175s):
that is the truncated-turn-retry replay cost documented in
`acceptance/truncated_turn_ab_2026_08_10/`, unchanged here.

## Caveats (recorded, none load-bearing)

1. **Tree drift mid-run.** This bench overlapped with parallel fix work in
   the same working tree. Reps 1-2 ran pre-edit; rep 3's fixtures may have
   picked up (a) the `LUXE_TOOL_BUDGET_CTX` wiring in `maintain.py` — inert
   when the flag is unset, which it was — and (b) partial tool-surface
   fixes designed byte-identical on success paths. Rep agreement is tight
   (walls within 3.4s, all 10/10 at score 4), so no rep-3 anomaly exists,
   but a **3-rep confirmation on the settled tree** runs next (doubling as
   the tool-budget A/B baseline). If it reproduces 30/30 · 120/150, this
   caveat closes.
   **CLOSED same day:** `acceptance/toolbudget_ab_2026_08_12/baseline/`
   (settled tree, all fixes + wiring committed to the working set, no env)
   reproduced **30/30 · 120/150 · 46.8s avg wall** — wall agreeing with this
   run to the decimal, tokens within 2% (121.3k vs 123.5k).
2. **The de94e7a confound never needed resolving.** This tree also carries
   the grep fix + announced tool limits. Attribution would only have
   mattered on a failure; on a HOLD with a token *improvement* the
   compaction telemetry above ties the delta to the calibration directly.
3. Criterion 3 (>1.5× shift) did not trigger: 0.76× tokens, 0.88× wall —
   and in the favorable direction; the phase-level inspection above was done
   anyway.

## Standing baseline

**30/30 · 120/150 on shipped defaults** (`ctx_server_truth=True`,
`truncated_turn_retry=True`, `tiered_compact=True`), now at **~47s / ~124k
tokens** per fixture average — the cheapest passing profile the suite has
recorded.
