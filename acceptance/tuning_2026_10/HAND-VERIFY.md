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

## Harness levers (2f34af7 + fix 166c319)

### glim_stub — Glimmer-30B-6bit + `LUXE_REPEAT_READ_STUB=1` (3 reps)

| Fixture | rep1 | rep2 | rep3 | Stub firings |
|---|---|---|---|---|
| neon-rain-implement-reset-shortcut | REAL | FAIL — max steps, no diff | FAIL — max steps, no diff | 0 / 0 / 1 |
| nothing-ever-happens-document-config | FAIL — no diff | FAIL — no diff | REAL — envcheck 27/27 | 0 / 0 / 0 |

- rep1 neon: Shift+R keydown in `HtmlInputHandler` (removed in `dispose`) →
  `game:reset` → `_resetRun()`: PLAYING-only, captures the persona before
  `_startNewGame({persona})` replaces state. Checked against base blob 59ca28e.
- **Neither pass had a stub firing**, so both are run-to-run variance, not
  the lever. The one firing (rep3 neon, step 15, `HtmlInputHandler.js`) did
  not change the outcome.
- Why it rarely fires: the loop re-reads come after compaction has rewritten
  the earlier result (`read_after_compact` 5–6 per failing neon run), and the
  stub deliberately declines when the earlier result is not intact in
  context.

### q38_cleanup — Qwen3.8-27B-8bit + `LUXE_CLEANUP_TURN=1` (3 reps)

`q38_cleanup_bug/` is the first attempt. Every cleanup turn there raised
`TypeError` (maintain passes a `str` repo) and deleted nothing. Invalid, kept
for the record; fixed in 166c319.

| Fixture | rep1 | rep2 | rep3 | Cleanup turn (rep1/2/3) |
|---|---|---|---|---|
| isomer-implement-healthcheck | REAL | REAL | REAL | — / deleted `health_check_tmp.py` / same |
| lpe-rope-calc-document-typing | REAL (`IO[bytes]`) | REAL (`BinaryIO`) | REAL (`BinaryIO`) | — / deleted 2 pyc + `test_smoke_typing.py` / same |
| neon-rain-implement-reset-shortcut | **BROKEN** (rep1_neon) | **BROKEN** | REAL | — / — / — |

- Healthcheck: identical `@app.route("/health")` → `jsonify({"status": "ok"})`
  ×3. `before_request` returns early without a session, so anonymous GET
  answers 200. rep1 wrote no scratch file (0 candidates). In rep2/3 the
  scratch file was offered and deleted, and the orphan gate passed.
- Typing: `f` was the only untyped param. In rep2/3 the litter was removed;
  rep1 left none.
- neon rep2 BROKEN (printed PASS): binds `this._onKeyDown` in the
  `HtmlInputHandler` constructor but never defines it. The TypeError fires on
  every `_startNewGame`. The first edit landed at step 29/30 and the run
  aborted at max steps (43 min). The partial edit was committed, and
  `npm test` doesn't construct the handler. The cleanup turn had 0
  candidates, so it is not the cause.
- neon rep3: new `KeyboardInputHandler.js` (imported by `Game.js`, so never a
  candidate) with a confirm modal, PLAYING guard, and persona kept.
- rep1 neon in the main pass errored at 0s (harness): stopping the bugged arm
  left an orphaned `luxe maintain` holding the repo lock. Re-run as
  `rep1_neon/`.
- **No legitimate file was offered or deleted in any run.** Every deletion
  was a file the model itself labelled temporary, or a pyc.
- rep1_neon (re-run) is BROKEN, printed PASS, clean finish at 6.5 min. A
  Shift+R keydown in `HtmlInputHandler` emits `run:reset`, and `Game.js`
  routes that to `this._restart()`. That method exists in neither base
  59ca28e nor the result, so the shortcut throws. The cleanup turn had 0
  candidates.

**Qwen3.8 + cleanup turn: 7/9 REAL** (healthcheck 3/3, typing 3/3, neon
1/3). The baseline q38full had the same three fixtures at 6/9 (healthcheck
0/3, typing REAL* 3/3, neon 3/3).
