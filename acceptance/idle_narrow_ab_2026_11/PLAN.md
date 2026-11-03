# Narrow idle test for post_write_idle_exit: A/B plan

User request 2026-11-03 ("start the idle-check A/B"), the recommendation in
`acceptance/bash_noop_ab_2026_11/REPORT.md`.

## The problem

`post_write_idle_exit` ends a run after 3 consecutive idle calls once a write
has landed, with no final turn and so no report. Idle today means
`bytes_out == 0 or error`. DEBUG logs of real bench cells
(`acceptance/bash_noop_ab_2026_11/debug/`) show the champion's verification
being counted idle: a silent `python -c "py_compile..."` that succeeded, a
silent fixture-setup `python -c`, and `npx eslint` exiting 2 with 491 bytes
of output. In that A/B, 8 to 10 runs per arm (of 30) ended this way, on
lpe-rope-calc-implement-strict-flag, neon-rain-implement-reset-shortcut and
the-game-implement-shuffle-shortcut.

## Lever (`LUXE_IDLE_NARROW=1`, opt-in; `agents/loop.py` `_narrow_idle_spares`)

A post-write call the legacy test counts idle is spared when it:

- returned output (any exit status): `output`
- exited non-zero from a command that ran (`exit code N`, N not 126/127):
  `nonzero_exit`
- succeeded silently: `silent_success`

It stays idle when it returned nothing and nothing ran: validator or
allowlist refusal, analyzer refusal, unknown tool, timeout, exit 126/127.
Identical repeats are unchanged (the dedup short-circuit counts them idle;
back-to-back repeats hit the stuck-loop guard first). Off = byte-identical.
Events: `idle_narrow_spared(step, name, reason, bytes_out)` per spare,
`idle_narrow_mode` once per run (liveness).

## Design

Champion `Qwen3.6-35B-A3B-6bit`, m5, all 10 maintain_suite fixtures, 3 reps,
arms interleaved per rep. Bench tree = `feat/idle-narrow` at the commit that
adds this plan (main `7bc3bfc` + the lever).

| Arm | Env |
|---|---|
| ctl | shipped defaults (now includes `python` on the bench PATH, `7bc3bfc`) |
| narrow | `LUXE_IDLE_NARROW=1` |

A fresh control is needed: the shipped defaults changed today
(`LUXE_BENCH_PY_PATH` default ON). The earlier `py` arm
(bash_noop_ab_2026_11) is the same configuration and serves as a sanity
check on ctl (it had 9 idle exits, 7 empty reports).

Slice first: 1 rep x {lpe strict-flag, neon reset-shortcut, the-game
shuffle-shortcut} x narrow. Read the events (spares fire, no idle exit or a
justified one, a report exists) before the full run.

Hand-verify: patch-ids already classified in
`acceptance/bash_noop_ab_2026_11/HAND-VERIFY.json` and
`acceptance/validation_context_ab_2026_11/HAND-VERIFY-champ.json` inherit
their verdict; every new patch is read. Also read every narrow report that
replaced an empty one: does it describe the checks it actually ran?

## Measurements (`analyze.py` -> `runs.json`)

Per run: pass, patch-id, `post_write_idle_exit`, empty final report, steps,
calls after the first write, `idle_narrow_spared` by reason, step-limit
label, aborted, wall, prompt tokens, liveness (`bench_venv_path`,
`idle_narrow_mode`).

## Gates

- **Abort:** a narrow run without `idle_narrow_mode`; a ctl run with
  `bench_venv_path` off; an infrastructure failure in more than 1 run per arm.
- **Regression (narrow vs ctl, 3-rep rule):** a fixture REAL 3/3 in ctl
  drops to 1/3 or less; hand-verified REAL drops by more than 1; wall sum
  +15% or more; 2 or more runs more than ctl end at max steps (step-limit
  label) or aborted (the drift risk: the gate no longer ends a model that
  keeps running commands with output after it is done).
- **Success:** no regression, and empty final reports fall to 2/30 or fewer
  (ctl is expected near 8), and `post_write_idle_exit` falls by at least half.
- **Verdict:** success -> recommend default-ON (`LUXE_IDLE_NARROW` flips to
  "exact 0 disables"); landing it is the user's call. Otherwise it stays
  opt-in with the record, or is removed if it regresses.

## Risks

| Risk | Mitigation |
|---|---|
| Runs no longer end early, so wall rises | Wall gate (+15%); steps and calls-after-write measured per run |
| A model that is done keeps running distinct commands until max_steps | Step-limit / aborted gate; max_steps and step-limit labelling remain the backstop |
| More post-write steps shift compaction, so patches change | patch-id comparison per fixture; every new patch hand-read |
| The report that replaces an empty one fabricates checks | Read each such report against its tool calls (the menu A/B showed this failure mode for gemma) |
