# LUXE_TRUNCATED_TURN_RETRY — maintain_suite A/B

**Run:** 2026-08-10, m5, `Qwen3.6-35B-A3B-6bit`, 3 reps × 10 fixtures × 2 arms
= 60 runs. Arms sequential (16:25→16:51 baseline, 16:51→17:19 treatment).
Variants: `benchmarks/maintain_suite/variants_truncated_turn_3rep.yaml`.
Diagnosis that motivated it:
`acceptance/pwir_ab_2026_08_10/STRICT-FLAG-FINDING.md`.

## Verdict: **all four pre-registered criteria met — promotion warranted**

With one caveat stated up front: the win comes entirely from ONE fixture. See
"How narrow is this" below before flipping the default.

## Results

| | baseline | treatment | Δ |
|---|---|---|---|
| passed | 27/30 | **30/30** | +3 |
| score | 111/150 | **120/150** | +9 |
| release gate, all 3 reps | true | true | — |
| avg wall | 49.7s | 53.6s | +7.8% |
| avg tokens | 132,179 | 161,948 | +22.5% |

Per fixture — one improves, nothing else moves:

```
isomer-document-quickstart              3/3 [4,4,4]   3/3 [4,4,4]
isomer-implement-healthcheck            3/3 [4,4,4]   3/3 [4,4,4]
lpe-rope-calc-document-typing           3/3 [4,4,4]   3/3 [4,4,4]
lpe-rope-calc-implement-strict-flag     0/3 [1,1,1]   3/3 [4,4,4]   <-- improved
neon-rain-document-modules              3/3 [4,4,4]   3/3 [4,4,4]
neon-rain-implement-reset-shortcut      3/3 [4,4,4]   3/3 [4,4,4]
nothing-ever-happens-document-config    3/3 [4,4,4]   3/3 [4,4,4]
nothing-ever-happens-manage-deps-audit  3/3 [4,4,4]   3/3 [4,4,4]
the-game-document-architecture          3/3 [4,4,4]   3/3 [4,4,4]
the-game-implement-shuffle-shortcut     3/3 [4,4,4]   3/3 [4,4,4]
```

30/30 restores the suite to the level the m5max_moe bake-off recorded.

## Criteria, as written before the run

**1. Score up — PASS.** 111 → 120, exactly the predicted +9 (strict-flag 1/5 →
4/5 across three reps). 4/5 with 2 files changed is what the 2026-05-10
bake-off scored on this fixture.

**2. No baseline-clean fixture regresses — PASS.** Every fixture passing 3/3 in
baseline passes 3/3 in treatment. This was the disqualifying criterion: the
nudge injects a message mid-run, and derailing healthy trajectories would have
sunk it regardless of score. 27 unaffected runs say it does not.

**3. Cost bounded under ~2× — PASS.** Tokens +22.5%, wall +7.8%, both far
under the ceiling. The single-fixture check had cost 14× on the fixture that
fires; suite-wide it is small because the trigger is rare.

**4. Every firing inspected — PASS.** Three firings, all on
lpe-rope-calc-implement-strict-flag, all at `completion_tokens = 8192` —
exactly `max_tokens_per_turn`, i.e. genuinely cut off. **Zero spurious
firings** on trajectories that ended normally.

## The telemetry tells the story cleanly

`terminal_turn_truncated` is ungated, so it fires in both arms and gives the
denominator:

| | baseline | treatment |
|---|---|---|
| `truncated_turn_retry` (nudged) | 0 | **3** |
| `terminal_turn_truncated` (still ended cut off) | **3** | **0** |

Baseline: three runs ended truncated and were reported as clean completions —
the bug. Treatment: those same three were nudged, and **none** ended truncated.
All three recovered on `retry#1`; the second retry in the bound was never
needed.

That the two columns swap exactly, 3 ↔ 0, is what an effective and
precisely-targeted intervention looks like.

## How narrow is this

The honest limitation. Across 60 runs the mechanism fired **3 times, all on one
fixture**. So:

- The **no-harm** evidence is solid: 27 other runs, 3 reps, untouched.
- The **it-helps** evidence is n=1 fixture. maintain_suite contains exactly one
  trajectory that hits the token cap on an `implement` task, and this fixes it.

That is a real fix for a real deterministic failure, not a tuning artifact —
but it does not demonstrate the intervention generalises, because the suite has
nothing else to generalise to. Wider evidence would need SWE-bench, where
capped turns are likelier on larger repos.

## Recommendation

Flip `LUXE_TRUNCATED_TURN_RETRY` to default ON. The pre-registered criteria are
met, the change is targeted (fires only on `finish_reason == "length"` with no
tool call), bounded (2 retries), and the failure it fixes is otherwise
invisible — a run that produced nothing reported as a clean completion.

If the narrowness argues for caution instead, the intermediate step is to leave
the default OFF and enable it for the bench harness only
(`env.setdefault("LUXE_TRUNCATED_TURN_RETRY", "1")` in
`benchmarks/maintain_suite/run.py`, beside the existing `LUXE_WRITE_PRESSURE`
line), which buys the suite-level fix without changing the interactive path.

Keep `terminal_turn_truncated` ungated either way. It costs nothing and it is
the only thing that distinguishes "finished" from "cut off" in the records.

## Reproduce

```bash
uv run python -m benchmarks.maintain_suite.run --all \
  --variants benchmarks/maintain_suite/variants_truncated_turn_3rep.yaml \
  --output acceptance/truncated_turn_ab_2026_08_10/baseline/ --per-fixture-timeout 1800

LUXE_TRUNCATED_TURN_RETRY=1 uv run python -m benchmarks.maintain_suite.run --all \
  --variants benchmarks/maintain_suite/variants_truncated_turn_3rep.yaml \
  --output acceptance/truncated_turn_ab_2026_08_10/treatment/ --per-fixture-timeout 1800
```

Telemetry is not under the output dir: each fixture's `state.json` carries a
`luxe_run_id`; events live in `~/.luxe/runs/<id>/events.jsonl`.
