# docs/research/ — index

Read-only audit artifacts. Most files were produced outside the main
development loop and preserved here so future cycles have the evidence
trail self-contained in the repo.

**Authority classes** (so future contributors don't mistake superseded
drafts for active guidance):

## Authoritative analyses (load-bearing conclusions)

The 2026-05-25 agentic-patterns audit. The conclusions inform the G1
context-lifecycle design (`docs/g1-context-lifecycle-design.md`) and the
2026-05-26 drift fixes (commit `7e896f4`).

- `agentic-patterns-exploration-plan.md` — top-level read map; defines
  E1–E5 investigation tracks and recalibrates the wiki-vs-luxe scorecard.
- `e1-context-cliff-report.md` — characterizes the SWE-bench
  `EMPTY_PATCH_CONTEXT_EXHAUSTED` ceiling (80/324 empty_patch rows,
  ~5% of the full bench, stable across 11 versions). The empirical
  basis for G1.
- `e5-instruction-contract-drift-report.md` — the 8-item drift audit
  (D1–D8); D1–D3 shipped in commit `7e896f4`.
- `a-empty-patch-breakdown.md` — splits the empty_patch tier into
  cliff (~25%) and non-cliff (~75%) slices; argues they need different
  interventions.
- `f-intervention-effectiveness.md` — outcome distribution by intervention
  class. Descriptive, not causal; flagged for that explicitly.

## Superseded proposals (historical record only)

The audit drafted three text patches. The actual changes shipped in
commit `7e896f4` (drift fixes D1+D2+D3) — these drafts are kept as the
"what was proposed" snapshot. **Do not apply.**

- `proposed-AGENTS.md` — superseded by the new `agents.md` (commit
  `7e896f4`). The shipped version corrects the audit draft on one point:
  all in-loop interventions are env-gated default-OFF in current code,
  not "always active."
- `proposed-fixes-README.md` — the apply-recipe for D1–D3; the actual
  diffs landed via Edit/Write, not the recipe.
- `proposed-sdd-and-claude-additions.md` — D2 + D3 patch drafts; shipped
  via the drift commit.

## Reproducibility scripts

Run from this directory; both write into the parent of the script
location, but the analyses themselves are read-only against
`acceptance/v*_taxonomy/*.json`.

- `e1-analyze.py` — regenerates the E1 cliff-incidence counts.
- `af-analyze.py` — regenerates the A (empty_patch breakdown) and F
  (intervention effectiveness) tables.

## Generated CSVs

Machine-readable mirrors of the analysis tables; useful for plotting or
re-aggregation. Paths inside are repo-relative
(e.g. `acceptance/v17_taxonomy/...`), not absolute.

- `e1-context-cliff-counts.csv`
- `a-empty-patch-breakdown.csv`
- `f-intervention-effectiveness.csv`

## Unrelated prior research (not part of the agentic-patterns audit)

- `forge-overlap-analysis.md`
- `hermes-harvest-backlog.md`
