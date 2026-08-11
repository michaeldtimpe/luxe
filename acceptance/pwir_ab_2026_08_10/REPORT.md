# LUXE_POST_WRITE_IDLE_REPEATS — maintain_suite A/B

**Run:** 2026-08-10, m5, `Qwen3.6-35B-A3B-6bit`, 3 reps × 10 fixtures × 2 arms
= 60 runs. Arms sequential (14:32→14:54 baseline, 14:54→15:14 treatment).
Variants: `benchmarks/maintain_suite/variants_pwir_3rep.yaml`.

## Verdict: **default stays OFF — the switch is INERT on this suite**

Not "it regressed". Not "it helped". The bench had **no opportunity to
exercise it**, so it cannot promote the default. Criteria were written into
the variants file before the run; this outcome fails the one that mattered.

## Results

| | baseline | treatment |
|---|---|---|
| passed | 27/30 | 27/30 |
| score | 111/150 | 111/150 |
| release gate, all 3 reps | true | true |
| `post_write_idle_exit` firings | **9** | **9** |
| avg wall | 40.8s | 37.6s |
| avg tokens | 93,387 | 96,493 |

Per-fixture scores are **identical across all six passes** — every fixture,
every rep, both arms:

```
isomer-document-quickstart              3/3 [4,4,4]   3/3 [4,4,4]
isomer-implement-healthcheck            3/3 [4,4,4]   3/3 [4,4,4]
lpe-rope-calc-document-typing           3/3 [4,4,4]   3/3 [4,4,4]
lpe-rope-calc-implement-strict-flag     0/3 [1,1,1]   0/3 [1,1,1]
neon-rain-document-modules              3/3 [4,4,4]   3/3 [4,4,4]
neon-rain-implement-reset-shortcut      3/3 [4,4,4]   3/3 [4,4,4]
nothing-ever-happens-document-config    3/3 [4,4,4]   3/3 [4,4,4]
nothing-ever-happens-manage-deps-audit  3/3 [4,4,4]   3/3 [4,4,4]
the-game-document-architecture          3/3 [4,4,4]   3/3 [4,4,4]
the-game-implement-shuffle-shortcut     3/3 [4,4,4]   3/3 [4,4,4]
```

## Why it is inert — the measurement that decides it

The guard needs **3 consecutive** idle calls (`_POST_WRITE_IDLE_MAX = 3`).
Counting post-write repeat calls (same `key_hash` as an earlier call in the
same run, after the first write) across all 60 runs:

| | baseline | treatment |
|---|---|---|
| total post-write repeats | 12 | 13 |
| runs containing ≥1 | 12/30 | 13/30 |
| **max per run** | **1** | **1** |

Every run that has a post-write repeat has **exactly one**, and no run pairs it
with two adjacent 0-byte/error calls, so none reaches the threshold of 3.
Re-verified offline afterwards: would-change is **0/60** for this sample.

> **Correction (2026-08-10).** An earlier wording here said a lone repeat "can
> never arm the guard by itself". That is wrong as a general claim — a repeat
> topping up an existing 2-streak reaches 3, and that is exactly how the
> pattern shows up in later August runs. The empirical result for THIS sample
> (0/60) is unaffected. See `SWEBENCH-VENUE-CHECK.md`. The identical firing count (9 = 9) confirms
it: the switch never converted a 2-streak into a 3-streak.

That also explains the identical scores. The arms are not "statistically
indistinguishable"; they are **behaviourally identical** on this suite.

The wall (−7.8%) and token (+3.3%) deltas are not attributable to the switch,
since no firing decision changed. They are substrate noise.

## What this does and does not establish

- **Establishes:** enabling the switch does no harm on maintain_suite. Score,
  per-fixture outcomes, failure set and gates are unchanged across 3 reps.
- **Does not establish:** any benefit, or that the mechanism is correct under
  load. maintain_suite trajectories simply do not contain the repeated-read
  pattern the switch targets — the fixtures are small, and the model reaches
  its diff without re-reading the same file three times.
- **Recommendation:** keep it opt-in, default OFF. Promoting a default on
  "changed nothing measurable" adds risk without evidence. It remains
  available for the interactive path, where the pattern does occur (the m1
  code drill shows a post-edit repeated read; see agents.sdd).

If the switch is ever to be promoted, it needs a corpus where the pattern is
present. **That was checked offline on 2026-08-10 and SWE-bench is NOT it**
(0.8% of 896 runs, 0% in the most recent month) — maintain_suite today is, at
5.8%. See `SWEBENCH-VENUE-CHECK.md`.

## Unrelated finding — investigated separately

**Diagnosed in `STRICT-FLAG-FINDING.md` (same directory).** Short version:
the model absorbs the 3-file repo in 3 tool calls, emits a 36,838-char
planning monologue, hits `max_tokens_per_turn: 8192` exactly, and is cut
off mid-word. That truncated response has no tool calls, so the loop takes
its terminal path — `loop.py` never inspects `finish_reason`, so a cut-off
turn is indistinguishable from a finished one. Original notes follow.


`lpe-rope-calc-implement-strict-flag` fails **6/6** (0/3 in both arms), score
1/5, with the same cause each time:

```
luxe produced no diff vs base_sha — regex_present outcome NOT credited
diff_produced: false · diff_files: 0 · gates_triggered: []
```

The model produces no diff at all. No gate fired — it is not a bailout, it
simply does not write. This is independent of the switch (identical in both
arms) and predates it. The m5max_moe bake-off (2026-05-10) recorded this
fixture passing on the champion with 15 tool calls and zero final prose; today
it makes 3 calls and 36,838 chars of it.

Suite state is therefore 27/30 (111/150) on the champion today, versus the
30/30 (120/150 across three models) recorded at the bake-off.

## Reproduce

```bash
# baseline
uv run python -m benchmarks.maintain_suite.run --all \
  --variants benchmarks/maintain_suite/variants_pwir_3rep.yaml \
  --output acceptance/pwir_ab_2026_08_10/baseline/ --per-fixture-timeout 1800

# treatment
LUXE_POST_WRITE_IDLE_REPEATS=1 uv run python -m benchmarks.maintain_suite.run --all \
  --variants benchmarks/maintain_suite/variants_pwir_3rep.yaml \
  --output acceptance/pwir_ab_2026_08_10/treatment/ --per-fixture-timeout 1800
```

Telemetry is NOT under the output dir — each fixture's `state.json` carries a
`luxe_run_id`, and the events live in `~/.luxe/runs/<id>/events.jsonl`.
