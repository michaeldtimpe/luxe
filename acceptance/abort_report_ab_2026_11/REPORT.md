# Abort report (`LUXE_ABORT_REPORT`, default ON): A/B result

Branch `feat/abort-report`. Plan and gates: `PLAN.md`. Analysis: `analyze.py`
(output regenerated into `runs.json`), patch verdicts: `HAND-VERIFY.json`.

## Verdict

| question | answer | why |
|---|---|---|
| (a) Land `LUXE_ABORT_REPORT` default ON? | **Yes.** | Runs ending with no report go from 9/30 (ctl, idle_capped_ab_2026_11) to 0/30 in both arms. All 13 fallback reports were checked line by line against diff.patch and the saved conversation: 0 false claims. Every empty-model-text run got one; no run with model text got one. |
| (b) Promote `LUXE_IDLE_NARROW=wide` to default? | **No.** | Fails the hand-verified REAL gate by one run (26 vs ctl 27: a step-limited neon run whose shortcut only works while the board has focus). It also spends +34% prompt tokens overall and commits a litter file in 3/3 strict-flag runs. Its main benefit (fewer empty reports, 9 to 4) is now covered by the fallback in ctl. |

## Setup

| | |
|---|---|
| Code | bench worktree `~/Downloads/luxe-bench` detached at `ac2ff9f` (apparatus) on top of `0779896` (abort report). The only `src/` + `benchmarks/` change vs `ced100e` (code under test in idle_capped_ab_2026_11) is the abort report and its bench field |
| Server | oMLX 0.8.0 (same as idle_capped_ab_2026_11) |
| Model | `Qwen3.6-35B-A3B-6bit` (champion) |
| Fixtures | all 10 maintain_suite fixtures, 3 reps, arms interleaved ctl then wide within each rep |
| When | 2026-11-19 12:17:52 to 12:51:22 CST, all 6 arm passes rc=0 (`arms.log`) |
| Warm-up | `isomer-implement-healthcheck` into `warmup/`, not counted; it passed (score 4); the bench exits rc=1 because the single-cell "v1 release" summary is NO |
| Liveness | `idle_narrow_mode` in 30/30 wide, 0/30 ctl; `bench_venv_path` on in 60/60 (`analyze.py` liveness column) |

Abort conditions (PLAN.md): none tripped. Both arms printed 30/30; no fallback on a
run with model text; no liveness miss.

## Per-arm results

Source: `analyze.py` output, `runs.json`.

| measure | ctl | wide |
|---|---|---|
| printed pass | 30/30 | 30/30 |
| model text empty | 9 | 4 |
| fallback reports written | 9 | 4 |
| runs with NO report at all | 0 | 0 |
| post-write idle exits | 9 | 0 |
| stuck-loop aborts | 0 | 3 |
| step-limit stops | 0 | 1 |
| backend errors | 0 | 0 |
| hand-verified REAL / THIN | 27 / 3 | 26 / 4 |
| wall, sum (s) | 926 | 981 (+6.0%) |
| wall, matched 21 pairs (s) | 546 | 524 (-4.2%) |
| prompt tokens, sum | 3.06 M | 4.11 M (+34%) |
| prompt tokens, matched | 1.73 M | 1.70 M (-1.8%) |

Where the empty model text is:

| fixture | ctl | wide |
|---|---|---|
| lpe-rope-calc-implement-strict-flag | 3 (idle exit) | 0 |
| neon-rain-implement-reset-shortcut | 3 (idle exit) | 1 (rep 2, step limit 30) |
| the-game-implement-shuffle-shortcut | 3 (idle exit) | 3 (stuck-loop abort) |

Same pattern as idle_capped_ab_2026_11 (ctl 9, wide 4), except wide's fourth is now a
step limit where it was an oMLX 500.

Where wide spends more (per-fixture sums over 3 reps, `runs.json`):

| fixture | wall ctl / wide (s) | prompt tokens ctl / wide (k) |
|---|---|---|
| lpe-rope-calc-implement-strict-flag | 258 / 311 | 666 / 1159 |
| neon-rain-implement-reset-shortcut | 92 / 112 | 492 / 995 |
| the-game-implement-shuffle-shortcut | 30 / 35 | 167 / 255 |
| other 7 fixtures | within 4 s and 18 k of each other | |

All of the extra cost is on runs ctl ends by idle exit; matched runs are flat. ctl
rep 1 was still slower than reps 2 and 3 despite the warm-up (346 s vs 286 s and
295 s; wide 333, 326, 322 s). Without rep 1, overall wall is ctl 581 s vs wide 648 s
(+11.5%), inside the +15% gate.

## Fallback report hand check

For each of the 13 runs, the report's file lines were compared with
`git apply --numstat` of the run's `diff.patch`, and its command lines with the
verification calls after the last successful write in
`~/.luxe/runs/<id>/messages.jsonl.gz`. The end reason was compared with
`single_mode_done.abort_reason` and the `post_write_idle_exit` event.

