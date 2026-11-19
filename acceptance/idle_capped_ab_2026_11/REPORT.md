# Capped refusal spares (ctl / wide / capped): A/B report

2026-11-19, m5. Plan and gates: `PLAN.md`. Per-run data: `runs.json`
(written by `analyze.py champ`; every row carries its `run_id`). Hand-verify:
`HAND-VERIFY.json`. Run order and timing: `arms.log`. Raw cells:
`champ-<arm>/mono__champ-rep<N>/<fixture>/` (`diff.patch`, `result.json`,
`diagnostics.json`). Per-run events, reports and saved conversations are in
m5's `~/.luxe/runs/<run_id>/` (`events.jsonl`, `synthesizer.md`,
`messages.jsonl.gz`).

## Verdicts

| Arm | Verdict | Deciding evidence |
|---|---|---|
| capped (`LUXE_IDLE_NARROW=capped`, K = 4) | Fails the empty-report gate (5/30, gate <= 1). Not better than wide on any measure. | The budget ran out in 6 runs; in 2 of them (neon reset-shortcut reps 2 and 3) the idle test then fired and the run ended with no report, where wide wrote reports. Wall is the same as wide (784 s vs 787 s). |
| wide (`LUXE_IDLE_NARROW=wide`) | Fails the empty-report gate (4/30). Passes every other gate, including both wall gates. | Idle exits 9 to 0. 3 of the 4 empty reports are the-game reps where the stuck-loop guard aborted (ctl also wrote no report there); the fourth is an oMLX 500 (infrastructure). REAL 27 = ctl 27. |

Recommendation at the end.

## Setup

| | |
|---|---|
| Code | bench worktree `~/Downloads/luxe-bench` at `ced100e` (detached): includes the capped lever and `LUXE_SAVE_MESSAGES` |
| Server | oMLX 0.8.0. The previous A/B (`idle_narrow_ab_2026_11`) ran on 0.7.x |
| Model | `Qwen3.6-35B-A3B-6bit` (champion) |
| Fixtures | all 10 maintain_suite fixtures, 3 reps, arms interleaved ctl, wide, capped within each rep |
| When | 2026-11-19 10:34:01 to 11:18:09 CST (`arms.log`), all 9 arm passes rc=0 |
| Liveness | `idle_narrow_mode` in 30/30 wide and 30/30 capped (capped `refusal_spares` = 4), 0/30 ctl; `bench_venv_path` on in 90/90 (`analyze.py` liveness column) |

`analyze.py` ran unmodified on the real output.

## Results (30 runs per arm)

Counts from `analyze.py champ` stdout and `runs.json`.

| | ctl | wide | capped |
|---|---|---|---|
| printed expected_outcome | 30/30 | 30/30 | 30/30 |
| empty final report | 9 | 4 | 5 |
| post_write_idle_exit | 9 | 0 | 2 |
| aborted (stuck-loop / backend) | 0 | 4 (3 / 1) | 3 (3 / 0) |
| runs with idle_refusal_budget_spent | | | 6 |
| idle spares (refusal / output / silent) | | 56 / 8 / 11 | 45 / 9 / 9 |
| tool calls, sum | 518 | 572 | 583 |
| steps, sum / median | 307 / 10.5 | 350 / 11 | 356 / 10.5 |
| wall, sum (s) | 920 | 787 (-14.4%) | 784 (-14.8%) |
| prompt tokens, sum | 2.64 M | 3.29 M (+24.7%) | 3.33 M (+26.3%) |
| hand-verified REAL / THIN / DAMAGING (runs) | 27 / 3 / 0 | 27 / 3 / 0 | 27 / 2 / 1 |

