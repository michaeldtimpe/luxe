# Qwen3.8-27B elimination bake-off — REPORT

**Date:** 2026-08-17/18 (overnight) · **Host:** m5 (M5 Max, 128 GB) · **Substrate:** commit `1bcc325`, server-truth ctx calibration ON, tiered compact ON, truncated-turn retry ON
**Arms:** `Qwen3.6-27B-6bit` (incumbent thick fallback, fresh reference) vs `mlx-community/Qwen3.8-27B-{4bit, 8bit, bf16}` (converted via mlx-vlm 0.6.8, uploaded 2026-08-14)
**Apparatus:** maintain_suite, 10 fixtures, temp=0, `--per-fixture-timeout 1800`, pinned work-dir. Offline max = 4/5 per fixture (`pr_opened` can't fire). Run-id manifest: `acceptance/qwen38_27b_run_id_manifest.tsv`.
**Data:** `acceptance/qwen38_27b_r1_2026_08_17/` (full 4×10), `acceptance/qwen38_27b_r2_rep2/`, `_rep3/` (targeted reps: 6 doc/manage fixtures × 3 surviving arms). All local to m5.

## Verdict

**Qwen3.6-27B-6bit keeps the fallback slot. Qwen3.8-27B is not promoted at any quant.**

- **Ground lost:** (1) the suite's hardest fixture, `nothing-ever-happens-document-config`, regresses badly — 4bit fails it 3/3, 8bit passes only 1/3, bf16 times out on it; the incumbent is 3/3 clean. (2) Wall time: 2.3–3.8× the incumbent on average (reasoning-on inflates output 2–4×).
- **Ground gained:** raw decode speed at 4bit (26–30 tok/s vs 16–21) — but reasoning verbosity spends the gain, so wall still loses. Off-bench (unmeasured here): 262K native ctx, newer knowledge cutoff, hybrid linear attention.

## Round 1 — full suite (1 rep × 10 fixtures × 4 arms)

| Arm | Pass | Avg wall | Avg out tok | Gen tok/s | Notes |
|---|---|---|---|---|---|
| Qwen3.6-27B-6bit (ref) | **10/10** | 182.5s | 3.0k | 16.3 | clean sweep on today's substrate |
| Qwen3.8-27B-4bit | 9/10 | 430.6s | 11k | 25.4 | FAIL doc-config |
| Qwen3.8-27B-8bit | **10/10** | 421.3s | 6.8k | 16.1 | — |
| Qwen3.8-27B-bf16 | 9/10 +1 ERR | 696.6s | 6.1k | 8.7 | timeout on doc-config |

**Elimination after R1: bf16.** Dominated by 8bit — 2× memory (51 vs 27.5 GB), ~half the decode speed, worse outcome. Forensics note: bf16 was *succeeding* on doc-config (well-formed 110-line CONFIG.md written at step 6, refined at 13/18) and was killed 3s past the 1800s cap — a speed death, not a quality one.

## Round 2 — targeted reps (6 doc/manage fixtures × 3 arms × 2 more reps)

The 5 uncontested doc/manage fixtures passed **every arm, every rep** (45/45 cells outside doc-config). All variance concentrates in one cell:

### `nothing-ever-happens-document-config` across 3 reps

| Arm | R1 | Rep2 | Rep3 | Rate |
|---|---|---|---|---|
| Qwen3.6-27B-6bit | PASS 335s | PASS 327s | PASS ~330s | **3/3** |
| Qwen3.8-4bit | FAIL 763s | FAIL 1301s | FAIL 908s | **0/3** |
| Qwen3.8-8bit | PASS 614s | FAIL 1772s | ERROR (1800s timeout) | **1/3** |

## Failure mechanism (forensics on runs `e80eec6c5182`, `57b0c805025a`, vs passing `9220096b9a8c`)

Every Qwen3.8 miss has one signature: **write-avoidant exploration**. 30–48 tool calls, ~35 reads, 300–520k cumulative prompt tokens, **zero writes**, run dies on max-steps (or the 1800s cap). Peak context pressure never exceeded 74.6% — the window never overflowed; compaction stayed at phase 1 throughout. The `write_pressure` intervention fires (step ~7) and **bounces off Qwen3.8** in every failing cell, while the same intervention immediately precedes the write in passing cells. This is the pre-v1.4.1 "Mode B" failure class resurrected: the incumbent needed write-pressure threshold tuning to clear this fixture, and that tuning is calibrated to Qwen3.6's response to the nudge, not Qwen3.8's.

Reading of the quant gradient: bf16 converges to a write (slowly), 8bit sometimes, 4bit never — quantization degrades the model's already-marginal decisiveness on this fixture. It is simultaneously a *family* trait (even bf16 is slow/late to write vs the incumbent's 330s clean runs) and *quant-amplified*.

## Harness bug found (open, small)

`bailout_type="context_overflow"` is a mislabel: the classifier (`benchmarks/maintain_suite/run.py:866`) string-matches "max steps" in `abort_reason`. Cell A peaked at 74.6% context — no overflow occurred. Deserves a rename/split (`max_steps` vs measured overflow) + test; not fixed mid-bench.

## Standing decisions / re-open triggers

1. **Fallback manifests unchanged** (m5 = 27B-6bit, m1/m4 = 27B-4bit remain Qwen3.6). Champion `Qwen3.6-35B-A3B-6bit` was never in question — Qwen3.8 has no MoE at runnable scale (only 2.4T-A95B).
2. **Weights retention:** suggest keeping 8bit (27.5 GB) for opt-in `/model` sessions (262K ctx is a real capability), dropping bf16 (51 GB, no role now that the quality question is answered), 4bit optional. User call — nothing removed.
3. **Re-open if:** (a) an official/community **6bit** MLX quant lands (true like-for-like vs the incumbent pin); (b) oMLX exposes **thinking-off** for Qwen3.8 (most of the wall gap is reasoning tokens; a no-think arm is a cheap, high-value rebench); (c) write-pressure thresholds are ever retuned per-model — the intervention provably doesn't land on this family as configured.
