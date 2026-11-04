# Post-write idle test (narrow / wide): A/B report

2026-11-03, m5. Plan, slice re-scope and gates: `PLAN.md`. Per-run data:
`runs.json` (`analyze.py`; every row carries its `run_id`). Hand-verify:
`HAND-VERIFY.json`. DEBUG dispatch logs of two narrow cells: `debug/`. Raw
run directories on m5 under
`~/luxe-bench-tmp/inab/acceptance/idle_narrow_ab_2026_11/champ-<arm>/`.

## Verdicts

| Arm | Verdict | Deciding evidence |
|---|---|---|
| wide (`LUXE_IDLE_NARROW=wide`) | **Success gates met, wall regression gate tripped (+18.2% vs +15%). Stays opt-in; promotion is the user's call.** | Empty reports 7 -> 0, idle exits 7 -> 0, 30/30 printed and every varying patch REAL. Wall 990 -> 1170 s, prompt tokens 3.02 M -> 3.92 M. |
| narrow (`LUXE_IDLE_NARROW=1`) | **Insufficient. Stays opt-in.** | 2 reps only (stopped by user decision): empty reports 4/20, idle exits 4. The idle calls left are refusals, which narrow keeps idle. |
| ns (narrow + `LUXE_BASH_NOOP=strip`) | **Not run to completion** (16 runs). | Empty 1/16, idle exits 1. Lost the slice tiebreak to wide (two levers, one of them runs refused commands). |

## Design as run

Champion `Qwen3.6-35B-A3B-6bit` on oMLX, all 10 maintain_suite fixtures,
arms interleaved per rep, 17:50 to 18:52 CST. Bench tree `3f1b3d1` (main
`7bc3bfc` + the lever), so ctl has `python` on the bench PATH (now the
shipped default). Liveness: `bench_venv_path` on in 30/30 ctl and 30/30
wide, `idle_narrow_mode` in 30/30 wide.

The run started with four arms. After the slice the user asked for the
full A/B on the slice winner only, so it was stopped at ns rep 2 and
resumed with ctl and wide; completed cells were kept (narrow 20 runs,
ns 16).

**One infrastructure failure, an execution error on my side, retried.** I
relaunched the first launch under nohup and the killed process still held
the repo lock, so ctl rep 1 `lpe-rope-calc-document-typing` never reached
the model ("another luxe run is active on this repo"). It was re-run alone
at 18:53 with `--retry-errors` (`arms.log` RETRY line). It produced the same
patch as ctl reps 2 and 3 in the same wall time. It is not interleaved.

## Results (ctl vs wide, 30 runs each)

| | ctl | wide |
|---|---|---|
| printed expected_outcome | 30/30 | 30/30 |
| runs ended by post_write_idle_exit | 7 | 0 |
| runs with an empty final report | 7 | 0 |
| step-limit label / aborted | 0 / 0 | 0 / 0 |
| median steps | 10 | 11 |
| tool calls, sum | 508 | 581 |
| idle-test spares (refusal / output / silent) | | 39 / 13 / 15 |
| wall, sum (s) | 990 | 1170 (+18.2%) |
| prompt tokens, sum | 3.02 M | 3.92 M (+29.5%) |

**Where the wall went** (ctl -> wide, sum of 3 reps):

| Fixture | ctl | wide | delta | Why |
|---|---|---|---|---|
| neon-rain-implement-reset-shortcut | 89 s | 169 s | +80 | ctl: 3/3 cut off by the gate, no report. wide: verification continues, 3/3 reports |
| the-game-implement-shuffle-shortcut | 29 s | 71 s | +42 | same: ctl 3/3 cut off, wide 3/3 reports |
| lpe-rope-calc-implement-strict-flag | 262 s | 296 s | +33 | ctl rep 1 cut off at 65 s; ctl reps 2 and 3 are identical to wide |
| nothing-ever-happens-document-config | 228 s | 262 s | +34 | the fixture that varies run to run; wide rep 3 took 26 steps, 137 s. Not trap-related |
| the other 6 | 381 s | 371 s | -10 | identical trajectories |

So about 155 of the 180 extra seconds are the gate no longer cutting off
verification on the three trap fixtures. That is what the gate was ending
early, and why the wall gate trips.

**Outcomes.** On 7 of 10 fixtures every ctl and wide run produced the same
patch (`git patch-id`). The varying patches were hand-verified (12):
neon reset-shortcut 4 patches, lpe strict-flag 2, nothing-ever-happens
document-config 6. All 12 REAL; none worse than base. Wide on neon gave one
patch that keeps the persona across the reset (better than any earlier
champion patch) and one whose Shift+R also starts a game from the title
screen (a nit). The six CONFIG.md patches were checked by a subagent against
the base source (all 55 variables, defaults) and spot-checked; all share one
wrong claim (`DASHBOARD_PORT` default 8080; the dashboard is off unless
`PORT` or `DASHBOARD_PORT` is set, `bot/main.py:193`), in both arms.

**The 7 recovered reports are real.** Every wide report on the three trap
fixtures was read against its diff and its tool calls. They describe the
change accurately, say what was not possible (`vite build` not on PATH, no
tests allowed by the `.sdd`), and the lpe report's three `pe_scan.py` checks
match four bash calls that returned output. No fabricated checks.

## Gates

| Gate | Result |
|---|---|
| Abort: liveness, infra failures | Pass (1 infra failure, retried, cause stated above) |
| Regression: per-fixture REAL 3/3 -> <=1/3 | Pass (every varying patch REAL) |
| Regression: hand-verified REAL drop > 1 | Pass |
| Regression: 2+ more step-limit / aborted runs | Pass (0 and 0) |
| **Regression: wall +15% or more** | **Tripped: +18.2%** |
| Success: empty reports <= 2/30 | Pass (0/30) |
| Success: idle exits fall by at least half | Pass (7 -> 0) |

By the plan's rules wide is not promoted on this record. The trade, for the
user's decision: on the champion, wide turns 7/30 runs that ended silently
mid-verification into runs that finish verifying and write an accurate
report, at +18% wall (+6 s per run averaged over the suite, +17 s per run
on the three trap fixtures) and +30% prompt tokens. No other effect: no
step-limit hits, no aborts, no regression in patches.

## What was learned

1. **The narrow test alone does not fix it.** Once silent successes and
   non-zero exits stop counting, the idle calls that remain are refusals:
   Python `lint`/`typecheck` on `.js` (PR #50's clean error), `2>&1` /
   `|| true` suffixes, `node` (not allowlisted), and a repeat of a refused
   command. Narrow keeps all of those idle by design (`debug/`).
2. **For this champion the gate's refusal counting is the whole problem.**
   With refusals spared (wide), the gate never fired in 30 runs, and the
   stuck-loop guard and max_steps never had to stand in for it. The
   model's verification after refusals ends by itself, in 16 to 25 steps.
3. **The gate's original target is still covered.** Identical repeats go
   through the dedup short-circuit (idle in every mode), and back-to-back
   repeats hit the stuck-loop guard first (`tests/test_idle_narrow.py`).

## What lands (branch `feat/idle-narrow`, opt-in, off = byte-identical)

`LUXE_IDLE_NARROW=1|wide`, its events (`idle_narrow_spared`,
`idle_narrow_mode`), tests, agents.sdd, and this record.
