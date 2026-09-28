# SWE-bench Verified n=75 baseline — m5, 2026-09-28

- **Code:** luxe main `1693d06`, after the 2026-09 review (PRs #10–#29).
- **Model:** champion `Qwen3.6-35B-A3B-6bit` on m5 (M5 Max, 128 GB).
- **Config:** default `configs/single_64gb_swebench.yaml`, with the full intervention stack on (the runner defaults).
- **Subset:** frozen `benchmarks/swebench/subsets/v1_baseline_n75.json`.
- **Reps:** 1.

Grading used the official Docker harness (swebench 4.1.0) on m5, with colima and Rosetta (amd64 images), in 5
batches of 15. Each batch's images were removed before the next, keeping disk use bounded.

## Result

| | Count | Rate |
|---|---|---|
| **Resolved** (FAIL_TO_PASS all pass, PASS_TO_PASS no failures) | **38 / 75** | **50.7%** |
| Resolved among non-empty patches | 38 / 64 | 59.4% |
| Empty patch | 11 / 75 | 14.7% |
| Harness errors / patch failed to apply | 0 / 0 | — |

**Wall time:** predictions 1h38m (11:20–12:58, about 78 s per instance); grading 49m (including image pulls).

**Integrity checks:** all 64 non-empty patches applied and produced a report. Every resolved instance has
non-empty FAIL_TO_PASS successes and zero PASS_TO_PASS failures.

## Context (not a like-for-like comparison)

Earlier harness-graded n=75 runs were on m1, with older code and different intervention stacks:

| Run | Resolved | Empty patch |
|---|---|---|
| v1.6 (2026-05-09) | 36/75 = 48.0% | 18 |
| v1.9 (2026-05-13) | 34/75 = 45.3% | 19 (full stack) |
| **this run** | **38/75 = 50.7%** | **11** |

The resolved count is the best on record, but with one rep and a changed host, ±2 is noise. Empty patches are
the clearer movement: 11, against 18–19 in May.

## By repo

| Repo | Resolved | Empty |
|---|---|---|
| astropy | 2/8 | 1 |
| django | 4/8 | 1 |
| matplotlib | 2/8 | 4 |
| seaborn | 0/2 | 1 |
| flask | 1/1 | 0 |
| requests | 5/8 | 0 |
| xarray | 5/6 | 1 |
| pylint | 0/5 | 2 |
| pytest | 5/7 | 0 |
| scikit-learn | 5/6 | 0 |
| sphinx | 3/8 | 1 |
| sympy | 6/8 | 0 |

**Empty patches:** astropy-14096, django-11734, matplotlib-13989/20488/20676/25775, seaborn-3069, xarray-6938,
pylint-4604/6386, sphinx-10323. Matplotlib and pylint are the weak spots: 2/13 resolved, and 6 of the 11 empty
patches.

## Files

- `preds/`: `predictions.json` and per-instance JSON.
- `preds.log`
- `harness/`: `harness_summary.json`, plus per-instance `report.json` / `run_instance.log` under `harness/logs/`.
- `harness.log`
