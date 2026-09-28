# SWE-bench Verified n=75 baseline — m5, 2026-09-28

## 3-rep summary (added after reps 2–3)

| Rep | Resolved | Empty patch | Harness errors |
|---|---|---|---|
| rep1 | 38/75 (50.7%) | 11 | 0 |
| rep2 | 36/75 (48.0%) | 12 | 0 |
| rep3 | 35/75 (46.7%) | 12 | 0 |
| **mean** | **36.3/75 (48.4%)**, sd 1.5 | **11.7** | 0 |

- **Stable core:** 34 instances resolve in all 3 reps, 39 in at least one, 36 in none.
- **5 flippers:**
  - matplotlib-13989: 0 1 1 (empty in rep1)
  - xarray-3095: 1 0 0
  - sphinx-10449: 1 0 0
  - sympy-12481: 1 1 0
  - sympy-13031: 1 0 0
- **Empty in all 3 reps (9):** astropy-14096, django-11734, matplotlib-20488/20676/25775, seaborn-3069,
  xarray-6938, pylint-4604/6386. 15 instances were empty in at least one rep.
- **Integrity:** every non-empty patch in every rep applied. Every resolved instance passes FAIL_TO_PASS with no
  PASS_TO_PASS failures.

**Verdict:** rep1's 38 was the top of the range; the confirmed baseline is about 36/75 (48%). That is level with
v1.6's single-rep 36/75, not better. The empty-patch drop holds (11–12 per rep, against 18–19 in May). Reps 2–3
are in `rep2/` and `rep3/`, graded together, with each instance's image pulled once.

## Rep 1 detail

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
| **this run, rep1** | **38/75 = 50.7%** | **11** |
| **this run, 3-rep mean** | **36.3/75 = 48.4%** | **11.7** |

Rep1 alone looked like the best on record. The 3-rep mean puts it level with v1.6. Empty patches are
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
