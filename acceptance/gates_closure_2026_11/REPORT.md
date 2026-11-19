# Closure check for the destructive_diff and tool_output_echo gates (2026-11-19)

## What was owed and why

Commits d93f6ba and 13d2aae (both 2026-08-19, same message) closed the destructive_diff replace-in-place gap and added the tool_output_echo gate. RESUME.md:392-400 records "OWED: a bench-as-truth maintain_suite run" because the new gates change scores by construction, and RESUME.md:430 lists the owed run as open item (a). The gap that motivated the gates is in `acceptance/gemma4_r1_2026_08_19/` (40 runs, `gates_triggered` empty on diffs that overwrote documents) and `acceptance/echo_repro_2026_08_19/` (6 runs, also empty). Both predate the gates and are excluded from the post-ship counts below.

Gate triggers, read from `benchmarks/maintain_suite/gates.py` (`apply_strict_gates`):

- destructive_diff prong 1: deletion/addition ratio (the old rule).
- Prong 2: replace-in-place token loss (bag 0.58 legit max vs 0.71+ overwrite, and distinct-words 0.57 legit max vs 0.88+; both must agree; generated files skipped).
- Prong 3: a doc section deleted with its body and not retitled among the added headings (fence-aware since 2026-10-21).
- tool_output_echo: added lines matching `^\d+\t`, strictly consecutive, run of at least 10.

## Evidence scanned

Every `result.json` and `runs.json` row under `acceptance/` carrying a `gates_triggered` (or `gates`) field, restricted to directories dated after the ship. Counts come from `scan.py` output (rows per directory); each run is counted once and both gates run on every write-task run.

| dir | runs |
|---|---|
| harness_ab_2026_10 | 565 |
| m5max_moe_r2_2026_10 | 504 |
| validation_context_ab_2026_11 | 180 |
| bash_noop_ab_2026_11 | 150 |
| splash_engine_ab_2026_09 | 130 |
| tuning_2026_10 | 103 |
| idle_narrow_ab_2026_11 | 96 |
| ctx_cal_tools_2026_10 | 60 |
| thinkingcap_27b_2026_10 | 60 |
| harvest_2026_10 | 1 |

Total about 1849 runs. Four post-ship dirs hold no local artifacts (archived): cache_ab_2026_10, secret_scrub_ab_2026_10, hardening_2026_10, grader_fixes_2026_10 (its REPORT.md is still cited below). Excluded as pre-gate: gemma4_r1_2026_08_19, echo_repro_2026_08_19, and four May `deluxe_m5max_dense*` llama-70b healthcheck hits (ratio prong, pre-ship).

## Firings

Source for each row: the dir's result.json or runs.json (scan.py), and the dir's HAND-VERIFY or REPORT file.

| dir | model | fixture | rep/arm | gate | printed | hand-verify |
|---|---|---|---|---|---|---|
| validation_context_ab_2026_11 | gemma-4-26b MoE | neon-rain-document-modules | rep1-3, ctl | destructive_diff (prong 3) and tool_output_echo | 1/5 FAIL x3 | DAMAGING (patch dce7860c0b: read_file line prefixes in all 132 lines, Subdirectories section absent) |
| harness_ab_2026_10 | q38 via codex | isomer-document-quickstart | rep1 | destructive_diff (prong 3) | 1/5 FAIL | REAL, so a grader false positive |
| harness_ab_2026_10 | q38 via codex-cat | isomer-document-quickstart | rep2 | destructive_diff (prong 3) | 1/5 FAIL | REAL, same bug; fixed 2026-10-21 (`acceptance/grader_fixes_2026_10/REPORT.md`, bug 1) |
| harness_ab_2026_10 | champion via codex-cat | lpe-rope-calc-document-typing | rep2 | destructive_diff (prong 1, "deleted 46, added 2, 23x") | 1/5 FAIL | REAL per HAND-VERIFY; the diff did strip 46 blank/context lines ("sed collapsed every double blank line") |

Tallies (scan.py output plus the rows above):

