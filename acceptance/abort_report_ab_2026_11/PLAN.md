# Abort report (`LUXE_ABORT_REPORT`, default ON): A/B plan

Status: apparatus only, not run. Branch `feat/abort-report`.

## Why

`acceptance/idle_capped_ab_2026_11/REPORT.md`: when the agent loop ends
early (stuck-loop guard "repeated same tool calls 2 consecutive turns",
post_write_idle_exit, a backend error) the final text is empty, so
`luxe maintain` wrote no report: synthesizer.md, the commit body and the PR
body were empty. On the-game-implement-shuffle-shortcut every arm wrote a
correct patch and then produced no report (capped 5/30 empty, wide 4/30). A
model "final turn" asking for a report was refuted 2026-11-03 (tool call 9/9)
and is not re-added.

## What changed (a reporting change, no model call)

`maintain._abort_report` (after the cleanup turn, before the PR cycle): a
write run whose MODEL text is empty gets a Python-built report from
`luxe/abortreport.py`: a header saying luxe wrote it, not the model, and why
the run ended; files changed vs base; verification calls after the last
successful write with exit status (refusals listed as refused, dedup repeats
as not re-run). No correctness claim. `AgentResult.final_text` and
`single_mode_done.final_text_chars` are untouched, so "empty report" is still
measured on the model's text. Event `abort_report_written` (reason, files,
n_commands); bench field `diagnostics.json` `report_fallback`.

Expected effect on the loop: none. Nothing the model sees changes; the
trajectory, diff and score of every run are the same as with the flag off.
The A/B checks that, measures the fallback in both idle-test arms, and
hand-checks every fallback report.

## Design

- Model: champion `Qwen3.6-35B-A3B-6bit` on oMLX, m5.
- Fixtures: all 10 maintain_suite fixtures, 3 reps, interleaved (each rep
  runs every arm once before the next rep starts).
- Warm-up: before rep 1, one throwaway run of `isomer-implement-healthcheck`
  on shipped defaults into `warmup/` (not counted; analyze.py never reads
  it). The previous A/B's first arm ran in a slow-decode window.
  `WARMUP_ID=<fixture>` picks another, `WARMUP_ID=none` skips it.
- Arms:
  - `ctl`: shipped defaults (abort report ON, `LUXE_IDLE_NARROW` unset).
  - `wide`: `LUXE_IDLE_NARROW=wide` (abort report ON).
- Liveness: the fallback has no announce line; it is live in a run iff that
  run's model text is empty. `analyze.py` prints `empty_report` (model text),
  `fallback` and `no_report`; every run with `empty_report` must also count
  in `fallback`, and the fallback list must show no run with model text.
  `idle_narrow_mode` must be present in every wide run and absent in ctl
  (the `liveness` column).

## Launch (m5, bench worktree on this branch, oMLX up with the champion)

```sh
nohup acceptance/abort_report_ab_2026_11/run_arms.sh Qwen3.6-35B-A3B-6bit champ 3 \
  ctl wide=LUXE_IDLE_NARROW=wide \
  > /dev/null 2>&1 &
```

Progress: `acceptance/abort_report_ab_2026_11/arms.log`. Analysis:
`python3 acceptance/abort_report_ab_2026_11/analyze.py champ` (writes
`runs.json`, prints the per-arm table, per-fixture table, matched and overall
wall, and the list of fallback reports with their synthesizer.md paths).
Then hand-verify every varying patch into `HAND-VERIFY.json` (REAL / THIN /
VACUOUS, same rubric as idle_capped_ab_2026_11) and read every fallback
report against its run's diff and events.jsonl.

## Gates (both arms)

| gate | threshold |
|---|---|
| Model-written empty reports (`empty_report`) | counted per arm, reported separately (wide expected below ctl, not gated) |
| Runs ending with NO report, model or fallback (`no_report`) | 0 / 30 |
| Printed pass | 30 / 30 |
| Hand-verified REAL | >= ctl |
| Fallback reports with a false claim (hand check: files, commands, exit status, reason vs the run's records) | 0 |
| Matched-run wall (pairs where every arm wrote a MODEL report) | <= +5% vs ctl |
| Overall wall | <= +15% vs ctl |

Abort: any arm below 28/30 printed, a fallback on a run that has model text,
or a liveness miss stops the analysis and is diagnosed before anything is
reported. Failures are classified model_behavior / infrastructure /
execution.

## Risks

| risk | mitigation |
|---|---|
| No run aborts, so the fallback never fires and nothing is measured | Expected count from idle_capped_ab: the-game in ctl (and 4/30 in wide). If `fallback` is 0 in both arms, report the gate as unexercised and add a `IDS=the-game-implement-shuffle-shortcut` 3-rep slice before any verdict |
| The fallback reads like a model claim ("tests pass") | Header says luxe wrote it; outcomes are exit codes and quoted last output lines only; hand check gate above |
| A refused or deduped call is reported as run | `abortreport._outcome` lists refusals as "refused, not run" and dedup repeats as "not re-run"; tests pin both |
| Commit body excerpt cuts the header | `pr._commit_body_excerpt` keeps the first 1200 chars; the header is the first line |
| Substrate drift across the ~2 h run | Warm-up run, interleaved arms |
