# tuning_2026_10 — hand verification

Every diff resolved per run through the reflog (`collect.py` + `attrcheck.py`:
0 mismatches in dev1, dev1b, q38full rep1–3). Graders: doc-config via
`envcheck.py` (base 59b8874); strict-flag executed via `strict_test.sh`;
everything else read. Grades: REAL / THIN / VACUOUS / BROKEN / FAIL.

## dev1 / dev1b — `commit_first` prompt probe (neon reset + doc-config)

| Arm | neon-rain-implement-reset-shortcut | nothing-ever-happens-document-config |
|---|---|---|
| Glimmer-30B-6bit + commit_first | FAIL — max steps, no diff | FAIL — max steps, no diff |
| Qwen3.8-27B-8bit (dev1) | ERROR — 30-min cap at step 14 (throughput) | ERROR — 30-min cap at step 23 |
| Qwen3.8-27B-8bit + commit_first (dev1) | REAL | ERROR — oMLX `incomplete_tool_call` (server) |
| Qwen3.8-27B-8bit (dev1b, 60-min cap) | REAL — keydown in `HtmlInputHandler` → `action:request {__restart}` | REAL — 27/27 env vars |
| Qwen3.8-27B-8bit + commit_first (dev1b) | REAL — new `KeyboardInputHandler`, `isPlaying` + repeat guards; byte-identical to dev1 | REAL — 27/27 env vars (40 min) |

ERROR rows are harness (cap / server), not model verdicts; dev1b re-ran them.

Glimmer trace (`diagrun.sh` with the commit_first overlay, `glim-cf-neon`):
correct plan in reasoning by step 19 (emit `__restart` from a keydown in
`HtmlInputHandler`), then re-litigated whether `_startNewGame()` drops the
persona and re-read `Game.js` 227–256 at steps 19, 24, 29 until max steps.
The write-pressure notice fired at step 15 and was ignored. Same shape as the
baseline-prompt trace — the appendix changed nothing.

## q38full — Qwen3.8-27B-8bit, baseline prompt, 10 × 3 (60-min cap)

| Fixture | rep1 | rep2 | rep3 | Notes |
|---|---|---|---|---|
| lpe-rope-calc-document-typing | REAL* | REAL* | REAL* | `f: BinaryIO` (the only untyped param). *Litter: committed `__pycache__/*.pyc` + a `test_smoke_typing.py` it labels "Temporary" |
| lpe-rope-calc-implement-strict-flag | REAL | REAL | REAL | identical diff ×3; strict_test: exit 1 on bad GGUF incl. LM Studio default path, exit 0 without `--strict` |
| the-game-implement-shuffle-shortcut | REAL | REAL | REAL | window keydown `r`, skips inputs/modifiers; rep2 via `useRef` |
| the-game-document-architecture | REAL | REAL | REAL | identical ×3; `selectWithNeighbors` description checked against server.js |
| neon-rain-implement-reset-shortcut | REAL | REAL | REAL | rep1/2 new `run:reset` listener (keeps confirm, PLAYING-only); rep3 `action:request {__restart}` |
| neon-rain-document-modules | REAL | REAL | REAL | identical ×3; per-subdir section + EventBus note |
| isomer-document-quickstart | REAL | REAL | REAL | three different rewrites, all cite compose port/volume + `/` login |
| isomer-implement-healthcheck | FAIL | FAIL | FAIL | identical ×3: the `/health` route is correct, but it leaves `health_check_tmp.py` ("delete after use") → `orphan_file` gate |
| nothing-ever-happens-manage-deps-audit | REAL | REAL | REAL | cve_lookup-sourced GHSA/CVE/PYSEC ids, fixed versions, same-major upgrade floors |
| nothing-ever-happens-document-config | FAIL | THIN | FAIL | rep1/3 no diff (rep3 29.5 min, 30k out tokens); rep2 misses all 23 `PM_NH_*` (helper-loop reads) |

**Per rep: 8 / 8 / 8 REAL (24/30).** Bar is ≥9 REAL. Two recurring defects
(3-rep rule): doc-config 0/3 REAL, healthcheck litter 3/3.
