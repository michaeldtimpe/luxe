# LUXE_TOOL_BUDGET_CTX A/B — 2026-08-12

**Run:** m5, `Qwen3.6-35B-A3B-6bit`, 3 reps × 10 fixtures × 2 arms = 60 runs,
settled tree (all 2026-08-12 tool fixes + wiring in place), sequential arms.
Variants + criteria pinned pre-run: `variants_toolbudget_3rep.yaml`.

**Question:** should the per-tool-result read cap scale with the context
window (`budget_for_ctx(32768)` = 13,653 bytes vs the fixed 256 KB constant)?

## Preflight (the C10 lesson, honored)

The knob was INERT BY CONSTRUCTION on the benchmark path until
`maintain.apply_ctx_read_budget` (2026-08-12) — `set_read_budget`'s only
caller was chat's repl. Wiring verified three ways before the arm ran:
unit tests (37), a no-model liveness call (13,653 for num_ctx=32768), and a
single-fixture preflight showing the `read_budget_applied` event + the
console line + zero over-budget returns (3/3 PASS, walls 81/314/135s —
the 314s outlier did not generalize to the arm).

## Verdict: PROMOTED to default-ON on the maintain/bench path

| arm | passed | score | avg wall | avg tokens |
|---|---|---|---|---|
| baseline (budget off) | 30/30 | 120/150 | 46.8s | 121,315 |
| treatment (budget on) | 30/30 | 120/150 | 48.8s (+4.3%) | 116,553 (−3.9%) |

Criteria: (1) treatment holds 30/30 · 120/150 ✓, with real firings ✓ and
cost far under the 1.5× bound ✓ — tokens actually FELL; (2) zero fixture
regressions ✓; (3) opportunity was exercised ✓ — not vacuous.

**Opportunity + enforcement, measured:** baseline runs returned 21
over-budget reads (would-be refusals); treatment returned zero true
over-budget results (six events at exactly 13,716 bytes = budget + 63 bytes
of framing, deterministic across reps — measurement overhead, not a leak).
`read_budget_applied` present in all 30 treatment runs, absent from all 30
baseline runs. The windowing echo is visible: 277 → 379 read_file calls,
concentrated in the big-file fixtures (document-config 90→145,
neon-rain-document-modules 42→87).

## The interesting redistribution

Per-fixture wall (3 reps, baseline → treatment):

- `lpe-rope-calc-implement-strict-flag` **163s → 85-92s** — the suite's
  expensive fixture got ~2× faster. Smaller tool results mean the
  truncated-turn retry replays a smaller context; the budget is a
  prompt-diet exactly where prompts were most bloated.
- `nothing-ever-happens-document-config` 58-160s → 99-145s and
  `neon-rain-implement-reset-shortcut` 29s → 54-107s — read-heavy fixtures
  pay the windowed re-read tax; worst rep-level outlier 3.7× (absolute
  ~107s, well under any timeout).
- Everything else ±small; `the-game-*` byte-stable.

## Scope of the flip

- **Maintain/bench path: default ON** (`LUXE_TOOL_BUDGET_CTX=0`, exact
  string, disables — same opt-out grammar as `LUXE_TRUNCATED_TURN_RETRY`).
- **Chat: stays opt-in** (`=1`), because zero chat evidence exists and chat
  UX around large files is a different question (`/ctx` tiers already scale
  the budget when enabled there).
- **BFCL: still unwired** — its adapter calls `run_agent` in-process and
  never passes through `maintain_pipeline`; the flag is inert there and
  `tools.sdd` says so. Wire + preflight before ever A/B-ing it on BFCL.

The standing suite baseline going forward is the treatment profile:
**30/30 · 120/150 · ~49s · ~117k tokens** on shipped defaults.