Where the empty reports are (`runs.json` `final_chars` = 0; abort reasons from
`single_mode_done` in each run's `events.jsonl`):

| Fixture | ctl | wide | capped |
|---|---|---|---|
| lpe-rope-calc-implement-strict-flag | 3 (idle exit) | 0 | 0 |
| neon-rain-implement-reset-shortcut | 3 (idle exit) | 1 (rep 1, oMLX 500) | 2 (reps 2, 3: budget spent, then idle exit) |
| the-game-implement-shuffle-shortcut | 3 (idle exit) | 3 (stuck-loop abort) | 3 (stuck-loop abort) |

### Wall: a slow-decode window in ctl rep 1

The raw wall numbers favor wide and capped because of the substrate, not either
lever. The first five ctl rep 1 runs (10:34 to about 10:39) decoded at 47 to 59
tok/s; the other 85 of 90 runs ran at about 147 tok/s (per-run `step_usage`
events, sum(completion_tokens) / sum(decode_s)). On the same trajectory,
the-game-document-architecture took 36.8 s in ctl rep 1 and 10.6 s in every other
cell. ctl took 455 s in rep 1 and 241 / 224 s in reps 2 and 3; wide took
271 / 254 / 262 s. The cause is not in these logs (likely warm-up right after
the oMLX restart); classified as infrastructure. ctl always ran first, so only
ctl took the hit. Wall is therefore reported three ways ("matched" = pairs where
every arm wrote a non-empty report, as in `analyze.py`):

| View | pairs | wide wall vs ctl | capped wall vs ctl | wide ptok vs ctl | capped ptok vs ctl |
|---|---|---|---|---|---|
| overall, all runs (raw) | 30 | -14.4% | -14.8% | +24.7% | +26.3% |
| overall, without the 5 slow pairs | 25 | +4.9% | +5.4% | +13.1% | +18.4% |
| overall, reps 2 and 3 only | 20 | +11.1% | +11.9% | +24.8% | +29.8% |
| matched, all runs (raw) | 21 | -15.2% | -16.5% | -9.7% | -9.3% |
| matched, without the 5 slow pairs | 19 | -4.0% | -5.2% | -10.6% | -10.1% |
| matched, reps 2 and 3 only | 14 | +0.9% | -0.8% | -2.6% | -4.9% |

Prompt tokens do not depend on decode speed: the levers cost about +25% overall,
all of it on runs ctl cut off; on matched runs the cost is flat or slightly lower.

### Per-fixture wall (sum of reps 2 and 3, s; raw 3-rep sums in parentheses)

| Fixture | ctl | wide | capped | Note |
|---|---|---|---|---|
| lpe-rope-calc-implement-strict-flag | 71 (187) | 98 (150) | 98 (147) | ctl stopped at step 14 with no report; wide and capped identical: 24 steps, ran a verify script, report written |
| neon-rain-implement-reset-shortcut | 54 (106) | 72 (112) | 81 (123) | ctl stopped at step 12 or 13; wide and capped verify for 14 to 26 steps |
| the-game-implement-shuffle-shortcut | 18 (50) | 21 (36) | 21 (32) | every arm ends empty; wide and capped take 3 more calls, then the stuck-loop guard aborts |
| isomer-implement-healthcheck | 30 (61) | 31 (48) | 37 (52) | capped rep 3 took 16 steps instead of 13 |
| nothing-ever-happens-document-config | 92 (135) | 94 (138) | 84 (128) | varies run to run in every arm; neither lever fired (no spares) |
| other 5 fixtures | 200 (384) | 200 (305) | 200 (301) | same patch in all 45 runs; raw sums differ only by the ctl slow window |

## Gates (from `PLAN.md`; wide judged against the same gates)

| Gate | capped | wide |
|---|---|---|
| Empty reports <= 1/30 | Fail: 5/30 | Fail: 4/30 |
| Printed pass 30/30 | Pass | Pass |
| Hand-verified REAL >= ctl (27) | Pass: 27 | Pass: 27 |
| Matched-run wall <= +5% vs ctl | Pass (-16.5% raw, -5.2% without slow pairs, -0.8% reps 2-3) | Pass (-15.2%, -4.0%, +0.9%) |
| Overall wall <= +15% vs ctl | Pass (-14.8% raw, +5.4% without slow pairs, +11.9% reps 2-3) | Pass (-14.4%, +4.9%, +11.1%) |

Failure classification for the empty reports:

- **the-game, 3 in wide and 3 in capped: model_behavior.** Byte-identical across
  the 6 runs (84,946 prompt tokens each). After the edit the model calls Python
  `lint` on `.jsx` (refused), then `npx vite build` three ways: two refused for
  `2>&1` / `|`, one exits 1 with no output. It then repeats `npx vite build 2>&1`
  and `npx vite build` verbatim; the dedup short-circuit answers both and at
  step 11 the stuck-loop guard aborts ("repeated same tool calls 2 consecutive
  turns"). The abort path writes no report. The idle test is not involved (capped
  spent 3 of its 4 spares). In the previous A/B (oMLX 0.7.x) wide wrote 3/3
  reports here, so this trajectory is new on 0.8.0.
- **neon rep 1 in wide: infrastructure.** oMLX returned 500, "Model output
  contains an unrecoverable tool call. Previously delivered tool calls must not
  be executed again on retry", at step 26 (run `8cc28e27b1fd`), after the patch
  was written (REAL). The string appears in 5 run dirs in m5's `~/.luxe/runs`,
  one of them from this A/B.
- **neon reps 2 and 3 in capped: model_behavior hitting the budget.** Both spent
  the 4 spares on Python `lint`/`typecheck` against `.js` files and on
  `2>&1` / `||` refusals, then kept trying variants (`node --check`, not
  allowlisted; `npm run build 2>&1`; repeated `npx eslint`). The idle test fired
  2 or 3 calls after the budget ran out (`ba447e803332` step 18, `dff12ab7c6bb`
  step 22). Their trajectories differ from wide's (wide rep 2 spared exactly 4
  refusals, then got output and finished), so this data cannot say whether a
  larger K would have saved them. PLAN.md's K = 5 mitigation does not apply as
  written: the-game ended empty but never spent its budget.
