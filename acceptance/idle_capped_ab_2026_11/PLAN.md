# Capped refusal spares (`LUXE_IDLE_NARROW=capped`): A/B plan

Status: apparatus only, not run. Branch `feat/idle-capped`.

## Why

`acceptance/idle_narrow_ab_2026_11` (ctl vs wide, champion, 10 x 3): wide
took empty reports 7 -> 0 at 30/30, but overall wall was +18.2% (gate +15%)
and prompt tokens +30%. Most of the extra wall came from three fixtures where
ctl was cut off with no report (neon-rain-implement-reset-shortcut,
the-game-implement-shuffle-shortcut, lpe-rope-calc-implement-strict-flag):
wide spent the time finishing them. Restricted to the 23/30 (fixture, rep)
pairs where both arms wrote a report, wide was +2.9% wall and +9.4% prompt
tokens (recomputed from that A/B's runs.json with this directory's
`analyze.py`).

capped keeps wide's recovery while bounding a run that keeps getting refused:
refusals are spared only up to a per-run budget `LUXE_IDLE_REFUSAL_SPARES`
(default 4), after which a refusal counts idle again
(`idle_refusal_budget_spent` is emitted once).

## Why K = 4

From the wide arm's events (`~/.luxe/runs/<id>/events.jsonl`):

| group | runs | refusal spares per run | max consecutive refusal streak |
|---|---|---|---|
| ctl had an empty report | 7 | 2, 4, 5, 5, 5, 5, 6 | 1, 2, 2, 2, 2, 5, 5 |
| ctl wrote a report | 23 | 0 x18, 1 x3, 2 x2 | 0 x18, 1 x5 |

Replaying each wide run's post-write call stream under a budget K (write
resets the streak, a dedup repeat is idle, a refusal past the budget is idle,
anything else resets), the idle exit would fire in 6 runs at K = 0..2, 3 runs
at K = 3 (all three the-game reps, one call earlier than they finished), and
0 runs at K >= 4. All the exits are in runs where ctl had an empty report. So
4 is the smallest budget that reproduces wide's outcome on every observed run.
It has no slack on the-game (K = 3 fails there), so the A/B is also the test
of whether 4 holds under rep variance. A replay assumes the trajectory up to
the exit point is unchanged, which holds because the budget changes nothing
the model sees until the exit.

## Design

- Model: champion `Qwen3.6-35B-A3B-6bit` on oMLX, m5.
- Fixtures: all 10 maintain_suite fixtures, 3 reps, interleaved (each rep
  runs every arm once before the next rep starts).
- Arms:
  - `ctl`: shipped defaults (`LUXE_IDLE_NARROW` unset).
  - `wide`: `LUXE_IDLE_NARROW=wide` (re-run so it shares substrate hours).
  - `capped`: `LUXE_IDLE_NARROW=capped`, budget default 4.
- Liveness: `idle_narrow_mode` (mode, and `refusal_spares` for capped) must
  be present in every wide and capped run and absent in ctl; `analyze.py`
  prints it in the `liveness` column.

## Launch (m5, bench worktree on this branch, oMLX up with the champion)

```sh
nohup acceptance/idle_capped_ab_2026_11/run_arms.sh Qwen3.6-35B-A3B-6bit champ 3 \
  ctl wide=LUXE_IDLE_NARROW=wide capped=LUXE_IDLE_NARROW=capped \
  > /dev/null 2>&1 &
```

Progress: `acceptance/idle_capped_ab_2026_11/arms.log`. Analysis:
`python3 acceptance/idle_capped_ab_2026_11/analyze.py champ` (writes
`runs.json`, prints the per-arm table, per-fixture table, matched and overall
wall). Then hand-verify every varying patch into `HAND-VERIFY.json`
(REAL / THIN / VACUOUS, same rubric as idle_narrow_ab_2026_11).

## Gates (capped; wide is reported against the same gates)

| gate | threshold |
|---|---|
| Empty reports | <= 1 / 30 |
| Printed pass | 30 / 30 |
| Hand-verified REAL | >= ctl |
| Matched-run wall (pairs where every arm wrote a report) | <= +5% vs ctl |
| Overall wall | <= +15% vs ctl |

Abort: any arm below 28/30 printed, or a liveness miss, stops the analysis
and is diagnosed before anything is reported. Failures are classified
model_behavior / infrastructure / execution.

## Risks

| risk | mitigation |
|---|---|
| K = 4 has no slack on the-game (exits at K = 3 in replay) | Per-fixture table shows `budget_spent` and empty reports; if the-game ends empty under capped, re-run that fixture with `LUXE_IDLE_REFUSAL_SPARES=5` before any verdict |
| capped equals wide on every run, so it cannot fix the overall wall | Expected if no run spends its budget; then the verdict rests on the matched-run gate and overall wall is reported as the price of recovered reports |
| Substrate drift across the ~3 h run | Interleaved arms; ctl re-run in the same hours |
