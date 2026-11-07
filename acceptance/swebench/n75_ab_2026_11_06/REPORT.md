# SWE-bench Verified n=75: read-budget / context A/B, m5, 2026-11-06

**Verdict: HOLD. No default change.** Over 3 reps the 128K-context arm (ctxonly) and the
stock 32K control both average **35.7/75**. Rep 1's +4 (38 vs 34) was noise: rep 2 went
33 vs 34 and rep 3 went 36 vs 39. The bigger read budget does change outcomes on specific
instances, deterministically, but the wins and losses cancel.

## Setup

- **Code:** luxe main `65fd43c`, unchanged for all 7 arm-reps. The bench imported from the
  `~/Downloads/luxe` tree, which was not modified during the runs.
- **Model:** champion `Qwen3.6-35B-A3B-6bit` on m5 (M5 Max, 128 GB), oMLX, temp 0.
- **Subset:** frozen `benchmarks/swebench/subsets/v1_baseline_n75.json`.
- **Grading:** official Docker harness, swebench 4.1.0, colima + Rosetta, 1800 s per-instance test timeout.
- **Arms** (each config differs from `configs/single_64gb_swebench.yaml` only in the lines listed):

| Arm | num_ctx | max_steps | max_tokens_per_turn | read budget (`budget_for_ctx`) |
|---|---|---|---|---|
| control | 32768 | 30 | 8192 | 13,653 B |
| ctxonly | 131072 | 30 | 8192 | 54,613 B |
| unbudgeted (rep 1 only) | 131072 | 150 | 32768 | 54,613 B |

- **Order:** rep 1: ctxonly, unbudgeted, control (`run_n75.sh`). Reps 2 and 3: control,
  ctxonly, ctxonly, control (`run_reps.sh`). Arms ran one after another and were never
  concurrent.

## Result

| Rep | control | ctxonly | unbudgeted |
|---|---|---|---|
| rep 1 | 34 | 38 | 38 |
| rep 2 | 34 | 33 | |
| rep 3 | 39 | 36 | |
| **mean (sd)** | **35.7 (2.9)** | **35.7 (2.5)** | 38 (n=1) |

- **n=71** (excluding the 4 ungradeable requests ids, see below): identical numbers, since
  none of those four resolved in any arm-rep. The pre-registered gate (42/75, which is 38/71
  with the four removed) is **not met**. ctxonly's mean is 35.7/71.
- **Empty patches per rep:** control 6 / 8 / 5, ctxonly 6 / 7 / 6, unbudgeted 5.
- **Harness errors:** 0, except pylint-6528 in rep-1 control and unbudgeted (see below).
- **Prediction wall (min):** control 60 / 58 / 66, ctxonly 64 / 67 / 71, unbudgeted 90.
  Grading took 50 to 78 min per arm-rep.
- **Stable core:** control resolves 33 instances in all 3 reps, 39 in at least one and 36 in
  none. For ctxonly the figures are 31, 41 and 34. The September baseline (`1693d06`) was
  38 / 36 / 35, mean 36.3, so both arms sit level with it.

## The mechanism is real; the net effect is zero

Read stats mined from `~/.luxe/runs` (`tabulate.py`). A "refused" read is a `read_file`
that returned 0 bytes on a file larger than the cap, i.e. the whole-file refusal that asks
the model to page in windows.

| | control rep 1 / 2 / 3 | ctxonly rep 1 / 2 / 3 |
|---|---|---|
| read_file calls | 460 / 441 / 476 | 440 / 442 / 443 |
| oversize refusals | 11 / 10 / 13 | 0 / 0 / 0 |
| bytes read (MB) | 1.07 / 1.06 / 1.09 | 1.37 / 1.42 / 1.45 |

The instances that move between the arms (resolve pattern across reps 1/2/3; E = empty patch):

