# Validation menu (#4) and context pack (#2): A/B report

2026-11-03, m5. Plan, gates and user decisions: `PLAN.md`. Per-run data:
`runs.json` (180 runs, produced by `analyze.py`). Hand-verify verdicts:
`HAND-VERIFY-champ.json` (48 unique patches) and `HAND-VERIFY-gemma.json` (26),
summarized in `HAND-VERIFY.md`. Raw run directories were on m5 under
`~/luxe-bench-tmp/vcab/acceptance/validation_context_ab_2026_11/<label>-<arm>/`
and were deleted with that worktree on 2026-11-03 (user request) (events in `~/.luxe/runs/<run_id>/`; every row of `runs.json` carries its
`run_id`).

## Verdicts

| Lever | Verdict | Deciding gate |
|---|---|---|
| `LUXE_VALIDATION_MENU` (#4) | **Refuted, dropped from the code** | Champion regression: hand-verified REAL 28/30 to 25/30, isomer-document-quickstart REAL 3/3 to THIN 3/3. Gemma: the required Checks section was fabricated in 7/30 runs (false-claim gate). |
| `LUXE_CONTEXT_PACK` (#2) | **Inert, stays opt-in** | No regression (REAL 28/30 both arms); no success gate cleared (exploration before first write -15% against -25%; prompt tokens -6% against -10%; nothing to convert at ctl 30/30). |

Both arms of "both" inherit the menu's regression (REAL 25/30).

## Design as run

- Bench tree `ddc6073` = feature branch `20653e8` plus PR #50 (Python
  analyzers refuse non-Python files) cherry-picked, so every arm, ctl
  included, ran with that fix.
- Champion `Qwen3.6-35B-A3B-6bit`: ctl / menu / pack / both, all 10
  maintain_suite fixtures, 3 reps, interleaved per rep (09:24 to 10:31 CST).
  Per-fixture timeout 1800 s, `--work-dir ~/.luxe/bench-workspace`.
- Probe `gemma-4-26b-a4b-it-6bit`: ctl / menu, 10 x 3, interleaved (10:33 to
  11:15). User-approved probe, not a re-open of the 2026-08-19 verdict.
- Every treatment run logged `main_context_injected` (liveness confirmed).

## Champion results (120 runs)

| | ctl | menu | pack | both |
|---|---|---|---|---|
| printed expected_outcome | 30/30 | 27/30 | 30/30 | 27/30 |
| hand-verified REAL | **28/30** | **25/30** | **28/30** | **25/30** |
| THIN / FAIL / DAMAGING | 1 / 0 / 1 | 5 / 0 / 0 | 1 / 1 / 0 | 4 / 0 / 1 |
| wall, sum (s) | 1,007 | 863 | 912 | 1,024 |
| prompt tokens, sum | 3.05 M | 2.87 M | 2.87 M | 3.06 M |
| runs with at least one verification call | 22 | 30 | 21 | 30 |
| check-tool calls (lint/typecheck/git_diff...) | 15 | 84 | 30 | 69 |
| refused or empty bash calls | 48 | 15 | 16 | 24 |
| runs ended by post_write_idle_exit | 6 | 0 | 8 | 0 |
| runs with an empty final report | 6 | 0 | 6 | 0 |
| median exploration calls before first write | 6.5 | 6.0 | 5.5 | 6.0 |

Per fixture, every cell is identical across arms (REAL 3/3) except:

- **isomer-document-quickstart:** ctl REAL 3/3, pack REAL 3/3, menu THIN 3/3,
  both THIN 3/3. Deterministic: one patch-id per arm across all reps. The
  trajectories match ctl step for step (read, grep, one `edit_file`,
  `git_diff`); with the menu present the single edit is a 2 to 3 line comment
  touch-up instead of ctl's 26-line restructure of the Quick Start. The grader
  fails it on the 4-added-line floor; the hand read agrees it under-delivers.
- **nothing-ever-happens-document-config:** varies run to run in every arm,
  ctl included (ctl: REAL, THIN, DAMAGING; both: REAL, THIN, DAMAGING; menu:
  REAL, THIN, THIN; pack: REAL, REAL, THIN). This is the suite's known
  variance class and carries no lever signal. The two DAMAGING docs state
  wrong defaults for a trading bot's risk limits (ctl r1: max open exposure
  `10000.0` against `1_500.0` in `bot/risk_controls.py:22`).
- **lpe-rope-calc-implement-strict-flag, pack rep 3:** FAIL at the step limit
  (`bad_files = [] if args.strict else None` followed by
  `(bad_files or []).append(path)`, so `--strict` can never fail). One rep of
  three; below the 3-rep regression rule.

