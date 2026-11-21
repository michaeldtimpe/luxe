# SWE-bench image exec (G3): 3-arm slice result (2026-11-20, m5)

**Setup.** `fo_slice_2026_11.json` (13 flip targets with a 9-rep prior of 0/9 or 1/9 for most, 12 no-harm
instances), 3 reps interleaved, champion on oMLX 0.8.0, bench at 5f05acf. Arms: `control` (shipped
defaults), `fo` (`LUXE_FAILED_OUTPUT=1`), `ix` (`--image-exec` + exec config + `LUXE_FAILED_OUTPUT=1` +
`LUXE_BASH_NOOP=strip`). Predictions 10:28 to 14:36, grading to 18:22, 225 runs, 0 harness errors.
Analysis: `analyze.py` (`slice/analysis.json`). Plan and gates: `PLAN.md` (G0-G2 passed earlier).

## Verdict

**Image exec works and finds real fixes, but it fails the no-harm gate by one, because verification now
runs out of the 30-step budget.** Do not promote as is. Next: the same ix arm with `max_steps` 60, and
optionally without the commit-pressure stack (diag_misses levers 2 and 3, both already built).

| | control | fo | ix | gate |
|---|---|---|---|---|
| resolved, all 75 | 31 | 32 | **35** | report |
| flip targets (39) | 1 | 1 | **7** | ix > control and fo: **PASS** |
| no-harm (36) | 30 | 31 | 28 | ix at most 1 net loss: **FAIL (-2)** |
| every new PASS read by hand | | 1 | 4 | **all REAL** |
| image calls in every ix run | 0/75 | 0/75 | 72/75 | **miss by 3**: model behavior (below), 0 image errors |
| runs at the 30-step limit | 5 | 4 | **23** | |
| post-write idle exits | 57 | 59 | 12 | |
| median steps | 14 | 15 | 22 | |
| median wall per instance | 32 s | 33 s | 81 s | |
| failing output shown to the model | 0 | 40 | 160 | |

`fo` alone is neutral (+1 overall, 0 net no-harm loss): showing a failing command's output helps little
while those commands fail to import on the host. Its value is as part of `ix`.

## Flips (hand-read against the gold patch)

| instance | prior | control / fo / ix | what ix did |
|---|---|---|---|
| astropy__astropy-13236 | 0/9 | 1 / 0 / **3** | Core hunk = gold. The diag control deleted the `NdarrayMixin` import (ImportError, 644 P2P failures); ix saw the ImportError in the image and moved the import to `astropy/table/__init__.py` instead. REAL x3 |
| astropy__astropy-13453 | 0/9 | 0 / 0 / **2** | Adds `self.data.cols = cols` and `_set_col_formats()` (gold; the diag report said every earlier attempt missed `cols`). Call order differs from gold, tests pass. REAL |
| psf__requests-5414 | 0/9 | 0 / 0 / **1** | `host.startswith(u'*') or host.startswith(u'.')`, the gold condition. REAL |
| pylint-dev__pylint-6528 | 1/9 | 0 / **1** / **1** | Both arms move the ignore checks into a non-static `_discover_files` and prune ignored dirs during the walk; same approach as gold. REAL |

Not flipped although it was the smoke's headline: requests-2931 resolved in G2 (on 68a639b) and 0/3 here
(5f05acf, after the "leave scratch files" prompt change), a near-tie under a changed prompt.

## No-harm losses (why the gate fails)

| instance | prior | control / ix | cause in ix |
|---|---|---|---|
| matplotlib__matplotlib-14623 | 0/9 | 2 / 1 | all 3 ix runs end at the 30-step limit (45 to 72 line patches, still mid-work) |
| pylint-dev__pylint-4970 | 0/9 | 1 / 0 | 2 of 3 ix runs at the step limit; control's one resolve is a 22-line patch its other two reps did not repeat |
| django__django-10880 | 6/9 | 3 / 2 | ix r1: post-write idle exit with an empty patch after 1 image call |
| pydata__xarray-2905 | 6/9 | 2 / **3** | (a gain) |

Two of the three losses are on instances whose prior is 0/9, and in both the ix runs were still working
at step 30. This is the budget, not a wrong fix.

## Liveness and environment

- 0 `image_exec_error` in 75 runs. The 3 ix runs with no image call never ran python: two habituation exits
  at step 20 with no write, and one 10-step early_bail text answer (model behavior).
- Django testbeds have no pytest: the model hit `No module named pytest` 8 times and then switched to
  `tests/runtests.py` 9 times without any hint.
- Scratch files stripped from 7 ix patches; none reached a prediction.
- Grading took 3.7 h with 2 harness workers; `run_slice.sh` now uses `GRADE_WORKERS=4`.

## Next experiment (needs a go-ahead; ~4 h predictions + ~2 h grading)

Same slice, 3 reps: `ix` (as here) vs `ix60` (= ix with `max_steps: 60` in a copy of the exec config) vs
`ix60nb` (= ix60 + `--no-early-bail --no-write-pressure --no-action-density-gate`). Gates as here. Promotion
beyond the slice still needs the n=75 x 3 run against the 42/75 gate.
