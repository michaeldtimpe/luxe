# 2026-09 full-project review — benchmark verdicts

All runs used the champion `Qwen3.6-35B-A3B-6bit` on shipped defaults, 3 reps of each of the 10 maintain_suite
fixtures. "Score 4" is the offline maximum, because `gh pr create` has no GitHub remote for fixture repos. A printed
pass was not taken as a result: every arm's diffs were read by hand against `base_sha`.

The raw data lives in local, gitignored sibling directories: `review_2026_09_ab/` (m1, batch 1 plus replication),
`review_2026_09_ab2/` (m1, batch 2) and `review_2026_09_m5/` (m5, batch 3). Benchmarks moved to m5 on
2026-09-27 at the user's request. m5 arms are only ever compared with an m5 baseline.

## Verdicts

| Change | Where | Printed | Hand-verified (REAL/THIN/VAC) | Verdict |
|---|---|---|---|---|
| #13 tool path canonicalization | m1 batch 1 | 30/30 | diffs REAL | **landed** |
| fb50510 dedup + compaction markers | m1 batch 1 + replication | doc-config 4/6 vs base 6/6 | both misses zero-write | **compaction half HELD** (branch `fix/agent-dedup-after-write`) |
| #25 loop refactor (LoopState, 3 dead mechanisms removed) | m1 batch 2 | 30/30 | 25/4/1 vs main 26/3/1 | **landed** — 16/30 runs token-identical, the rest within run-to-run noise |
| #20 MCP opt-in (14 leaked tools off the bench surface) | m1 batch 2 | 30/30 | 21/9/0 vs main 26/3/1 | **landed on user decision** — −27% tokens, −20% wall; THINs are 2 deterministic fixtures, doc-config improved |
| #26 dedup after write (split from fb50510) | m5 batch 3 | 30/30 | 26/4/0 vs base 24/6/0 | **landed** — no harm; fired once in 30 runs and did not change that doc |
| #17 bench harness + graders | m5 batch 3 | 30/30 | 25/5/0 vs base 24/6/0 | **landed** — byte-identical to base on 8/10 fixtures |
| #29 SWE-bench harness amd64 pre-pull (colima on m5) | m5 live grading | 6/6 patches applied, 0 errors | reports for every instance | **landed** — m5 can run the full harness |
| #28 ship agent-made commits; unshipped diff = ERROR | m5, deps-audit ×3 | 3/3 push ok | 85-line audit reaches the cache | **landed**. Not model-visible |
| #27 BFCL reprompt order | m5 BFCL agent, 1,240 problems | 1,134 → **1,137** | 1,228 byte-identical; 12 differ (4 won, 1 lost, 7 fail both) | **landed** — parallel_multiple 85.5% → 87.0% |
| #27 habituation exit | m5 SWE-bench preds-only, 4 hab instances | empty 1/4 both | 25775 empty ×6 in both arms | **landed, inert here** — the same-step case did not recur on current code (all 6 exits since=5). Docker harness on m5 (#29): both arms resolve 1/4 (matplotlib-14623), identical |

## Findings that outlive this review

- **Bench results from 2026-08-25 to 2026-09-27 ran with 14 extra MCP tools** (codex_one and cpa), because an empty
  `enabled_for` meant "every task" on the maintain path. #20 makes it opt-in. Numbers from that window are not
  comparable with numbers after it.
- **m1 pushes had failed** because the fixture cache was read-only. It is writable since 2026-09-26. #17 adds an
  origin preflight, so this can't recur silently.
- **The doc-config fixture ships wrong safety numbers on every arm.** Four of nine m5 docs state wrong PM_RISK
  defaults, for example a $1,000 daily-drawdown breaker that is really disabled (0). DRY_RUN is correct in every doc.
  The grader can't see this. It is model capability, not a luxe regression.
- **deps-audit gets credit for a deliverable nobody can see.** The model commits through `bash` on a detached HEAD,
  so luxe logs `failed_no_mutations_produced` and pushes nothing, yet the fixture scores 4/5. The only copy of the
  output is in the workspace clone's reflog. **Fixed by #28**: luxe now ships agent-made commits, and an unshipped diff is an ERROR.
- **m5 is nearly deterministic** (8/10 fixtures token-identical across reps and arms); m1 is not. Prefer m5 for A/Bs.
- **The fixture repos have no `.gitignore`,** so arms sometimes commit `__pycache__/*.pyc`. This predates the review
  and appears on every arm.

## Not shipped, on evidence

- **Tool-call id pairing and stripping `_luxe_*` keys on the wire.** The defect is real, but 0 occurrences turned
  up across 6 OpenRouter sessions, and the Qwen template does not render ids. It is below the taxonomy bar
  (≥5 occurrences across ≥2 sessions).
- **Tightening text recovery of tool calls:** 0 `textfallback_drop` occurrences (2026-08-04 taxonomy).
- **Two items that needed cold-storage benchmarks** were run on m5 on 2026-09-27/28 (#27 above,
  `review_2026_09_m5b/`). The m5 BFCL agent-mode baseline is 91.45% (5 single-turn categories) and 91.69% with
  #27. It is not comparable with C.8's 88.39% (m1, older code). The original notes:
  - BFCL `min_tool_calls` reprompt ordering. The reprompt goes before the assistant replay, so C.8's 88.39% was
    measured with it.
  - The habituation exit on the same step as its nudge. 4 of 46 exits happened this way; SWE-bench configs only.