| Instance | control | ctxonly | Reading |
|---|---|---|---|
| django-11333 | 000 | 111 | **Budget win.** Same trajectory in all 3 reps of each arm. Control: the step-0 whole read of `urls/resolvers.py` (29.6 KB, over the 13.6 KB cap) is refused; it greps, reads one 2 KB window, edits, and never opens `urls/base.py`. Its patch (byte-identical across reps) moves the `lru_cache` onto `_get_resolver` but leaves `clear_url_caches()` calling `get_resolver.cache_clear()`, so the FAIL_TO_PASS test fails. ctxonly reads `resolvers.py` whole, then `base.py`, and updates both. |
| django-10880 | 111 | 000 | **Budget loss.** Control adds the space to the `'DISTINCT '` value. ctxonly reads more and puts the space in the shared `Aggregate.template`, which changes the SQL for every non-distinct aggregate and fails a PASS_TO_PASS test. Same trajectory shape in all 3 reps. |
| pylint-4661 | 000 | 011 | ctxonly wins 2 of 3. No refusal in control, so this is a trajectory difference, not the refusal itself. |
| sphinx-10323 | 0 0 E | 1 E E | Rep 1's "consistent win" does not hold. Control is refused once in every rep; ctxonly goes empty in 2 of 3. |
| sphinx-10449 | E E 1 | 111 | ctxonly win, 3/3 vs 1/3. |
| astropy-14096 | 001 | 100 | Flips in both arms. |
| pylint-6386 | 0 E 1 | 000 | Control hits 30 steps every rep, with refusals in 2. |
| sklearn-10297, sphinx-8120, astropy-8707 | 111 | 101 | ctxonly drops rep 2 on each: temp-0 noise at near-ties (identical step and read counts in every sklearn-10297 run). |
| xarray-2905 | 111 | 100 | ctxonly drops reps 2 and 3. No refusals in either arm. |
| matplotlib-13989 | 101 | 111 | Identical step and read counts in every run; temp-0 noise. |

So the read budget does what the rep-1 analysis said it does: with 4x the budget the
refusal path disappears, and on django-11333 that is the whole difference. But the same
extra reading also produces broader, wrong edits (django-10880), and the remaining
movement is the known give-up-vs-commit noise (about ±5 at n=75). Net: zero.

**The budget is not isolated from the window.** At `num_ctx=32768` a 54.6 KB result is
about 33K real tokens, the whole window, so a "budget-only" arm would be unsafe by
construction. ctxonly is "128K window, which buys a 4x read budget". Compaction also fires
far less at 128K. The refusal evidence says the budget is how it acts on django-11333; it
does not prove the budget is the only thing that changed.

## Rep 1 loose ends, resolved

- **pylint-6528 `no_report` (control, unbudgeted): a model-patch hang, not infrastructure.**
  The patch adds `while dirpath: ... dirpath = os.path.dirname(dirpath)`.
  `os.path.dirname('/')` is `'/'`, so the loop never ends on an absolute path, and the test
  run hit the 1800 s timeout (`test_output.txt` stops after `unittest_lint.py`). The two
  patches are byte-identical. Counted as unresolved (model_behavior). No regrade needed; the
  attempted 3600 s regrade was stopped before the m5 reboot and is superseded.
- **psf__requests 1724/1766/1921/2317: ungradeable.** Their tests call live httpbin over TLS
  and fail `SSL CERTIFICATE_VERIFY_FAILED` with the instance image's bundled CA certs,
  whatever the patch. TLS verification was not disabled. They are **flagged, not dropped**:
  the subset JSON gains an `ungradeable` block (the `instance_ids` list is unchanged, so
  every historical n=75 number stays comparable; `read_subset` reads `instance_ids` only).
  Report n=75 and n=71. requests-5414 is not affected: it is a genuine FAIL_TO_PASS miss in
  every arm.
- **Code drift since `1693d06`:** matplotlib-14623 and matplotlib-20826 resolved in
  September and are lost in all arms and reps here.

## Recommendation

- Keep `configs/single_64gb_swebench.yaml` at 32K. No change to `READ_BUDGET_FRACTION`.
- The one actionable signal is the refusal path. django-11333 fails because a refused
  whole-file read leaves the model without the file's callers, not because of the budget
  number. If this is picked up, the lever is what the refusal returns (for example, serve
  the first window plus an outline instead of an error). Test it at 32K against this
  control, at 3 reps, with django-11333 and django-10880 as the paired probes.
- The unbudgeted arm (150 steps, 32K tokens per turn) added nothing over ctxonly in rep 1:
  same 38, 40% more wall. Not worth more reps.

## Files

- `control/`, `ctxonly/`, `unbudgeted/` (rep 1); `rep2/<arm>/`, `rep3/<arm>/`: each has
  `predictions.json`, `summary.json` and `harness/harness_summary.json` (committed), plus
  per-instance JSON and `harness/logs/` (local to m5 only).
- `tabulate.py`: reproduces every number above from those files plus `~/.luxe/runs`.
  `tabulate.json` is its output from 2026-11-06 18:44.
- `run_n75.sh`, `run_reps.sh`: the launchers. `STATUS`: the run log.
- `regrade6528.py`, `regrade_infra.py`: the rep-1 regrade attempts (superseded, as above).
