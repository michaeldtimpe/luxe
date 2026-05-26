# F · intervention effectiveness

Cross-tabs **outcome distribution** by **intervention class fired** across all 4,053 taxonomy rows (SWE-bench n=75 × 17 runs + BFCL n=1240 × 2 runs). Asks: when an intervention fires, does the row land on a 'good' outcome more often than the no-intervention baseline?

Good outcomes (treated as success-like for this analysis): `CORRECT_ABSTAIN`, `MULTI_TOOL_COMPLETE`, `PLAUSIBLE_EDIT`, `SINGLE_TOOL_CORRECT`, `STRONG_GOLD_MATCH`.

**Important caveat:** intervention firing is *not random* — interventions fire because a stall/loop/prose-burst is already detected. So an intervention-fires row is a **higher-risk** row to begin with. A lower good-rate under an intervention does NOT necessarily mean the intervention is harmful; the right counterfactual is 'what would have happened without it on the same trajectory,' which is not available from this data. Read the rates as descriptive, not causal.

## All rows (n=4,053): outcome under each intervention

| slice | rows | good outcomes | good rate |
|---|---|---|---|
| (no intervention fired) | 3056 | 2554 | 83.57% |
| EARLY_BAIL | 861 | 388 | 45.06% |
| ACTION_DENSITY_GATE | 325 | 127 | 39.08% |
| WRITE_PRESSURE | 259 | 42 | 16.22% |


## SWE-bench only (n=1,272): outcome under each intervention

| slice | rows | good outcomes | good rate |
|---|---|---|---|
| (no intervention fired) | 576 | 339 | 58.85% |
| EARLY_BAIL | 861 | 388 | 45.06% |
| ACTION_DENSITY_GATE | 325 | 127 | 39.08% |
| WRITE_PRESSURE | 259 | 42 | 16.22% |


## Outcome distribution under `EARLY_BAIL` (most-fired intervention)

| outcome | count | share |
|---|---|---|
| WRONG_TARGET | 251 | 29.15% |
| STRONG_GOLD_MATCH | 219 | 25.44% |
| PLAUSIBLE_EDIT | 169 | 19.63% |
| EMPTY_PATCH_TIMEOUT | 115 | 13.36% |
| WRONG_LOCATION | 58 | 6.74% |
| STUCK_LOOP | 29 | 3.37% |
| EMPTY_PATCH_CONTEXT_EXHAUSTED | 20 | 2.32% |


## Intervention vocabulary observed in the data

- `ACTION_DENSITY_GATE` (325 occurrences)
- `EARLY_BAIL` (861 occurrences)
- `WRITE_PRESSURE` (259 occurrences)

## Findings (TL;DR)

1. **BFCL fires zero interventions in this dataset** — all 2,480 BFCL rows (v17 + v18 n=1240 each) have `interventions_fired = []`. The intervention stack is SWE-bench-only in practice, consistent with BFCL's short-context tool-call problems not triggering write_pressure / early_bail / prose_burst gates.

2. **SWE-bench no-intervention baseline good-rate: 58.85%** (339/576). This is what intervention slices should be compared against — but remember the selection-bias caveat above.

3. **Per-intervention good-rates on SWE-bench** (rate, pp delta vs no-intervention):

   - `EARLY_BAIL`: 45.06% (-13.79pp vs no-intervention 58.85%)
   - `ACTION_DENSITY_GATE`: 39.08% (-19.77pp vs no-intervention 58.85%)
   - `WRITE_PRESSURE`: 16.22% (-42.63pp vs no-intervention 58.85%)

4. **What this does and doesn't tell us.** Tells us: which interventions are even being exercised on the recorded benches, and how the recorded rows that triggered them landed. Doesn't tell us: whether intervention firing *caused* the outcome — that needs paired traces (intervention-on vs intervention-off for the same instance), which would be a future experiment, not a read-only analysis. The data here is a starting point for *which* interventions to ablate first if you want a causal answer.

5. **Reinforces E1.** Cliff-row coverage of interventions (25%) and non-cliff `empty_patch` coverage are very different problems. The intervention machinery is not silent on `empty_patch` — it's firing — but on the cliff slice it has no signal at all.

