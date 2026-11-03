# Validation menu (#4) + context pack (#2): A/B plan

Approved 2026-11-03. Origin: the deepsense.ai "harness engineering, 9 patterns"
review. luxe already had patterns 1, 5, 7 and 8. Patterns 2 (just-in-time
context) and 4 (discoverable validation menu) were gaps on the maintain write
path.

## Levers (both opt-in, exact "1", main pass only; `src/luxe/runcontext.py`)

- `LUXE_VALIDATION_MENU=1` (write tasks): the detected test command
  (`pr.detect_test_command`, previously used only by the PR step), the check
  tools the run's own surface exposes, and a closing "Checks" section in the
  report.
- `LUXE_CONTEXT_PACK=1`: a block of at most 2048 characters with pointers
  only: the top 5 BM25 files for the goal with their last 3 commit subjects,
  related tests by naming convention, and the 10 most recently changed paths.
  It never includes file contents.
- With both off, `maintain` passes no `extra_context`: no git call, no event,
  byte-identical.

## Decisions (user, 2026-11-03)

1. neon-rain-implement-reset-shortcut is graded by `npm test`; the menu names
   `npm test --silent` for any package.json repo. This is accepted as a
   KNOWN EXPOSURE, pinned in `tests/test_grader_leak.py`. neon-rain is
   reported separately in the menu arms.
2. Non-champion probe: `gemma-4-26b-a4b-it-6bit` (0 verification calls on any
   fixture, 2026-08-19). Probe only, not a re-open of that verdict.

## Step-0 finding (fixture history before base_sha)

| repo | commits before base | fixtures |
|---|---|---|
| neon-rain | 47 | 2 |
| nothing-ever-happens | 21 | 2 |
| isomer | 9 | 2 |
| the-game | 3 | 2 |
| lpe-rope-calc | 1 | 2 |

On the-game and lpe-rope-calc (4 of 10 fixtures), the pack's commit-subject
and recent-files parts carry almost nothing. Only the BM25 and related-test
pointers are informative there. A null #2 result on those fixtures says little.

## Steps

1. Implement and test (RED on old code, full suite green). Done: m1 4449 passed.
2. Slice: 1 rep x {lpe-rope-calc-implement-strict-flag,
   neon-rain-implement-reset-shortcut} x {menu, pack}. Read the injected
   blocks and reports by hand.
3. Champion: ctl / menu / pack / both, 10 fixtures x 3 reps, interleaved per rep.
4. Gemma: ctl / menu, 10 x 3, interleaved.
5. Hand-verify every PASS (REAL / THIN / VACUOUS / FAIL / DAMAGING),
   `git patch-id` vs ctl, REPORT.md + HAND-VERIFY.md.

Driver: `run_arms.sh <model_id> <label> <reps> ctl menu=LUXE_VALIDATION_MENU=1 ...`

## Measurements

- #4: verification calls before the final turn (lint*, typecheck*, vet_go,
  git_diff, bash matching the test command); whether the "Checks" section is
  present; false claims (a check reported as run with no matching call).
- #2: steps and exploration calls (read_file / grep / bm25_search /
  find_symbol / list_dir / glob) before the first write; prompt tokens;
  compaction events; peak pressure.
- Both: expected_outcome, hand-verified class, wall, step-limit labels, and
  "worse than base_sha with gates empty".

## Gates

- **Abort** on any of these: a grader-leak failure other than the KNOWN pin;
  a block over its cap; an infra failure in more than 1 run in an arm; a
  slice that shows the block missing or malformed.
- **Champion regression** on any of these (3-rep rule): a fixture passing
  3/3 in ctl fails 2 or more of 3 in a treatment arm; hand-verified REAL
  drops by more than 1; total wall rises more than 15%; any false claim (#4).
- **#4 success:** no champion regression; the "Checks" section is present in
  100% of write runs with 0 false claims; on Gemma, fixtures with at least one
  verification call go from 0/10 to at least 7/10, and worse-than-base
  outcomes fall by at least 1.
- **#2 success:** no champion regression, plus one of these: median
  exploration calls before the first write fall by 25% or more; prompt
  tokens fall by 10% or more; a fixture ctl fails converts in at least 2/3.
- **Verdicts:** helps only on the non-champion and is inert on the champion
  ⇒ default-ON as insurance (cleanup-turn precedent, labelled the same way).
  Inert everywhere ⇒ stays opt-in. Any regression ⇒ dropped, with a
  lessons.md entry.

## Risks

| Risk | Mitigation |
|---|---|
| The required Checks section makes the model run slow tests (60 s bash cap) | The section allows "not run (why)"; the wall gate catches it |
| The menu reads as a rigid MUST (pattern 7) | Outcome + procedure + escape wording; a test forbids ALWAYS/NEVER/MUST |
| The pack anchors the model on the wrong files | Pointers only, top 5, caveat in the header; the slice is read first |
| The pack's window cost shifts compaction | 2048-char cap; compare compaction steps against ctl |
| temp=0 noise | 3 interleaved reps, patch-id comparison, document-config reported separately |