What the menu did do on the champion: it raised verification (every run
called a check tool; check-tool calls 15 to 84), cut wall 14%, and its test
line ("run it exactly as written; stderr is already included; redirects,
pipes and chains are refused") eliminated the redirect trap below (0
idle-exits, 0 empty reports, against 6 and 6 in ctl). Its Checks sections were
honest: every "tests passed" claim matches a successful bash call, and a
missing test command was reported as "Not run". None of that changed an
outcome on the implement or manage fixtures (REAL 12/12 in every arm), and
the doc regression above is deterministic.

## Gemma results (60 runs)

| | ctl | menu |
|---|---|---|
| printed expected_outcome | 27/30 | 30/30 |
| hand-verified REAL | **10/30** | **5/30** |
| THIN / DAMAGING | 8 / 12 | 16 / 9 |
| left the repo worse than base (of which printed PASS) | 12 (9) | 9 (9) |
| fixtures with at least one verification call | 2/10 | 4/10 |
| check-tool calls | 0 | 7 |
| runs whose Checks section claims a check that never ran | n/a | **7/30** |
| wall, sum (s) | 1,307 | 1,098 |

- Gemma does use tools natively on oMLX 0.7.3 (0 errors after the server
  repair below).
- The plan's success gate (fixtures with verification 0/10 to at least 7/10)
  was not met: 2/10 to 4/10. Worse-than-base fell 12 to 9, but REAL halved.
- **Fabrication.** Seven menu runs (isomer quickstart and healthcheck, all
  reps, plus one document-config) write "git_diff: Ran to verify changes to
  `README.md`" or "git_diff: Reviewed changes manually" with zero `git_diff`
  calls in the run, and give invented reasons for skipped checks ("lint: Not
  run (no test command detected)" on a Python app). Requiring a
  self-reported Checks section gave a non-verifying model a template to fill,
  not a behavior to perform.
- The printed 27 to 30 is grader over-credit: 9 of the menu arm's 30 printed
  passes left the repo worse than base (e.g. `ap.add.argument(...)`, which
  crashes the script at startup; a shortcut change that deletes the app's
  `shuffle()` mount call and splash timer).

## Findings outside the two levers

1. **Pre-existing redirect trap (champion, every arm).** The model runs tests
   as `npm test 2>&1`, `npm test 2>&1 | tail -40`, `... || true`. Bench bash
   refuses redirects and chains; each refusal is an idle call; three in a row
   end the run through `post_write_idle_exit` with no final report. ctl: 48
   refused/empty bash calls and 6/30 runs with no report (lpe strict-flag and
   neon reset-shortcut, 3/3 each). The pack makes it fire more (it points at
   tests): 8 idle-exits. Outcome scores are unaffected (the graders read the
   diff), but the report is lost. Candidate lever: say it in the refusal
   message ("output already includes stderr; drop 2>&1") or accept a trailing
   `2>&1` as a no-op. Either changes the shared bash path and needs its own
   A/B.
2. **`lint` on a JavaScript file** returned `status: ok` with ~63 KB of
   `invalid-syntax` findings (ruff lints any file named explicitly). Fixed in
   PR #50; every arm here ran with the fix.
3. **`pr.detect_test_command` keys on `package.json` existing**, not on a
   `scripts.test` entry, so the-game is offered `npm test --silent`, which
   fails. The champion reported it honestly; it still cost calls. Affects the
   PR step's test run too.
4. **Stale oMLX on m5.** The server had run since 2026-10-27 from the deleted
   `0.7.3rc1` Cellar (0.7.3 installed 2026-11-02). Qwen, already imported,
   kept working; Gemma failed every load with `No module named
   'transformers.models.gemma4'`. `luxe repair` restarted it in 6 s. The
   first Gemma pass (60 errors in 60 s) is kept under `gemma_infra_failed/`
   on m5 and excluded from every number here.
5. **m5 suite.** 4 tests fail on m5 on untouched main as well
   (bash process-group timeout, bash over-cap tail, the rglob canary, glob
   partial results); harvest_2026_10 saw 2. Environment drift on m5, not this
   branch.
6. **Fixture history.** lpe-rope-calc has 1 commit before `base_sha` and
   the-game 3, so on 4 of 10 fixtures the pack's commit-subject part was near
   empty. A null pack result there says little.

## Measurement limits

`tool_call` events carry no arguments. "Test run" means a bash call with
non-empty output; the false-claim check is conservative (a claim with zero
successful calls of that kind) plus a hand read of every flagged and every
unflagged-but-differently-headed section. The verifier's two strongest
calls (ctl document-config r1 DAMAGING; pack strict-flag r3 FAIL) were
re-checked against base by hand.

## What lands

- PR #50 (analyzers refuse non-Python files): independent fix, ready.
- PR #51: the context pack, opt-in and inert, plus this record. The menu is
  removed from the code; `luxe.sdd` records why so it is not re-added.
