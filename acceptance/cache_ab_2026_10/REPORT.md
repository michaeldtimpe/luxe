# Prefix-cache A/B — reasoning round-trip (R) × cache-friendly compaction (C)

**Date:** 2026-10-15 · **Host:** m5 (M5 Max, oMLX 0.7.0, started by hand for
the run and stopped after) · **Model:** `Qwen3.6-35B-A3B-6bit` (champion) ·
**Code:** branch `cache-perf-telemetry` @ `0505bd0` (PR #30), worktree
`~/luxe-bench-tmp/cache-ab` · **Suite:** maintain_suite, 10 fixtures × 3 reps
per arm, `--per-fixture-timeout 1800 --work-dir ~/.luxe/bench-workspace`.

| arm | env | meaning |
|---|---|---|
| **base** | flags unset | **the current rev** — request-identical to `main` (the branch only adds telemetry) |
| **R** | `LUXE_REASONING_ROUNDTRIP=1` | assistant messages carry the model's own `reasoning_content` back |
| **C** | `LUXE_COMPACT_CACHE_FRIENDLY=1` | phase-1 compaction truncates each tool result once, in batches of ≥4 |
| **RC** | both | |

Arms ran sequentially (base 18:34 → RC end 19:58), same server, same
workspace. Run ids: `run_id_manifest.tsv`. Numbers: `analysis.md`
(`analyze.py`), `steps.txt` (`steps.py`), diffs: `verify.py` / `showdiff.py`.

## Headline

| arm | pass | score | avg wall | Δ wall | avg wall excl. lpe-strict | completion tok/run | cached % | prefill s | decode s | compaction fires | re-prefilled tok |
|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 30/30 | 120 | 47.4s | — | 35.5s | 4,688 | 86.5% | 181.9 | 1,217.7 | 66 | 452k |
| R | **29/30** | 117 | 34.3s | **−28%** | 29.1s | 3,197 | 85.7% | 177.5 | 832.3 | 33 | 308k |
| C | 30/30 | 120 | 43.5s | −8% | 32.7s | 4,354 | 89.5% | 153.2 | 1,133.7 | 27 | 352k |
| RC | 30/30 | 120 | 35.7s | **−25%** | 29.7s | 3,439 | 88.5% | 149.9 | 898.8 | 27 | 317k |

(Score max is 5/run; a pass earns 4 because the bench never opens a PR.)

Cache behaviour on m5, per continuation step (step N+1 vs step N's prompt):
**zero cold steps in any arm.** Partial hits are almost entirely compaction
rewrites — base 41 of 57 partial steps / 439k of 452k lost tokens; R 22/22;
C 23/39; RC 21/24. The non-compaction remainder is small tail misses (≈13k
tokens in base). The oMLX `<4096`-token store race seen on m1 (below) did
not show up as cold steps here — every m5 maintain prompt shares a ≥2,048
cached system block.

## Per fixture (passes/reps, avg wall)

```
fixture                                   base          R            C            RC
isomer-document-quickstart              3/3 20s      3/3 20s      3/3 20s      3/3 20s
isomer-implement-healthcheck            3/3 28s      3/3 18s      3/3 28s      3/3 18s
lpe-rope-calc-document-typing           3/3 30s      3/3 28s      3/3 29s      3/3 29s
lpe-rope-calc-implement-strict-flag     3/3 155s     3/3 82s      3/3 140s     3/3 90s
neon-rain-document-modules              3/3 24s      3/3 24s      3/3 24s      3/3 24s
neon-rain-implement-reset-shortcut      3/3 54s      3/3 56s      3/3 54s      3/3 51s
nothing-ever-happens-document-config    3/3 93s      2/3 48s      3/3 67s      3/3 58s
nothing-ever-happens-manage-deps-audit  3/3 40s      3/3 37s      3/3 41s      3/3 36s
the-game-document-architecture          3/3 19s      3/3 10s      3/3 19s      3/3 11s
the-game-implement-shuffle-shortcut     3/3 11s      3/3 21s      3/3 11s      3/3 21s
```

## Hand-verification

Every run's branch was diffed against `base_sha_used` in the fixture repo
(`verify.py`; branch from `~/.luxe/runs/<id>/pr_state.json`, never
`origin/<branch>`), and every distinct diff was read.

- **No pass deletes a file or is net-negative**; every pass's diff is
  confined to the files the goal names.
- **119 passes: 113 REAL, 6 THIN.** The six THIN are R and RC
  `the-game-document-architecture` (all reps): the README section names
  `src/App.jsx`, `shuffle()` and its `useEffect`, so it meets the letter of
  the goal, but it summarises "receives randomized selections" where base
  explains the server's neighbor-selection algorithm (`selectWithNeighbors`,
  the 6-wide shelf window). Base and C produce the fuller section.
- Differences that are not quality regressions: R/RC
  `isomer-implement-healthcheck` adds the route without base's
  `test_health.py` (that file is a print script, not a test); R/RC
  `isomer-document-quickstart` restructures the Quick Start into numbered
  steps, keeping all the content; R/RC `lpe-…-strict-flag` additionally
  treats partially read metadata as an error and scans GGUFs in the HF
  cache — arguably more complete than base, which also left a dead duplicate
  `return 0`.
- **The one failure — R rep2 `document-config`:** 30 steps of reading, no
  write (`failed_no_mutations_produced`); three phase-1 compactions mid-read,
  each followed by re-reads of the same files. This is the fixture's known
  write-avoidant mode (memory: replicate borderline doc/manage fixtures),
  not a crash. RC did 3/3 on the same fixture.
- **Branch-name caveat:** suffixes are first-free counters, so a run that
  never pushes "owns" a name the next run takes. Only the R rep2 failure is
  affected (its recorded branch holds rep3's commit). All other 119 branch
  names are unique and their commit times fall inside their run window.

## Reading the result

**Determinism.** temp=0 holds: base produced identical diffs across reps on
9/10 fixtures; R/RC differ on 2/10. So "3 reps" is mostly three copies of
one trajectory per fixture — n is effectively 10 trajectories per arm, not
30. Treat score parity as "no regression seen", not as a measured rate.

**C is a pure mechanism change.** On every fixture where compaction never
fires, C's trajectory, diff and wall are identical to base. It changes only
the runs that compact: fires 66 → 27, re-prefilled tokens −22%, prefill
seconds −16%, and `document-config` 93s → 67s / `lpe-strict` 155s → 140s.
No score change; diffs on non-compacting fixtures byte-identical.

**R changes the model's behaviour, not just the cache.** Cached % is
essentially flat (85.7 vs 86.5%) — the reasoning text adds prompt, and it is
cached like everything else. The wall win comes from the model doing less:
completion tokens −32%, steps 13.6 → 12.2/run, and on `lpe-strict` it
writes the patch in about half the decode (15,980 → 8,348 completion
tokens). The cost is visible too: one write-avoidant failure, a thinner
architecture doc, and slower `the-game-implement-shuffle-shortcut` (11s →
21s). The speedup is not one fixture's artefact — excluding `lpe-strict`,
R is still −18% (35.5s → 29.1s).

**RC keeps R's speed (−25%) with C's prefill savings and 30/30.** Whether C
actually *rescues* R's `document-config` failure can't be claimed from one
fail in three — but on that fixture RC compacted less than R (1.3 vs 2.0
fires/run) and finished in 58s.

## Recommendation

1. **C → candidate default-ON.** It is a fix for a real bug (phase-1
   re-truncates already-truncated results, writing a false "[Truncated — 33
   chars removed]" and rewriting the prefix every step past 50% pressure),
   it is inert where compaction does not fire, and it saved prefill with no
   score or diff change. Flip `LUXE_COMPACT_CACHE_FRIENDLY` default with the
   old path kept as the ablation (`=0`), per the default-ON pattern.
2. **R/RC → not yet.** −25–28% wall is a large win, but it moves the trajectory distribution, and the evidence is n≈10
   trajectories with one fail + one THIN pattern. Next step if wanted: a
   wider run (SWE-bench n=75 subset or 5+ reps with seeds varied) of **RC vs
   C**, plus an interactive check in `luxe chat`, before any default change.
   Note Qwen3.6's template already preserves thinking within a turn
   (`preserve_thinking=True` auto-set by oMLX), so R is closer to the
   distribution the model was trained on — a plausible reason it decides
   faster — but that is a hypothesis, not a measurement.

## Related findings (same investigation, not measured by this A/B)

- **Chat turns start cold.** The chat/code prefix (system + tools ≈ 2.6k /
  3.0k tokens) is under oMLX's 4,096-token hybrid-model snapshot block, and
  each turn is a fresh `run_single`, so nothing carries across turns. Fixing
  that is a chat.sdd-level design change (history currently reaches the
  model only via `extra_context`) — open, awaiting a decision.
- **oMLX 0.7.0 store race** (`scheduler.py` `_find_relevant_inflight_store`):
  the wait-for-in-flight-store only applies when restorable overlap ≥ one
  4,096 block, so a step whose previous prompt was <4,096 tokens races the
  async tail-snapshot store. m1 history: continuation-step misses 35%
  (34/98) when prev <4,096 vs 0.8% (15/1,931) when ≥4,096; reproduced live
  2026-10-15 18:29. Server-side; candidate upstream issue. Mostly a chat
  problem — maintain prompts cross 4,096 within a few steps.
