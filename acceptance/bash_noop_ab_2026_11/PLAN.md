# Bash no-op suffixes (redirect trap): A/B plan

User request 2026-11-03, after validation_context_ab_2026_11 surfaced it.

## The trap

The champion runs tests as `npm test 2>&1`, `npm test 2>&1 | tail -40`,
`npm test --silent 2>&1 || true`. The restricted bench bash refuses every
redirect, pipe and chain. Each refusal is an idle call (`bytes_out == 0` or an
error), and three in a row end the run through `post_write_idle_exit` with no
final turn, so no report. validation_context_ab_2026_11 control: 48 refused or
empty bash calls in 30 runs, and 6/30 runs ended with an empty report
(lpe-rope-calc-implement-strict-flag 3/3, neon-rain-implement-reset-shortcut
3/3). Outcome scores were unaffected (graders read the diff); the report is
lost, along with whatever verification the model meant to do.

## Lever (`LUXE_BASH_NOOP`, opt-in, set by maintain only; `tools/shell.py`)

Each recognized suffix is a no-op for this tool: output is already stdout and
stderr, already clipped to head + tail, and a non-zero exit is reported while
the output is still returned. Only trailing `2>&1`, `|| true` and
`| tail/head [-N]` are peeled off; the remainder must pass the full validator
and allowlist again, so what runs is always a prefix of the request.

- **hint:** keep the refusal and append why plus `Run instead: <cleaned>`.
  Costs the model one more call; nothing executes.
- **strip:** run the cleaned command, with a one-line note before its output.

Unset = byte-identical (golden tests unchanged). Chat never sets it.

**Security note for promotion:** `strip` executes a command the validator
refused as written. It is the validated prefix, so the allowlist and chain
rules are unchanged in effect, but it is a relaxation of "a refused command
never runs", and promoting it is the user's call regardless of the numbers.
`hint` changes only an error string.

## Re-scope after the slice (user decision 2026-11-03)

The first slice (hint/strip on lpe strict-flag + neon reset) still ended all
4 runs via `post_write_idle_exit`. DEBUG dispatch logs showed three feeders,
not one:

1. **`python` exits 127 on m5's bench PATH** (brew ships `python3` only;
   `python3` is not allowlisted). The model then tries `python3` (refused)
   and `which python python3 2>/dev/null || ...` (refused). m1's login PATH
   has `python` (brew `python@3/libexec/bin`), m5's does not, so every m5
   bench run so far gave the model a broken `python`.
2. **Python analyzers refuse `.js` files** (PR #50). The refusal is an error,
   errors count as idle, and 4 such calls in one step trip the gate.
3. **Redirect/chain habits** (`2>&1`, `|| true`, `| tail`): the lever above.

The common sink is the gate ending the run on an acting turn, with no
report. User chose: fix the environment first as an opt-in arm (not a new
default), then test the suffix lever and a loop-level fix on top of it.

## Levers (all opt-in, unset = byte-identical)

- `LUXE_BENCH_PY_PATH=1`: luxe's venv bin first on the bench bash PATH (the
  prepend chat already does), so `python` and `pytest` resolve on every host.
- `LUXE_BASH_NOOP=hint|strip`: as above.
- `LUXE_IDLE_EXIT_FINAL_TURN=1`: when (only) `post_write_idle_exit` ends the
  run, one more `backend.chat` with no tools and a fixed "write your final
  report now" message (prompts.py); its text becomes the report.

## Design

Champion, all 10 fixtures, 3 reps, interleaved per rep, 5 arms:

| Arm | Env |
|---|---|
| ctl | none |
| py | `LUXE_BENCH_PY_PATH=1` |
| py_hint | py + `LUXE_BASH_NOOP=hint` |
| py_strip | py + `LUXE_BASH_NOOP=strip` |
| py_final | py + `LUXE_IDLE_EXIT_FINAL_TURN=1` |

Slice first: 1 rep x {lpe strict-flag, neon reset} x {py, py_final},
checking `bench_venv_path` / `idle_exit_final_turn` events and the reports.
Hand-verify: patch-ids already classified in
`acceptance/validation_context_ab_2026_11/HAND-VERIFY-champ.json` inherit
their verdict; every new patch is read.

## Measurements

From `runs.json` (analyze.py, copied) plus the new events: `bash_noop_stats`
(hint / strip / unfixable counts per run), refused or empty bash calls,
`post_write_idle_exit`, empty final reports, successful test bash calls,
hand-verified class, wall, prompt tokens.

## Gates

- **Abort:** a treatment run missing its liveness event (`bench_venv_path`,
  `bash_noop_mode`); an infra failure in more than 1 run in an arm.
- **Regression (3-rep rule, each arm vs ctl):** a fixture REAL 3/3 in ctl
  drops to 1/3 or less; hand-verified REAL drops by more than 1; wall +15%
  or more.
- **py success:** no regression, plus (a) a DEBUG-logged diag run on lpe
  strict-flag shows `python ...` succeeding (events carry no error text, so
  127 is not visible in the full run), and (b) `post_write_idle_exit` on the
  Python fixtures (lpe, isomer, nothing-ever-happens) falls against ctl.
- **Report fix success (hint / strip / final, judged against py, since they
  all include it):** no regression vs ctl, and runs ending with an empty
  report fall to 1/30 or fewer.
- **Verdict:**
  - py succeeds: promote it default-ON as an environment fix, and record
    that m5 baselines before it ran with a broken `python`.
  - final succeeds: promote it (a loop change, but tool-less and only after a
    gate exit).
  - hint succeeds: promote it (an error string).
  - strip: report it; ask before promoting (security note above).

## Risks

| Risk | Mitigation |
|---|---|
| strip runs something the model did not intend | Prefix-only grammar, re-validated; tests show a chain before the suffix is still refused and never runs |
| hint adds a call per refusal, so wall rises | Wall gate; the trap fires on 2 to 3 fixtures |
| The model's next habit (`| grep FAIL`) is outside the grammar | Counted as `unfixable`; reported, grammar not widened mid-run |
| Fixing the report changes the step count, so compaction shifts | patch-id comparison against ctl per fixture |
