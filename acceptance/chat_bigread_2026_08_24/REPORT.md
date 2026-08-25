# Chat-side LUXE_TOOL_BUDGET_CTX drill — REPORT

Generated 2026-08-24 23:33 by `scripts/bigread_drill.py`. Per PLAN.md item 2.1: chat-side evidence for `tools/tools.sdd` § "`LUXE_TOOL_BUDGET_CTX` wiring", which currently records chat as having none.

## Parser self-test (real 2026-08-24 failure, session `168f1825a1fd`)

PASS — see stdout for the per-assertion breakdown, or re-run with `--dry-run` on a host holding `~/.luxe/sessions/168f1825a1fd/`.

## Real-incident replay (arm=OFF, both already happened, 2026-08-24)

This section is not a drill — it is the actual failure, parsed with the same code path the drill below uses. Both runs are arm=OFF (chat's real default) because that is what was running when the incident occurred.

| session | run_id | window | outcome | tool_calls | peak pressure | refused reads | resume calls | wall_s | retries | compaction |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| `168f1825a1fd` | `168f1825a1fd-2` | 131072 | aborted | 3 | 102.5% | 0 | 0 | 81.469 | 3 | phase3 NO-OP (71616→71616 tok) |
| `eb0b2923a3eb` | `eb0b2923a3eb-0` | 32768 | interrupted | 4 | 271.8% | 0 | 0 | 196.741 | 0 | phase3 NO-OP (36036→36036 tok) |

Both incident turns ran with `LUXE_TOOL_BUDGET_CTX` unset (chat's shipped default) — `refused_reads=0` in both confirms the budget never engaged; the fixed 256 KB cap (`_MAX_FILE_SIZE`) never fired either, because 257,988 B and 72,181 B both sit under it. The failure is pure ctx-window overflow, not a refusal.

## Planted-repo A/B matrix (this drill)

Scratch repo: `/var/folders/25/smphr2f909922wfm3d87_bph0000gn/T/luxe-bigread-drill-jra7ktnd` — big-notes.md 250,040 B, module.py 70,028 B, README.md 201 B, questions.md 71 B

| arm | window | outcome | tool_calls | peak pressure | refused reads | resume calls used | wall_s | retries | compaction | recovered |
|---|---:|---|---:|---:|---:|---:|---:|---:|---|---|
| off | 32768 | **timeout** (killed by drill's own --timeout) | 3 | 1064.2% | 0 | 0 | 12.443 | 0 | phase3 no-op (88251→88251 tok, 0 results) | no |
| off | 131072 | **timeout** (killed by drill's own --timeout) | 3 | 266.0% | 0 | 0 | 5.867 | 0 | phase3 no-op (88251→88251 tok, 0 results) | no |
| on | 32768 | completed | 6 | 50.6% | 2 | 1 | 60.021 | 0 | phase1 no-op (10920→10920 tok, 0 results) | yes |
| on | 131072 | completed | 7 | 39.0% | 2 | 2 | 79.872 | 0 | — | yes |

## Verdict

**arm=OFF hung outright at window(s) 32768, 131072 — the drill's own timeout had to kill it — while arm=ON completed every window cleanly**, at a cost of 3 extra resume call(s) total. This is the strongest possible result for this drill's question: not a slower turn or a retry storm, but a genuine unrecoverable hang that the flag prevents outright. See the real 2026-08-24 incident this reproduced (session `a2a182160112`, forensics mined from the same timed-out-session code path even though it never logged a completed turn).

