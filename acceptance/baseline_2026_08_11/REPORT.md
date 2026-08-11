# maintain_suite baseline — 2026-08-11

**Run:** 07:08→07:36, m5, `Qwen3.6-35B-A3B-6bit`, 3 reps × 10 fixtures = 30
runs at `f1b3a90`. **No env overrides** — this measures the shipped defaults.
Variants: `benchmarks/maintain_suite/variants_baseline_3rep.yaml`.

## 30/30 · 120/150 · release gate true on all three reps

```
isomer-document-quickstart              3/3  [4,4,4]
isomer-implement-healthcheck            3/3  [4,4,4]
lpe-rope-calc-document-typing           3/3  [4,4,4]
lpe-rope-calc-implement-strict-flag     3/3  [4,4,4]
neon-rain-document-modules              3/3  [4,4,4]
neon-rain-implement-reset-shortcut      3/3  [4,4,4]
nothing-ever-happens-document-config    3/3  [4,4,4]
nothing-ever-happens-manage-deps-audit  3/3  [4,4,4]
the-game-document-architecture          3/3  [4,4,4]
the-game-implement-shuffle-shortcut     3/3  [4,4,4]
```

avg wall 53.3s · avg tokens 162,385 · **0 harness errors, no PRError**.

Defaults in effect: `truncated_turn_retry=True`, `tiered_compact=True`,
`post_write_idle_repeats=False`.

## What this run was actually checking

Not a formality. It is the first full pass since three changes that could each
have broken the suite in the same way — as `PRError` failures indistinguishable
from model regressions:

1. the one-off prune of 619 branches,
2. the harness self-prune now running before every fixture,
3. `branch_prefix` restored to `luxe` after the `pwir2` workaround.

All three are clean: zero harness errors, and this ran against the real `luxe/*`
namespace that the last valid A/B deliberately sidestepped.

The self-prune fired **36 times** during the run and the backlog is holding at
the retention window:

| fixture-cache repo | worst slug |
|---|---|
| isomer | 26 / 99 |
| lpe-rope-calc | 26 / 99 |
| neon-rain | 26 / 99 |
| the-game | 26 / 99 |
| nothing-ever-happens | 9 / 99 |

26 = the 25 kept plus this run's new branch, which is the design working.
`nothing-ever-happens` sits lower because it was also used to test the prune at
`LUXE_BENCH_BRANCH_RETENTION=5`.

## Agreement with prior runs

| run | passed | score | wall | tokens |
|---|---|---|---|---|
| truncated_turn A/B, treatment (forced) | 30/30 | 120/150 | 53.6s | 161,948 |
| truncated_turn confirmation (default) | 30/30 | 120/150 | 53.4s | 161,904 |
| pwir re-run, baseline arm | 30/30 | 120/150 | 53.3s | 162,384 |
| **this baseline** | **30/30** | **120/150** | **53.3s** | **162,385** |

Four independent 30-run passes agree on score exactly and on wall within 0.3s.
Worth carrying forward: maintain_suite reproduces far more tightly on the
champion than the temp=0 non-determinism note implies — that note came from
SWE-bench pylint fixtures. Smaller effects are detectable here than the
±2-patch noise band suggests, which matters when designing the next A/B.

## Standing baseline

**30/30 · 120/150 on `Qwen3.6-35B-A3B-6bit`** — matching the m5max_moe
bake-off level, now on shipped defaults with a self-pruning harness.
