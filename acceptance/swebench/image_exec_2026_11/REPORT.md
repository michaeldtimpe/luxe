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
- Scratch files stripped from 7 ix patches. **Correction (2026-11-21):** one did reach a prediction: ix rep 1
  pylint-6528 carries `tmp/pylint_test/.a/foo.py` (unresolved). The strip misses scratch files inside new
  directories; see the follow-up below.
- Grading took 3.7 h with 2 harness workers; `run_slice.sh` now uses `GRADE_WORKERS=4`.

## Next experiment (needs a go-ahead; ~4 h predictions + ~2 h grading)

Same slice, 3 reps: `ix` (as here) vs `ix60` (= ix with `max_steps: 60` in a copy of the exec config) vs
`ix60nb` (= ix60 + `--no-early-bail --no-write-pressure --no-action-density-gate`). Gates as here. Promotion
beyond the slice still needs the n=75 x 3 run against the 42/75 gate.

## Follow-up: 60 steps, and 60 steps without commit pressure (2026-11-20 21:38 to 2026-11-21 07:05)

**Run.** Same slice, 3 reps interleaved, same server and model, bench at 8af5a49 (adds only the 60-step config
and the exec-config check, #69, and the cleanup `_`-stem fix, #70, which SWE-bench never exercises). Arms:
`ix` (as above, re-run), `ix60` (= ix with `max_steps: 60`, `configs/single_64gb_swebench_exec_60.yaml`),
`ix60nb` (= ix60 + `--no-early-bail --no-write-pressure --no-action-density-gate`). Every arm rc=0, 225 runs
graded with 4 workers in 1.7 h, 0 harness errors. No same-session control: the comparison is G3's `control`
above. Analysis: `analyze.py --out followup/analysis.json control=slice/control ix=followup/ix
ix60=followup/ix60 ix60nb=followup/ix60nb`.

### Verdict

**ix60nb is the best arm so far (38/75, 12 flip-target resolves, every new pass REAL), and it still fails
no-harm (-4 against the gate's 1).** 60 steps alone changes nothing on outcome: the step-limit hits drop
from 21 to 4, but ix60 resolves 34, the same as ix. The gain comes from removing the commit-pressure stack
on top of the larger budget. Not promoted. Two defects found below have cheap fixes; see Next.

| | control (G3) | ix | ix60 | ix60nb | gate |
|---|---|---|---|---|---|
| resolved, all 75 | 31 | 34 (10/12/12) | 34 (11/10/13) | **38** (13/13/12) | report |
| flip targets (39) | 1 | 7 | 8 | **12** | above ix: ix60nb **PASS** |
| no-harm (36) | 30 | 27 | 26 | 26 | net loss at most 1: **FAIL** (-3, -4, -4) |
| every new PASS read by hand | | | | | **all REAL** (one with a scratch file) |
| runs at the step limit | 5 | 21 | 4 | 7 | |
| median steps | 14 | 22 | 23 | 24 | |
| median wall per instance | 32 s | 86 s | 97 s | 106 s | |
| empty patches | 5 | 12 | 6 | 6 | |
| runs with an image call | 0/75 | 71/75 | 73/75 | 75/75 | |
| image exec errors | 0 | 0 | 0 | 0 | |

The re-run `ix` lands at 34 against G3's 35, and 7 flip-target resolves against 7: the G3 result reproduces
in a second session.

### Flips (per instance, resolved of 3)

| instance | prior | control / ix / ix60 / ix60nb |
|---|---|---|
| astropy__astropy-13236 | 0/9 | 1 / 3 / 3 / 3 |
| astropy__astropy-13453 | 0/9 | 0 / 1 / 2 / **3** |
| psf__requests-2931 | 0/9 | 0 / 1 / 1 / 2 |
| psf__requests-5414 | 0/9 | 0 / 2 / 0 / **3** |
| pylint-dev__pylint-6528 | 1/9 | 0 / 0 / 1 / 1 |
| pytest-dev__pytest-10051 | 0/9 | 0 / 0 / 1 / 0 |

### Hand verification (12 distinct passing patches, 2 already read in G3)

- **astropy-13453** (4 patches). `a67daf3d` is the gold patch (`self.data.cols = cols` plus
  `_set_col_formats()`). `22b0d532` and `520c7224` set `col.info.format` from `self.data.formats` on each
  column write() receives, before the rows are rendered: same effect, it changes the format on those column
  objects rather than through the writer. `2d050967` was read in G3. REAL x4.
- **psf__requests-2931** (2). The issue: a bytes body is passed through `to_native_string` and fails. Both
  bypass `_encode_params` for a bytes body in `prepare_body`. `dffea307` also decodes bytes params as utf-8
  where gold uses `to_native_string` (ascii): broader, same result on the test inputs. Neither touches the
  params path as gold does. REAL x2.
- **psf__requests-5414** (3). `a8dd3af5` is the gold condition. `b063d928` was read in G3. `8ed7cbf2`
  catches urllib3's `LocationParseError` in `HTTPAdapter.send` and raises `InvalidURL`: `requests.get`
  now raises the right exception, but at send time, so `Request.prepare()` alone does not. REAL (different
  layer).
- **pylint-6528** (2). Both make `_discover_files` an instance method and apply the ignore, ignore-patterns
  and ignore-paths checks to the arguments, the walked directories (pruned in place) and the files; gold
  prunes during the walk through a shared `_is_ignored_file`. REAL x2. **`ce007a88` (ix60 rep 3) commits
  `test_repro/.a/foo.py` and `test_repro/bar.py`**, scratch the strip did not catch.
- **pytest-10051** (1). Gold adds `LogCaptureHandler.clear()` that empties the record list in place. The model
  records the current phase in the item stash and re-points `caplog_records_key[when]` at the new list after
  `reset()`. Different mechanism, `get_records()` and `records` agree after `clear()`. REAL.

### No-harm losses

| instance | prior | control / ix / ix60 / ix60nb | cause |
|---|---|---|---|
| matplotlib__matplotlib-14623 | 0/9 | 2 / 0 / 0 / 0 | with more steps the model keeps building 43 to 45 line patches; ix60 ends 2 of 3 in a stuck loop |
| django__django-10880 | 6/9 | 3 / 2 / 2 / 1 | every loss is an empty patch: the only write is `repro_issue.py`, the model spends its steps configuring Django settings for it, and the post-write idle exit ends the run (below) |
| pylint-dev__pylint-4970 | 0/9 | 1 / 1 / 0 / 0 | 13-line patches that do not pass |
| pytest-dev__pytest-5262 | 8/9 | 3 / 2 / 2 / 3 | one different 19 to 22 line patch in rep 2 |
| astropy__astropy-12907 | 8/9 | 3 / 3 / 3 / 2 | one different 13-line patch |
| django__django-11333, pydata__xarray-2905 | 6/9 | 1 / 1 / 1 / 2 and 2 / 3 / 3 / 3 | (gains) |

The control row comes from a different session, and on the two 0/9-prior instances (14623, 4970) its 3 resolves
are above that prior. On the 10 no-harm instances with a prior of at least 6/9, each ix arm is at net -1. This
is a diagnostic only: the gate as written fails, and the decision needs a same-session control.

### Two defects the follow-up exposed

1. **A scratch write arms the post-write idle exit.** Under image exec the model often writes a reproducer
   first. `post_write_idle_exit` counts that as the change and ends the run when the model goes on reading,
   so the source is never edited. Empty-patch runs whose only writes were scratch files: ix 6 (1 idle exit,
   5 at the step limit), ix60 2, ix60nb 6 (4 idle exits); G3 ix 5. In ix60nb these are all 6 of its empty
   patches and 2 of its 3 django-10880 losses. `--no-early-bail` and the other two flags do not disable this
   exit.
2. **The scratch strip misses new directories.** It strips scratch-named files and new root-level `*.py`, not
   `test_repro/`, `tmp/pylint_test/` or `.a/` trees. 5 predictions carry such files (G3 ix rep 1, follow-up
   ix rep 1, ix60 reps 1 and 3, ix60nb rep 1), all on pylint-6528, whose issue is about ignored `.a`
   directories; one is a resolved prediction (ix60 rep 3).

### Next (needs a go-ahead)

Fix both defects (scratch paths do not count as the write for the idle exit under image exec; strip untracked
files in new top-level directories that hold no package), then run `control` vs `ix60nb` on this slice in
one session, 3 reps (~3.5 h predictions + ~1 h grading). If it clears no-harm, the n=75 x 3 run against the
42/75 gate decides promotion.
