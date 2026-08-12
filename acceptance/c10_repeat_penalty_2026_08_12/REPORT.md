# C10 re-run — repeat_penalty 1.05, first live measurement — 2026-08-12

**Run:** m5, 2 cells × 8 fixtures = 16 runs, single rep per cell (the
original C10 design), settled 2026-08-12 tree. Script:
`scripts/run_c10_repeat_penalty.sh` (re-pointed here; the hardcoded API key
it used to carry is scrubbed). The 2026-06-11 "no-op" result was RETRACTED
2026-08-11 — `extra_body` never reached the wire, both cells were
byte-identical requests. `252f12f` sends the knob top-level under both
spellings; plumbing verified variant → role → request body
(`tests/test_backend_vendor_fields.py`, 9 pass) before this run.

## Verdict: no-op, this time measured for real. CLOSED, no escalation.

| cell | pass | avg wall | avg prompt tokens | avg completion |
|---|---|---|---|---|
| c10-baseline | 8/8 | 57.9s | 147k | 5,109 |
| c10-rp105 (repeat_penalty=1.05) | 8/8 | 50.7s | 117k | 4,016 |

- **Zero PASS↔FAIL flips** — the pinned escalation rule (flip → 3-rep A/B)
  does not trigger.
- Both cells score 4/5 on every fixture (64/80 total).
- The wall/token deltas favor rp105 but are single-rep readings inside the
  suite's known variance (`nothing-ever-happens-document-config` alone
  swings 58-160s across same-arm reps in today's 3-rep baseline); they are
  not evidence of a win and C10's own priors (failures skew
  termination/long-context, not local repetition) say not to chase them.
- `rc=1` from the runner is a gate artifact, not a failure: the v1 release
  gate wants ≥8 of 10 fixtures per cell and C10 runs 8 by design, so the
  gate can never clear on this subset.

The question C10 asked in 2026-06 — does mild repetition penalty change
champion outcomes on maintain_suite? — is now answered with a live knob:
**no**. Default sampling stays exactly as pinned (temp=0, no penalty).