- tool_output_echo: 3 firings, 3 true positives (all the one gemma neon-rain patch), 0 false positives in about 1849 runs.
- destructive_diff: 3 true positives (gemma neon-rain, prong 3), 2 false positives (isomer quickstart, prong 3, root cause fixed 2026-10-21; 20 of 21 mis-graded runs then print PASS), 1 borderline (champion codex-cat prong 1, outside the 2026-08-19 prongs).
- Prong 2 (the headline of d93f6ba): zero firings in any post-ship run, so no live precision or recall evidence.
- Champion flips: none. No luxe-loop champion (Qwen3.6-35B-A3B) result changed in m5max_moe_r2, tuning, ctx_cal, thinkingcap, bash_noop, idle_narrow or validation_context (`validation_context_ab_2026_11` HAND-VERIFY-champ: neon-rain-document-modules REAL in all arms). The only champion firing is the non-luxe codex-cat harness case above.

## Replay of reconstructed failures and negative controls

Source: `replay.py` (calls the gate functions directly on real base files from `~/.luxe/bench-workspace` and on archived diff.patch files), plus 16 gate-related tests in `tests/test_acceptance_grader.py` that pass under `uv run pytest -k`.

The original +132/-132 and +9/-9 patches are not stored locally: `acceptance/gemma4_r1_2026_08_19` keeps only result.json, state.json and stdout (counts and base shas ef953cc and 514d22d). Reconstructions on the real base files substitute.

| case | destructive_diff | tool_output_echo |
|---|---|---|
| neon-rain ARCHITECTURE.md rewritten as `<n>\t<line>` (+134/-132) | fired (prong 3, section deleted) | fired (132 numbered lines) |
| neon-rain overwrite, headings kept, words replaced (+132/-132) | fired (prong 2: 309 of 344 distinct words, 90%; 95% of occurrences lost) | no |
| the-game README, Features section swapped for How It Works (+100/-106, larger than the original +9/-9) | fired (prong 3) | no |

Prong 2 now has a replay on a real base file where only prong 2 can see the damage (headings preserved, so prong 3 is blind).

Negative controls: 90 real REAL-class document-task diffs, namely `acceptance/harness_ab_2026_10` champion arms (Qwen3.6-35B-A3B via claude, codex, codex-cat, opencode, qwen; 3 reps x 5 document fixtures = 75) and `acceptance/thinkingcap_27b_2026_10` qwen3.6-27b ref (15). Real parsed hunks and base/HEAD headings were passed to the gates. Fired: 1 of 90, the known codex-cat rep2 lpe-rope-calc-document-typing ratio hit (+2/-46, prong 1). Prong 2, prong 3 and echo: 0 of 90, including the isomer quickstart diffs that tripped pre-fix prong 3.

## Verdict

The owed run is effectively closed for tool_output_echo, destructive_diff prong 3 and prong 2 at the unit and replay level: all three fire on reconstructed originals, 0 false positives on the 89 of 90 control diffs outside the known prong-1 case, and 0 luxe-loop champion flips. A model-bearing maintain_suite run is no longer needed to close it. Live-run evidence for prong 2 remains absent (no post-ship firing); it rests on the replay and the 400-commit calibration recorded in gates.py.

## Residuals

- The original 2026-08-17 diff bytes (+132/-132 neon-rain, +9/-9 the-game) exist only in the 2026-11-05 m5 archive on the backup NAS (105 GB, zstd stream; extracting one member needs a full scan, not run). Low value: the reconstructions exercise the same code paths.
- The codex-cat blank-line case (prong 1, 46 blank lines stripped, REAL-classified) is undecided as a gate false positive; it is outside the 2026-08-19 scope.
- RESUME.md:392-400 and :430 still read "owed"; they should cite this report, `acceptance/validation_context_ab_2026_11`, `acceptance/harness_ab_2026_10` and `acceptance/grader_fixes_2026_10`.
- Optional cheap live check for prong 2: a weak arm (gemma-4-26b MoE or Qwen3.8-27B) on the-game-document-architecture and neon-rain-document-modules, 3 reps, hand-verified. Not required for closure.

## Reproduction

From the repo root:

```
python3 acceptance/gates_closure_2026_11/scan.py          # per-dir run counts and gate firings
uv run python acceptance/gates_closure_2026_11/replay.py   # reconstructions + 90 negative controls
uv run pytest tests/test_acceptance_grader.py -k "destructive or echo"
```

replay.py needs the real base clones in `~/.luxe/bench-workspace` and the diff.patch files under `acceptance/harness_ab_2026_10` and `acceptance/thinkingcap_27b_2026_10`. scan.py lists every dir; the table above keeps only dirs dated after 2026-08-19.
