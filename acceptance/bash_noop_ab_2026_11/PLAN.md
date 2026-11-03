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

## Design

- Champion `Qwen3.6-35B-A3B-6bit`, all 10 maintain_suite fixtures, 3 reps,
  arms ctl / hint / strip interleaved per rep. A fresh ctl: the oMLX server
  was restarted (0.7.3rc1 to 0.7.3) between the previous A/B and this one.
- Slice first: 1 rep x {lpe-rope-calc-implement-strict-flag,
  neon-rain-implement-reset-shortcut} x {hint, strip}, reading the
  `bash_noop_stats` events and the reports.
- Hand-verify: a patch whose `git patch-id` matches one already classified in
  `acceptance/validation_context_ab_2026_11/HAND-VERIFY-champ.json` inherits
  that verdict; every new patch is read.

## Measurements

From `runs.json` (analyze.py, copied) plus the new events: `bash_noop_stats`
(hint / strip / unfixable counts per run), refused or empty bash calls,
`post_write_idle_exit`, empty final reports, successful test bash calls,
hand-verified class, wall, prompt tokens.

## Gates

- **Abort:** any treatment run without `bash_noop_mode`; an infra failure in
  more than 1 run in an arm; the slice shows a cleaned command that is not a
  prefix of the request.
- **Regression (3-rep rule):** a fixture REAL 3/3 in ctl drops to 1/3 or
  less; hand-verified REAL drops by more than 1; wall +15% or more.
- **Success:** no regression, and runs ending with an empty report fall from
  ctl's count to 1/30 or fewer, and refused/empty bash calls fall by 50% or
  more.
- **Verdict:**
  - `hint` succeeds: promote default-ON (an error-string change).
  - Only `strip` succeeds: report it and ask before promoting (security note).
  - Both succeed: promote `hint`, and report `strip`'s margin over it.
  - Neither: keep opt-in, record why.

## Risks

| Risk | Mitigation |
|---|---|
| strip runs something the model did not intend | Prefix-only grammar, re-validated; tests show a chain before the suffix is still refused and never runs |
| hint adds a call per refusal, so wall rises | Wall gate; the trap fires on 2 to 3 fixtures |
| The model's next habit (`| grep FAIL`) is outside the grammar | Counted as `unfixable`; reported, grammar not widened mid-run |
| Fixing the report changes the step count, so compaction shifts | patch-id comparison against ctl per fixture |
