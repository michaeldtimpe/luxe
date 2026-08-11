# LUXE_POST_WRITE_IDLE_REPEATS — second maintain_suite A/B

**Run:** 2026-08-10, m5, `Qwen3.6-35B-A3B-6bit`, 3 reps × 10 fixtures × 2 arms
= 60 runs. Arms sequential (21:24→21:52 baseline, 21:52→22:19 treatment) at
`98e2c22`, current substrate (`truncated_turn_retry` default-ON).
Criteria fixed before the run in
`benchmarks/maintain_suite/variants_pwir_rerun_3rep.yaml`.

## Verdict: **REFUTED — default stays OFF**

The first A/B found the switch inert for lack of opportunity. This one gave it
opportunity and it still did nothing measurable. That is a stronger result than
"inert": the mechanism can fire in this corpus and changes no outcome.

## Results

| | baseline | treatment | Δ |
|---|---|---|---|
| passed | 30/30 | 30/30 | — |
| score | 120/150 | 120/150 | **0** |
| release gate, all 3 reps | true | true | — |
| avg wall | 53.3s | 53.3s | **0.0%** |
| avg tokens | 162,384 | 162,892 | **+0.3%** |
| harness errors | 0 | 0 | — |
| `post_write_idle_exit` firings | **12** | **12** | **0** |

Firings by fixture, identical in both arms:

```
lpe-rope-calc-implement-strict-flag   3
isomer-implement-healthcheck          3
the-game-implement-shuffle-shortcut   3
lpe-rope-calc-document-typing         3
```

## Against the pre-registered criteria

1. **Score unchanged — PASS.** 120/150 both arms, every fixture 3/3.
2. **Wall and/or tokens down — FAIL.** This was the win condition. Wall is
   identical to 0.1s; tokens are 0.3% *higher*, i.e. noise. No saving.
3. **Firings land only after the diff is complete — N/A.** The switch caused
   no additional firing to inspect.
4. **No fixture regresses — PASS.**

Criterion 2 was the whole hypothesis, and it failed. An intervention that
changes no outcome does not earn a default.

## Why the predicted saving did not materialise

The venue check (`../pwir_ab_2026_08_10/SWEBENCH-VENUE-CHECK.md`) predicted
~5.8% of runs would change, and replaying this run's telemetry confirms the
opportunity was real: **3 runs per arm** had a streak that reaches 3 only when
repeats count. But the firing totals are identical, so in those runs the guard
already armed by the zero-byte path — the repeat merely would have armed it at
a slightly different moment, on trajectories that were ending anyway.

The step saved is therefore inside the noise, which is what the wall figure
says: 53.3s versus 53.3s.

## Recommendation

Keep the switch, default OFF, documented as refuted — the same disposition as
`LUXE_RESPOND_TERMINAL` and the trajectory-shape suppressor, both of which
survive in-tree as default-OFF infrastructure with their refutes recorded.
Do not re-bench it on maintain_suite; two A/Bs at 3 reps each now say the same
thing from opposite starting conditions (no opportunity, then opportunity).

The blind spot it closes is real — `read_file` is dedup-exempt, so a repeated
read resets a streak the guard's own docstring claims to catch. It simply does
not matter on any corpus available here. If it is ever revisited, the burden is
to find a workload where post-write repeat streaks reach 3 *without* zero-byte
calls already getting there.

## Note: the first attempt at this A/B was invalid

Run once at 20:38 and discarded. It reported baseline 29/30 (116) vs treatment
27/30 (108), which looks like the switch destroying work. It was not: the
treatment failures were `rc=1` with no run_id and this traceback —

```
luxe.pr.PRError: Cannot find a free branch name based on
  `luxe/document/add-a-config-md-at-the`
```

`plan_branch_name` tries `base` then `-2`…`-99`; the fixture-cache held 98
branches for that slug. Baseline ran first and consumed the last free names, so
the gap was arm ordering, not the switch. See `BRANCH-LEAK.md`.

This run used `branch_prefix: pwir2` against an empty namespace so no history
was deleted; `configs/pr.yaml` was restored by the driver's trap and verified
clean afterwards.
