# Failed-command output (`LUXE_FAILED_OUTPUT=1`, opt-in): A/B plan

Status: apparatus only, not run. Branch `feat/failed-output`.

## Why

`src/luxe/agents/loop.py` `_dispatch_one` sends `executed.error or
executed.result` as the tool message. `tools/shell.py` returns
`(output, "exit code N")` on a non-zero exit (both exec paths), so since v1.0
the model has seen only `exit code N` for every failing pytest, npm test,
eslint or build, never the output. `acceptance/swebench/diag_misses_2026_11/
REPORT.md`: 18 calls with 82 to 2289 bytes of output hidden; 11/34 misses ran
a test-like command, every one with its output hidden or an import failure.
`acceptance/abort_report_ab_2026_11/REPORT.md` shows the same exit-code-only
records on maintain_suite.

## What changed

With the flag on, a tool that returns a non-empty error AND a non-empty
result sends `error + "\n" + result`, after secret redaction and through the
same clamp (`LUXE_TOOL_RESULT_CLAMP`, its own flag) as any result. Error-only
and result-only calls are unchanged. Event `failed_output_shown` (step, name,
bytes) when the flag changed the content. Bench and chat share
`_dispatch_one`, so the flag covers both. Off is byte-identical (golden
request and trajectory tests unchanged). Unlike abort_report this changes
what the model sees on every path, so it is an arm, not a default.

## Part 1: maintain_suite no-harm check

maintain_suite rarely runs a failing test (most fixtures pass their tests on
the first write, and many test commands are refused by the bash validator),
so this run is a no-harm check, not the efficacy test. Expect few
`failed_output_shown` events; a low count is not a failure of the arm.

- Model: champion `Qwen3.6-35B-A3B-6bit` on oMLX, m5.
- Fixtures: all 10 maintain_suite fixtures, 3 reps, interleaved (each rep
  runs every arm once before the next rep starts).
- Warm-up: before rep 1, one throwaway run of `isomer-implement-healthcheck`
  on shipped defaults into `warmup/` (not counted). `WARMUP_ID=none` skips.
- Arms:
  - `ctl`: shipped defaults (flag unset).
  - `fo`: `LUXE_FAILED_OUTPUT=1`.
- Liveness: `fo_shown` must be 0 in every ctl run (by construction). In fo it
  counts the calls where the flag changed the content; if it is 0 across all
  30 fo runs the arm was unexercised, and Part 1 is reported as no-harm only.
- Measured per arm (`analyze.py`): `fo_shown`, `fo_runs`, `fo_bytes`; bash
  calls after the LAST write, split into ran (bytes_out > 0) and refused or
  empty (bytes_out 0); runs with at least one post-last-write bash that ran;
  prompt tokens (per-arm table, matched and overall).

### Launch (m5, bench worktree on this branch, oMLX up with the champion)

```sh
nohup acceptance/failed_output_ab_2026_11/run_arms.sh Qwen3.6-35B-A3B-6bit champ 3 \
  ctl fo=LUXE_FAILED_OUTPUT=1 \
  > /dev/null 2>&1 &
```

Progress: `acceptance/failed_output_ab_2026_11/arms.log`. Analysis:
`python3 acceptance/failed_output_ab_2026_11/analyze.py champ` (writes
`runs.json`, prints per-arm, per-fixture, matched and overall tables). Then
hand-verify every varying patch into `HAND-VERIFY.json` (REAL / THIN /
VACUOUS / DAMAGING, same rubric as abort_report_ab_2026_11).

### Gates

| gate | threshold |
|---|---|
| Printed pass, fo | 30 / 30 |
| Hand-verified REAL | fo >= ctl |
| New DAMAGING (fo patch DAMAGING where ctl's same fixture was not) | 0 |
| Overall wall | fo <= +15% vs ctl |
| Prompt tokens (per-arm, matched, overall) | reported, not gated |
| `fo_shown` in ctl | 0 (liveness) |

Abort: fo below 28/30 printed, any `fo_shown` in ctl, or a secret
redaction event that names a value in fo output stops the analysis for
diagnosis before anything is reported. Failures are classified
model_behavior / infrastructure / execution.

## Part 2: SWE-bench slice (the real test)

Subset `benchmarks/swebench/subsets/fo_slice_2026_11.json` (25 ids):

- 13 flip targets: the C/D instances named in diag_misses REPORT.md section
  4, lever 1 (3 D: requests-2931, requests-5414, astropy-13236; 6 C with
  P2P regressions: astropy-13453, -13977, pylint-6528, pytest-10051,
  seaborn-3187, sympy-13091; 4 C with a runnable repro in the issue:
  astropy-14182, matplotlib-24870, sphinx-7462, django-10999). The report's
  prose says "12 C/D" but its lever row names these 13; all 13 are kept.
- 12 no-harm: the instances resolved in the diag_misses control rep.
- `per_instance` = the 9-rep prior record; `per_instance_role` =
  flip_target | no_harm.

Arms `control` (shipped defaults) and `fo` (`LUXE_FAILED_OUTPUT=1`), 3 reps
interleaved, each arm-rep graded immediately after it runs.

```sh
nohup acceptance/failed_output_ab_2026_11/swebench_slice.sh 3 > /dev/null 2>&1 &
```

Run it from the main checkout (`cd ~/Downloads/luxe-bench` happens inside;
that bench worktree must be on this branch). Output:
`acceptance/failed_output_ab_2026_11/swebench/<arm>-rep<N>/` (predictions +
`harness/harness_summary.json`), `arms.log` start/end/DONE lines,
`grade.log`.

Gates: flips counted against the 9-rep prior; regressions on the 12 no-harm
instances at most 1 net loss across 3 reps; every new PASS read by hand.
Caveat carried from diag_misses: bash on the bench path runs in the host
venv, not the instance image, so many test commands fail at import or
collection. The flag shows that failure's output too, which is part of what
is measured, but a flat result may mean the channel is still closed by the
environment (lever 1b), not that the output is useless.

Promotion (default ON) still requires the n=75 x 3 run against the 42/75
gate and a passing Part 1.

## Risks

| risk | mitigation |
|---|---|
| Long failing output floods context | Shell already clips to head+tail (`_clip_output`); TieredCompact unchanged; clamp available; prompt tokens reported per arm |
| Output carries a secret | Redaction runs on result and error before the content is built; test plants a fake value (`tests/test_failed_output.py`) |
| Arm unexercised on maintain_suite | Expected; Part 1 is no-harm only, Part 2 is the efficacy test |
| `abortreport` parses `^exit code` from the error field | Unchanged: it reads `ToolCall.error`, which the flag does not touch; the content also still starts with the error line |
| Earlier A/B verdicts (bash_noop, idle_capped) were measured with output hidden | Noted in diag_misses REPORT; not re-run here |
