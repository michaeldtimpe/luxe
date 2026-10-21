# LUXE_CTX_CAL_TOOLS — champion no-harm (2026-10-20, m5)

**Verdict: no harm. Promoted to default-ON.** For the champion the lever corrects the
measurement and leaves outcomes alone: 9 of 10 fixtures sent byte-identical requests in
all six runs.

## What changed

The server counts the request's tool schemas in `usage.prompt_tokens`;
`estimate_messages_tokens` never saw them. The server-truth ratio therefore read ~7× at
step 0 (2,225 / 320 on Glimmer neon-reset) and multiplied that onto the next file read.
With the flag, the schemas (chars//4, once per run) join the estimate the ratio divides and
come off `effective_ctx` as a fixed charge. Commit 819e573 (opt-in), flipped in the commit
that adds this report. Origin: `acceptance/harness_ab_2026_10/REPORT.md` § Glimmer
neon-reset.

## Design

- Champion `Qwen3.6-35B-A3B-6bit`, `variants_champ.yaml`, all 10 maintain_suite fixtures,
  `--work-dir ~/.luxe/bench-workspace`, per-fixture timeout 1800 s.
- Arms: **ctl** = shipped defaults (flag unset, then opt-in), **cal** = `LUXE_CTX_CAL_TOOLS=1`.
- 3 reps, interleaved ctl/cal per rep on the same day (19:23–20:08 CDT), driver
  `m5:~/luxe-bench-tmp/calt/noharm.sh`.
- Then 3 Glimmer neon-reset diagnostic runs with the flag (`diagrun.sh`, 30 steps).

## Results

| | ctl | cal |
|---|---|---|
| expected_outcome | **30/30** | **30/30** |
| score | 120/150 | 120/150 |
| hand-verified REAL | 25/30 | 26/30 |
| wall (sum) | 1,303 s | 1,263 s |
| prompt tokens (sum) | 3.78 M | 3.57 M |
| compaction events | 193 | 155 |

Every difference in the last four rows comes from `nothing-ever-happens-document-config`
(below) and one cold-start outlier (ctl rep1 neon-reset, 85 s vs 39 s for its other
five runs). Excluding the document-config fixture, wall and prompt tokens match run for run.

### Per fixture

| fixture | diffs (6 runs) | compaction steps | peak pressure ctl → cal |
|---|---|---|---|
| isomer-document-quickstart | identical | none | 0.57 → 0.22 |
| isomer-implement-healthcheck | identical | none | 0.23 → 0.09 |
| lpe-rope-calc-document-typing | identical | none | 0.57 → 0.38 |
| lpe-rope-calc-implement-strict-flag | identical | 8, 18 both arms | 0.69 → 0.67 |
| neon-rain-document-modules | identical | none | 0.41 → 0.36 |
| neon-rain-implement-reset-shortcut | identical | 12–15 both arms | 1.83 → 1.65 |
| nothing-ever-happens-document-config | 6 distinct | varies in both arms | 0.53–0.57 both |
| nothing-ever-happens-manage-deps-audit | identical | none | 0.50 → 0.34 |
| the-game-document-architecture | identical | none | 0.42 → 0.25 |
| the-game-implement-shuffle-shortcut | identical | none | 0.21 → 0.15 |

"Identical" means the same `git patch-id` for all six runs and the same prompt-token total
to the token. Each run's commit came from the clone's reflog, because bench branch names
are reused. The reported peak drops on every fixture. Where compaction fires, it fires at
the same step with the same phase. On these fixtures the server-truth ratio had already
converged by the time pressure mattered, so the early inflation never crossed a threshold.

document-config is not deterministic within the control arm alone: ctl1 stopped at 14
steps, while ctl2 and ctl3 ran 30 steps. The cal arm ran 30, 17 and 15 steps. Its spread
is noise this fixture already has, not a lever effect.

## Glimmer neon-reset with the flag alone

1 REAL / 2 FAIL (`glim-neon-calt{1,2,3}`).
- **calt1:** binds Shift+R in `HtmlInputHandler` and emits the game's existing `__restart`
  meta-action, which goes through the existing confirm and `_startNewGame()`. `npm test`
  passes. It stopped at the step limit with the diff complete and was labelled incomplete.
- **calt2 and calt3:** no write in 30 steps.

The luxe reference is 0/4. The trace showed three conditions, and this flag removes part of
one (early over-compaction). 1/3 matches the single-lever band (0–1/3) the trace measured.
It does not convert the fixture.

## Collateral at the flip

- The golden trajectories were regenerated. Their fake server reports `len(body)//3`, which
  includes the tool schemas, against 2.5K–8K toy windows. The charge moves every scenario's
  pressure, and moves compaction timing in `default_flags` and `clamp_and_damp`.
- `TestCalibrationDamping` pins `LUXE_CTX_CAL_TOOLS=0`. It replays the 2026-08-24 chat
  ratio, which was measured without the schemas.
- Full suite: 3972 passed. The one failure is the stale-oMLX-pid chat smoke test, which
  depends on the environment and fails on main as well.

Hand-verify notes: `HAND-VERIFY.md`.
