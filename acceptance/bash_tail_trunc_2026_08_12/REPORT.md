# Bash truncation → head+tail — why this is a confirmation run, not an A/B — 2026-08-12

**Change:** the bash tool's 8 KB output cap kept the FIRST 8,192 bytes; test
runners put the failure summary at the END, so a capped test run reliably
lost exactly the lines the model needed (2026-08-12 audit, F12).
`shell._clip_output` now keeps 2 KB head + 6 KB tail around an announced
omission marker, snapped to line boundaries; byte-identical under the cap.

## The A/B that was asked for is uninterpretable, and here is the measurement

Opportunity check BEFORE building (the C10 discipline):

- **Today's corpus** (six full maintain_suite passes, 183 bash tool calls):
  **zero** calls reach the cap. Largest output: 2,230 bytes — not within 4×
  of the 8,192 threshold. A treatment arm can never fire; both arms would be
  byte-identical trajectories and any delta would be substrate noise
  laundered into a verdict.
- **All recorded runs ever** (5,230 bash calls): 205 firings across 124
  runs — all SWE-bench-era workloads (real repos, real pytest volume). The
  cap matters in practice; maintain_suite just cannot exercise it.

So the evidence stands on three legs instead of two arms:

1. **Mechanism tests** — `tests/test_bash_truncation.py` (9), including the
   founding case: a pytest-shaped 600-test output whose short-summary block
   is provably absent from the old form's first 8 KB and present now.
2. **Byte-identity under the cap** — pinned by test (`is` passthrough).
3. **Suite confirmation (this run)** — 3 reps × 10 fixtures on shipped
   defaults with the clip in place:

| run | passed | score | avg wall | avg tokens |
|---|---|---|---|---|
| standing baseline band (2026-08-12 runs) | 30/30 | 120/150 | 45.6-53.6s | 116.5-123.5k |
| **this run (head+tail clip)** | **30/30** | **120/150** | **45.2s** | **109.4k** |

Identical score, wall at the fast edge of the band, tokens marginally below
it — consistent with zero firings (the change cannot have caused the token
dip; that is rep-to-rep variance on the same-score profile).

**Venue note:** SWE-bench is where this shape change could show score
effects (it produced all 205 historical firings). If SWE-bench is ever
re-benched, the clip is part of the substrate being measured — noted in
tools.sdd.

Same-day sibling fix, same failure class, harness side:
`grade.py` git failures now raise `GitRunError` → fixture ERRORED instead of
grading as "the model changed nothing" (maintain_suite.sdd). Healthy-path
grading byte-identical (tests); this run graded 30/30 with `errored=0` under
the old in-memory grader and the standing profile is unchanged.
