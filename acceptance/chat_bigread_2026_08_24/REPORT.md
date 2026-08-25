# Chat-side LUXE_TOOL_BUDGET_CTX drill — REPORT

Generated 2026-08-24 22:23 by `scripts/bigread_drill.py`. Per PLAN.md item 2.1: chat-side evidence for `tools/tools.sdd` § "`LUXE_TOOL_BUDGET_CTX` wiring", which currently records chat as having none.

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

Scratch repo: `/var/folders/25/smphr2f909922wfm3d87_bph0000gn/T/luxe-bigread-drill-xenb9ymc` — big-notes.md 250,040 B, module.py 70,028 B, README.md 201 B, questions.md 71 B

**PENDING — not run.** This script does not dispatch live turns on its own; a running backend is required. Run:

```bash
python3 scripts/bigread_drill.py --backend local
python3 scripts/bigread_drill.py --backend openrouter
```

(each performs the full unset/`=1` x 32768/131072 matrix against the named backend and rewrites this file with real rows).

| arm | window | outcome | tool_calls | peak pressure | refused reads | resume calls used | wall_s | retries | compaction | recovered |
|---|---:|---|---:|---:|---:|---:|---:|---:|---|---|
| off | 32768 | PENDING | — | — | — | — | — | — | — | — |
| off | 131072 | PENDING | — | — | — | — | — | — | — | — |
| on | 32768 | PENDING | — | — | — | — | — | — | — | — |
| on | 131072 | PENDING | — | — | — | — | — | — | — | — |

## Verdict

Not yet measurable — the matrix above is pending a live run (see command above). The real-incident section already proves arm=OFF loses the turn on both backends at both windows tested in the wild; what is still unmeasured is arm=ON's actual behavior — specifically whether it trades one fatal turn for a clean completion, or for several timid ones (PLAN.md's explicit concern).

