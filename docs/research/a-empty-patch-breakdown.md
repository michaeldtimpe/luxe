# A · empty_patch breakdown (cliff vs non-cliff)

E1 showed 80/324 SWE-bench `empty_patch`-tier rows are cliff (`EMPTY_PATCH_CONTEXT_EXHAUSTED`). This report characterizes the other **~75% slice** — the non-cliff `empty_patch` rows that the README's deferred v1.9 action_density gating work is meant to address.

**Total `tier=empty_patch` rows across all SWE-bench artifacts:** 324  ·  cliff: 80  ·  non-cliff: 244 (75.3%)

## Outcome distribution — non-cliff empty_patch rows

| outcome | count | share of non-cliff empty_patch |
|---|---|---|
| EMPTY_PATCH_TIMEOUT | 193 | 79.1% |
| STUCK_LOOP | 51 | 20.9% |


## Failure-chain heads — non-cliff empty_patch rows

| failure_chain head | count |
|---|---|
| BAILOUT_AFTER_READS | 125 |
| EARLY_PROSE_COLLAPSE | 53 |
| STUCK_LOOP | 51 |
| EMPTY_PATCH_TIMEOUT | 11 |
| CONFIDENCE_COLLAPSE_EXPLORATORY | 4 |


## Interventions fired — non-cliff empty_patch rows

- Rows with at least one intervention fired: **173/244 (70.9%)**

| intervention | occurrences |
|---|---|
| EARLY_BAIL | 143 |
| WRITE_PRESSURE | 100 |
| ACTION_DENSITY_GATE | 71 |


## Cliff slice (reference) — interventions fired

- Rows with at least one intervention fired: **20/80 (25.0%)**

| intervention | occurrences |
|---|---|
| EARLY_BAIL | 20 |


## `has_patch` / `patch_len` — non-cliff empty_patch rows

| has_patch | count |
|---|---|
| False | 231 |
| None | 13 |

- patch_len == 0: **219**  ·  patch_len > 0: **0** (rows with patch_len present: 219)

## Findings (TL;DR)

1. **Non-cliff `empty_patch` is dominated by `EMPTY_PATCH_TIMEOUT` (193 of 244 = 79.1%).** This is the `EMPTY_PATCH_TIMEOUT` slice the README's v1.9 action_density work targets.

2. **Intervention coverage differs sharply between cliff and non-cliff.** Cliff: 25.0% of rows had ≥1 intervention fire. Non-cliff: **70.9%** of rows had ≥1 intervention fire. The intervention machinery sees the non-cliff failures; the cliff is comparatively unsignaled.

3. **The two slices are addressed by different mechanisms.** Cliff (~25% of empty_patch): needs G1 graceful context lifecycle — no in-loop intervention can rescue a backend prompt-size 400. Non-cliff (~75%): action_density gating + the existing intervention stack (early_bail / write_pressure / prose_burst) is the right surface, and the data shows that surface is already firing on most of these rows but not converting them. The lever is intervention **conversion**, not intervention **coverage**.