- **ctl, 9 runs: model_behavior, the known failure,** now more frequent: 9/30
  empty reports and 9 idle exits vs 7 and 7 on 0.7.x (lpe strict-flag 1/3 to 3/3).

The budget did no harm in the other 4 runs where it ran out (lpe strict-flag
reps 1 to 3, capped neon rep 1): refusals after it were interleaved with
productive calls, so the idle streak never built; patch, steps and report match
wide.

## Hand-verify

Details in `HAND-VERIFY.json`: one entry per distinct `git patch-id`, with the
cells that produced it, the class and a reason. 27 distinct patches: 19 REAL,
7 THIN, 1 DAMAGING.

| Fixture | distinct patches | classes | notes |
|---|---|---|---|
| isomer-document-quickstart | 1 (all 9 runs) | REAL | port 27001 and loopback mapping match app.py, Dockerfile and compose |
| isomer-implement-healthcheck | 3 | 3 REAL | correct route; all 9 runs leave a scratch `_test_health.py` in the commit (empty in capped rep 3); the cleanup turn offered 0 candidates |
| lpe-rope-calc-document-typing | 1 | REAL | |
| lpe-rope-calc-implement-strict-flag | 2 | 2 REAL | ctl's `pe_scan.py` change is byte-identical to wide/capped's; wide and capped (6/6) also commit `test_bad.gguf` at the repo root, made to verify strict mode |
| neon-rain-document-modules | 1 | REAL | |
| neon-rain-implement-reset-shortcut | 8 (1 or 2 runs each) | 8 REAL | `a47e0a3717` (wide) and `e4f47a86e3` (capped) keep the persona across the reset; `70be85e740` (ctl) binds in Game.js instead of `src/input/` |
| nothing-ever-happens-document-config | 8 | 7 THIN, 1 DAMAGING | see below |
| nothing-ever-happens-manage-deps-audit | 1 | REAL | all 13 advisory ids appear in the run's `cve_lookup` output |
| the-game-document-architecture | 1 | REAL | |
| the-game-implement-shuffle-shortcut | 1 | REAL | nit: no modifier check, so Cmd/Ctrl+R gets preventDefault and reshuffles |

