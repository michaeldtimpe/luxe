# Redirect trap (idle-exit with no report): A/B report

2026-11-03, m5. Plan, re-scope and gates: `PLAN.md`. Per-run data:
`runs.json` (150 runs, `analyze.py`). Hand-verify: `HAND-VERIFY.json`
(the 30 patches that differ between arms). DEBUG dispatch logs of two real
bench cells: `debug/`. Raw run directories were on m5 under
`~/luxe-bench-tmp/bnab/acceptance/bash_noop_ab_2026_11/champ-<arm>/` and were
deleted with that worktree on 2026-11-03 (user request); events and reports
remain in m5's `~/.luxe/runs/<run_id>/`, and every `runs.json` row carries
its `run_id`.

## Verdicts

| Arm | Verdict | Deciding evidence |
|---|---|---|
| py (`LUXE_BENCH_PY_PATH=1`) | **Mechanism confirmed, success gate not met. Stays opt-in; promotion is the user's call.** | `python ...` now succeeds where it exited 127 (DEBUG log). Idle-exits on the Python fixtures unchanged (lpe strict-flag 3/3 in every arm). No regression. |
| py_hint (`LUXE_BASH_NOOP=hint`) | **Inert. Stays opt-in.** | Fired 16 times; empty reports 8 (ctl 8). |
| py_strip (`LUXE_BASH_NOOP=strip`) | **Inert. Stays opt-in.** | Fired 12 times; empty reports 10. |
| py_final (`LUXE_IDLE_EXIT_FINAL_TURN=1`) | **Refuted. Removed from the code.** | 9 firings, 9 answered with a tool call (`finish=tool_calls`), 0 reports. |

None of the four reduces runs that end with no report. The root cause is
the gate's idle test, below.

## Design as run

Champion `Qwen3.6-35B-A3B-6bit` on oMLX 0.7.3 (after the `luxe repair`
earlier the same day), all 10 maintain_suite fixtures, 3 reps, arms
interleaved per rep, 14:22 to 15:33 CST. Bench tree `1b80f78`. Every
treatment run logged its liveness event (`bench_venv_path` 120/120,
`bash_noop_mode` 60/60). 0 errors.

## Results (150 runs)

| | ctl | py | py_hint | py_strip | py_final |
|---|---|---|---|---|---|
| printed expected_outcome | 30/30 | 30/30 | 30/30 | 30/30 | 30/30 |
| runs ended by post_write_idle_exit | 8 | 9 | 9 | 10 | 9 |
| runs with an empty final report | 8 | 7 | 8 | 10 | 5 (see below) |
| refused or empty bash calls | 40 | 42 | 51 | 33 | 41 |
| runs with a successful test-shaped bash call | 14 | 13 | 13 | 18 | 16 |
| hint / strip fired | | | 16 | 12 | |
| refusals outside the suffix grammar | | | 19 | 18 | |
| wall, sum (s) | 805 | 847 | 793 | 786 | 798 |
| prompt tokens, sum | 2.93 M | 3.01 M | 2.98 M | 2.79 M | 2.92 M |

**Outcomes.** On 8 of 10 fixtures every arm produced the same patch
(`git patch-id`) in all 3 reps, so the arms cannot differ there. The two
fixtures that vary run to run (neon-rain-implement-reset-shortcut,
nothing-ever-happens-document-config; 30 distinct patches) were
hand-verified: 29 REAL, 1 FAIL (py rep 2, reset-shortcut: the listener is
removed in `_startNewGame` and never re-added, so Shift+R dies at the first
game start; re-checked by hand). ctl itself produces three different patches
on that fixture, and one FAIL in three is below the 3-rep rule: no regression
in any arm.

**py_final's 5.** The final turn fired 9 times. Every time the champion
answered with a tool call (`bash` 6, `read_file` 3) even though no tools
were offered; the server parsed it from the text. Three of those carried a
46 to 78 character preamble ("let me run the tests once more"), which is
what made the report non-empty. None is a report.

## Root cause: the idle test, not the feeders

DEBUG dispatch logs of two real bench cells (`debug/`, captured through a
`sitecustomize.py` on the runner's `PYTHONPATH`, so the trajectories are the
bench's own) show what the gate counts as idle (`bytes_out == 0 or error`):

| Call | What happened | Counted idle |
|---|---|---|
| `python -c "import py_compile; py_compile.compile('pe_scan.py', doraise=True)"` | succeeded, printed nothing | yes |
| `python -c "os.makedirs('/tmp/test_gguf'...); open(...).write(...)"` | succeeded (test fixture setup) | yes |
| `npx eslint src/App.jsx` | exit 2 with 491 bytes of eslint output | yes |
| `cd /tmp && mkdir -p test_gguf && echo ... > ...; echo "Exit code: $?"` | refused (chain) | yes |
| `mkdir -p /tmp/test_gguf` | refused (not allowlisted) | yes |
| `lint` on `src/App.jsx` | refused (Python analyzer, PR #50) | yes |

The champion was verifying: compiling, building a test fixture, running
eslint. Three such steps after a write end the run. The gate was built for
a different model's drift (repeated empty checks after success,
qwen3-coder-next, 2026-05); for the champion it ends real verification.
That explains every result above:

- **py** fixes the 127, and the next verification step (a silent
  `python -c`) is counted idle instead.
- **hint/strip** cover `2>&1`, `|| true`, `| tail`; most refusals here are
  multi-command setups (`cd && mkdir && echo >`) no safe grammar covers
  (19 and 18 outside it).
- **final** asks a model that is mid-verification to stop; it does not.

## Recommendation (not done; needs its own A/B)

Change what counts as idle: a call that EXECUTED is not idle if it
produced output or exited non-zero (both are information), and a silent
success counts only when it repeats an earlier identical call. Refusals
(validator, allowlist, unknown tool) stay idle. That keeps the gate's
original target (repeated empty checks) and stops it ending verification.
Measure against ctl on the champion; the 8 idle-exit runs (lpe strict-flag,
neon reset-shortcut, the-game shuffle-shortcut) are the fixtures to watch.

## Other findings

1. **m5's bench PATH has no `python`.** m1's login PATH has brew's
   `python@3/libexec/bin`; m5's does not, and `python3` is not allowlisted,
   so on m5 every model `python ...` exited 127 in every bench run to date.
   Affects verification on the Python fixtures (lpe, isomer,
   nothing-ever-happens) equally across arms, so past comparisons hold;
   absolute verification behavior on m5 was understated. `LUXE_BENCH_PY_PATH`
   fixes it; the same gap is why 4 tests fail on m5 (they call `python`).
2. **PR #50 adds idle calls.** Python analyzers on `.js` now return a clean
   error instead of ~63 KB of noise; the error counts as idle. Better input
   for the model, worse for the gate. The idle-test change above covers it.
3. **The diag harness lesson.** Two hand-built "same as the bench" diag runs
   did not reproduce the bench trajectory (the bench injects a synthetic
   `forbids_create` `.sdd` and passes other flags). `sitecustomize.py` on the
   runner's `PYTHONPATH` gives DEBUG logs of the real cell.

## What lands

Branch `feat/bash-noop-suffixes`: `LUXE_BENCH_PY_PATH` and
`LUXE_BASH_NOOP=hint|strip` as opt-in levers (off = byte-identical), the
final turn removed (agents.sdd records why), and this record.