| arm | rep | fixture | run id | reason (checked) | files | commands | result |
|---|---|---|---|---|---|---|---|
| ctl | 1 | lpe strict-flag | bc42c56afa79 | post-write idle exit | 1 match | 5/5 (2 exit 0, 3 refused) | correct |
| ctl | 1 | neon reset-shortcut | 9113d81daee9 | post-write idle exit | 2 match | 3/3 (2 tool error, 1 refused) | correct |
| ctl | 1 | the-game shuffle | 761f33a359e2 | post-write idle exit | 1 match | 3/3 (1 tool error, 2 refused) | correct |
| ctl | 2 | lpe strict-flag | 709c38040305 | post-write idle exit | 1 match | 5/5 | correct |
| ctl | 2 | neon reset-shortcut | 361b450d24ca | post-write idle exit | 2 match | 3/3 | correct |
| ctl | 2 | the-game shuffle | 7d397042f835 | post-write idle exit | 1 match | 3/3 | correct |
| ctl | 3 | lpe strict-flag | f034fd55c34b | post-write idle exit | 1 match | 5/5 | correct |
| ctl | 3 | neon reset-shortcut | ec6a22fa1d40 | post-write idle exit | 2 match | 6/6 (2 tool error, 3 refused, 1 exit 127) | correct |
| ctl | 3 | the-game shuffle | 2e552c1156a7 | post-write idle exit | 1 match | 3/3 | correct |
| wide | 1 | the-game shuffle | 43642779632d | stuck-loop abort | 1 match | 6/6 (1 tool error, 2 refused, 1 exit 1, 2 not re-run) | correct |
| wide | 2 | neon reset-shortcut | 127605b82cf3 | step limit (30) | 3 match | 12 shown of 17, "5 more not shown" | correct |
| wide | 2 | the-game shuffle | d04546102c13 | stuck-loop abort | 1 match | 6/6 | correct |
| wide | 3 | the-game shuffle | 5d286757ad93 | stuck-loop abort | 1 match | 6/6 | correct |

**False claims: 0 of 13.** The step-limited run keeps the incomplete note above the
luxe header, as designed.

Limits of the check: the "last output line" quoted for a non-zero exit cannot be
checked against the saved conversation, because the tool message holds only
`exit code N` (see the incidental finding); `bytes_out` in events.jsonl is non-zero
for each such call, so the output existed. Commit and PR bodies were not inspected:
every run logged `pr_blocked` (no GitHub remote on the fixture clones). Design note:
on the 17-command run the report keeps the first 12 calls after the last write and
hides the last 5, which are the most recent.

## Hand-verified patches

`HAND-VERIFY.json`: 14 distinct patches, 8 reused from
`../idle_capped_ab_2026_11/HAND-VERIFY.json` by `git patch-id --stable`, 6 read by
hand against the fixture base source.

| fixture | patch | cells | class | note |
|---|---|---|---|---|
| lpe-rope-calc-document-typing | 8fcff185ac (new) | all 6 | REAL | annotation only |
| lpe-rope-calc-implement-strict-flag | 7daa217e67 (new) | ctl 1-3 | REAL | strict path never executed in-run (test commands refused) |
| lpe-rope-calc-implement-strict-flag | 400bed02fb (new) | wide 1-3 | REAL | same pe_scan.py; strict verified in-run; commits litter `test_pe_scan/fake.gguf` |
| neon-rain-implement-reset-shortcut | 00fa69e67c (new) | ctl 1 | REAL | document keydown, restart keeps the persona |
| neon-rain-implement-reset-shortcut | 40b24affb2 (new) | ctl 2 | REAL | persona dropped as in the existing Restart menu |
| neon-rain-implement-reset-shortcut | a1236be6b9 (reused) | ctl 3, wide 1, wide 3 | REAL | |
| neon-rain-implement-reset-shortcut | 47fe9d05bb (new) | wide 2 | THIN | listener on the board element, not document; Shift+R does nothing until the board is clicked. Step-limited run |
| nothing-ever-happens-document-config | 5d290a5ee5 (reused) | all 6 | THIN | 32/55 env vars |
| 6 other fixtures | reused | all | REAL | |

Counts: ctl 27 REAL / 3 THIN; wide 26 REAL / 4 THIN; 0 VACUOUS, 0 DAMAGING.

Patches differ from idle_capped_ab_2026_11 on lpe document-typing and strict-flag with
the same server, model and loop code. The abort report runs after the agent pass and
changes nothing the model sees, so this is temp-0 substrate noise; with no flag-off
arm, "no effect on the loop" holds by construction here, not by measurement.

## Gates

| gate | threshold | ctl | wide |
|---|---|---|---|
| model-written empty reports | reported, not gated | 9 | 4 |
| runs with no report | 0/30 | 0 PASS | 0 PASS |
| printed pass | 30/30 | 30 PASS | 30 PASS |
| hand-verified REAL | >= ctl | 27 (reference) | 26 FAIL |
| fallback reports with a false claim | 0 | 0/9 PASS | 0/4 PASS |
| matched-run wall | <= +5% vs ctl | reference | -4.2% PASS |
| overall wall | <= +15% vs ctl | reference | +6.0% PASS |

The wide REAL miss is one step-limited run and could be noise, but it is the gate as
written (model_behavior).

## Recommendation

1. Land `LUXE_ABORT_REPORT` default ON.
2. Do not promote `LUXE_IDLE_NARROW=wide`.

## Incidental finding: a failing command's output never reaches the model

`agents/loop.py:1496` sends `content = executed.error or executed.result`, and
`tools/shell.py:227,435` return `error = "exit code N"` for any non-zero exit, so the
tool message is the bare string `exit code N` (confirmed in the saved
conversations, e.g. run 43642779632d). A failing `pytest`, `npm test`, eslint or
build reaches the model as an exit code with no error text. In the code since
`7332da1` (v1.0). Follow-up A/B: `acceptance/failed_output_ab_2026_11/`.

## Analyzer fix

`analyze.py` counted stuck-loop aborts from event kinds that are never emitted (the
reason exists only on `single_mode_done.abort_reason`). Fixed: `stuck_abort` reads the
abort reason, `abort_reason` is kept in `runs.json`, and the per-arm table gains a
`stuck_abort` column.