**CONFIG.md (nothing-ever-happens-document-config).** The bot reads 55 env vars
at base `59b8874`. `bot/config.py` has 24 `PM_NH_` lines; the CONFIG.md patches
have 0 (re-checked by hand). Every patch documents 32 of the 55, leaving out the
23 `PM_NH_*` strategy overrides (`bot/config.py:198-291`), and every patch gives
`DASHBOARD_PORT` a default of 8080 (from `.env.example`); the code has no default
(`bot/main.py:193-196`), so the dashboard is off unless `PORT` or
`DASHBOARD_PORT` is set. 7 patches are THIN (accurate where they say something).
`a544a00024` (capped rep 3) is DAMAGING: it gives `PM_RISK_*` defaults of
1000 / 500 / 200 / 300 against the code's 1500 / 1000 / 0 / 900
(`risk_controls.py:22-25`), telling an operator a $200 daily-drawdown kill
switch is on by default when it is disabled. Not attributable to capped: neither
lever fired on this fixture (`runs.json` `spared`), and it produced 8 distinct
patches in 9 runs across all arms. Grading note: the previous A/B rated its
CONFIG.md patches REAL and reported only the `DASHBOARD_PORT` error; this pass is
stricter and applies to all three arms equally.

Report accuracy (reports read against their tool calls): the 6 lpe wide/capped
reports are identical and the claimed verify script result (exit 0 without
`--strict`, exit 1 with it) matches the real output, though none mentions the
leftover `test_bad.gguf`; capped neon rep 1's test counts (12/0, 56/0) match;
wide neon rep 2's `npm test` claim matches, with one small wrong claim (Shift+R
is not a reload shortcut); isomer capped rep 3's functional test (Status 200) is
real. No fabricated checks found.

## Substrate caveat

oMLX 0.8.0 decodes at about 147 tok/s here against about 70 on 0.7.x. Wall
numbers compare only within this A/B, not with `idle_narrow_ab_2026_11`. The new
substrate also changed trajectories (the-game under wide now ends in a
stuck-loop abort instead of a report), and runs where neither lever fired still
vary at temp 0 (document-config: 8 patches in 9 runs; isomer ctl rep 1 took 26
steps against 13). A one-run difference between arms on a single fixture is
within noise.

## Recommendation (for the user's decision)

1. **Do not promote capped; keep it opt-in or drop it.** It matched wide's wall
   (784 s vs 787 s) and its one behavioral difference was two more runs cut off
   with no report. Its purpose was to bound wide's wall cost, which on this
   substrate is +5% to +11% overall and about 0% on matched runs, inside both
   gates.
2. **wide is a reasonable promotion candidate but misses the empty-report gate
   as written (4/30).** None of its 4 empty reports is an idle exit: 3 are the-game
   stuck-loop aborts where ctl also failed, 1 is an oMLX 500. It removed all 9
   idle exits, recovered 6 reports (lpe 3/3, neon 2/3 net of the infra abort),
   held REAL at 27, and passed both wall gates in every view. For a clean pass
   first: make the stuck-loop abort path write a report (helps ctl too), then
   re-run.
3. **Separate findings to track:** the cleanup turn misses litter from longer
   verification (`test_bad.gguf` in lpe is non-code so never a candidate;
   `_test_health.py` in isomer got 0 candidates in all 9 runs); oMLX 0.8.0 has a
   new 500 ("unrecoverable tool call") that luxe reports as a backend abort.
