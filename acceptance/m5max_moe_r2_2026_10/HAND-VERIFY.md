# m5max_moe_r2 — R1 hand-verify

Every printed pass read by hand against the fixture goal and the base repo
(`~/.luxe/fixture-cache/<repo>` @ base_sha). Diffs: `r1/_review/<arm>/*.diff`
(collect.py). Grades: **REAL** (does what the goal asks, correct against the
base), **THIN** (technically meets the goal, materially incomplete),
**VACUOUS** (grader satisfied, feature does not work), **FAIL** (printed fail).
Advance bar (PLAN.md P4): ≥9 REAL, no fixture worse than base, verification
calls present.

## laguna-xs-2.1-6bit — 8 REAL / 0 THIN / 1 VACUOUS / 1 FAIL → does NOT advance

Printed 9/10 (37/50), wall 9.2 min (champion 7.9), 8 verify calls (champion 12).

| fixture | grade | note |
|---|---|---|
| isomer-document-quickstart | REAL | access section + login/dashboard flow; port matches compose/app.py |
| isomer-implement-healthcheck | REAL | `/health` → `jsonify({"status":"ok"}),200`; jsonify already imported; `before_request` returns early sans session, so unauthenticated GET works |
| lpe-rope-calc-document-typing | REAL | `f` was the ONLY untyped top-level param in base; typed `BinaryIO` (+ import) |
| lpe-rope-calc-implement-strict-flag | REAL | `--strict` exit 1 on header errors; return types became tuples and EVERY caller (all in pe_scan.py) updated |
| neon-rain-document-modules | REAL | per-subdir section; spot-checked Ctrl+Shift+D debug + PLANNING phase claims against src |
| neon-rain-implement-reset-shortcut | **VACUOUS** | emits `__restart` as an EVENT NAME — no listener; restart goes via `emit(action:request,{type:__restart})`. Also tests `e.key === r` under shiftKey (browser sends `R`). Shift+R does nothing; `npm test` passes because nothing tests it. Both refs got it right (`R` + a wired listener) |
| nothing-ever-happens-document-config | FAIL | no write in 29 tools / 136s, context_overflow bail; no branch committed |
| nothing-ever-happens-manage-deps-audit | REAL | SECURITY-AUDIT.md, 3 packages w/ CVE + fixed version from cve_lookup, upgrade table |
| the-game-document-architecture | REAL | `selectWithNeighbors` description verified line-by-line (6-group, random position, edge clamp) |
| the-game-implement-shuffle-shortcut | REAL | keydown on document, input/textarea guarded, cleanup; `[]` deps safe — `shuffle` uses setters only |

No fixture left worse than base (neon-rain adds a dead listener, harmless).

## References (same standard)

### qwen3.6-35b-a3b-6bit-ref (champion) — 9 REAL / 1 THIN / 0 VACUOUS / 0 FAIL

Printed 10/10 (40/50), wall 7.9 min, 12 verify calls.

| fixture | grade | note |
|---|---|---|
| isomer-document-quickstart | REAL | access comment rewritten in place; port/compose binding correct |
| isomer-implement-healthcheck | REAL | `/health` GET → jsonify ok 200 |
| lpe-rope-calc-document-typing | REAL | `f: IO[bytes]` + tightened `_pe_from_gguf` dict types |
| lpe-rope-calc-implement-strict-flag | REAL | `--strict` lists bad files to stderr, exit 1; every caller updated. Nits: unreachable duplicate `return 0`; a PARTIALLY-read header (`return meta, True` after an exception) is not flagged |
| neon-rain-document-modules | REAL | per-subdir list, accurate |
| neon-rain-implement-reset-shortcut | REAL | `e.key === R` under shift, new `game:restart` → `_startNewGame()`; skips the confirm dialog and drops persona, but that is exactly what the existing `__restart` path does |
| nothing-ever-happens-document-config | **THIN** | 26/27 real env reads covered (misses `PM_NH_MARKET_REFRESH_INTERVAL_SEC`) BUT documents `HOST`/`CHAIN_ID`/`SIGNATURE_TYPE` as env vars — they come from config.json (`conn.get`), two marked "required", SIGNATURE_TYPE default wrong (says 1, code 2); `file:line` refs inaccurate (`config.py:101` is `if (`) |
| nothing-ever-happens-manage-deps-audit | REAL | 3 vulnerable pins w/ GHSA+CVE+fixed version, upgrade advice |
| the-game-document-architecture | REAL | two-layer explanation, line refs verified (shuffle L26, selectWithNeighbors L103) |
| the-game-implement-shuffle-shortcut | REAL | input/textarea/contentEditable guard, cleanup |

### qwen3.6-27b-6bit-ref (m5 dense fallback) — 9 REAL / 1 THIN / 0 VACUOUS / 0 FAIL

Printed 10/10 (40/50), wall 31.3 min, 19 verify calls.

| fixture | grade | note |
|---|---|---|
| isomer-document-quickstart | REAL | restructured into numbered steps, commands preserved |
| isomer-implement-healthcheck | REAL | |
| lpe-rope-calc-document-typing | REAL | `f: BinaryIO` |
| lpe-rope-calc-implement-strict-flag | REAL | opt-in `failed` list threaded through scanners; gap: `scan_lmstudio` calls `scan_gguf_dir` without `failed`, so LM Studio GGUFs escape `--strict` |
| neon-rain-document-modules | REAL | + EventBus paragraph |
| neon-rain-implement-reset-shortcut | REAL | best of the three: `R` + routes through `_handleAction({type:__restart})`, keeping the confirm dialog |
| nothing-ever-happens-document-config | **THIN** | 41 names but omits ALL 23 `PM_NH_*` strategy overrides — the bulk of runtime config |
| nothing-ever-happens-manage-deps-audit | REAL | |
| the-game-document-architecture | REAL | |
| the-game-implement-shuffle-shortcut | REAL | activeElement guard incl. select/role=textbox |

**Calibration:** both references land at exactly 9 REAL — the P4 bar is the
champions own level, not above it. doc-config is THIN for both: the suites
hardest fixture, where printed passes are least trustworthy.

## lfm2.5-24b-a2b-6bit — ELIMINATED (repo left worse than base) → does NOT advance

Printed 8/10 (34/50), wall 11.6 min, 5 verify calls, 179k reasoning chars
(5× the champion's). Graded only far enough to establish elimination:

| fixture | grade | note |
|---|---|---|
| lpe-rope-calc-document-typing | **BROKEN (printed pass)** | added `from io import BinaryIO` — `io` has no BinaryIO; `pe_scan.py` now dies with ImportError on every invocation (reproduced from the run's branch). The regex grader credits the annotation |
| neon-rain-implement-reset-shortcut | **VACUOUS** | emits `'reset'` (no listener) while adding an unreachable `__reset_run` action; binds keydown on the board `<div>` (not focusable, never fires) |
| isomer-document-quickstart | FAIL | printed 1/5 |
| the-game-implement-shuffle-shortcut | FAIL | no output, `stuck_no_output` |
| isomer-implement-healthcheck | REAL | |
| others | not graded | elimination already certain (≤6 REAL possible, damage present) |

## muse-glimmer-30b-6bit — 9 REAL / 0 THIN / 0 VACUOUS / 1 FAIL → ADVANCES (P4 bar met)

Printed 8/10 (34/50) — hand-verify flips one printed FAIL. Wall **46.7 min
(5.9× the champion)**, vlm engine (no cross-turn cache reuse), 13 verify
calls (champion 12), 148k reasoning chars. Preflight drill FAILED 2/2 on
behavior (re-runs pytest, never edits) — yet the bench says capable; the
drill's 12-step budget is tight for this model's verify-heavy style.

| fixture | grade | note |
|---|---|---|
| isomer-document-quickstart | **REAL (printed FAIL)** | failed ONLY the legacy `min_added_lines ≥4` floor (2 dense lines); spec R1 docker-compose / R2 port / R3 dashboard-access all satisfied, setup steps intact. Content accurate (`app.run(...port=27001)`, compose `127.0.0.1:27001:27001`) |
| isomer-implement-healthcheck | REAL | |
| lpe-rope-calc-document-typing | REAL | `f: BinaryIO` from typing |
| lpe-rope-calc-implement-strict-flag | REAL | cleanest of four arms: module-level set filled at EVERY failure point incl. partial header; stderr list + exit 1; no signature churn |
| neon-rain-document-modules | REAL | per-subdir; `TextRenderer` claim verified (src/render/TextRenderer.js) |
| neon-rain-implement-reset-shortcut | FAIL | no write in 30 tools, context_overflow |
| nothing-ever-happens-document-config | **REAL** | all 27 env reads covered, nothing invented (only `PM_NH_` prefix + compose `POSTGRES_PASSWORD`) — the ONLY arm, refs included, to get this fixture fully right |
| nothing-ever-happens-manage-deps-audit | REAL | GHSA+CVE+PYSEC aliases, fixed versions, requirements.txt line cites, API-compat reasoning |
| the-game-document-architecture | REAL | `loadStudioLists`/`fetchPlexLibrary`/sections URL verified |
| the-game-implement-shuffle-shortcut | REAL | `toLowerCase()` (Shift+R also shuffles — acceptable), input guard, cleanup |

Gated 4-bit (P4b): Glimmer SURVIVED at 6-bit → its 4-bit arm runs.

## k2-horizon-mova-36b-a4b-6bit — ELIMINATED (repo left worse than base) → does NOT advance

Self-converted (6b g64 affine, routers BF16 by the model's quant_predicate,
template/tokenizer byte-equal to upstream). Printed 9/10 (37/50), wall
**18.0 min (2.3× the champion — fastest challenger)**, 5 verify calls,
126k reasoning chars.

| fixture | grade | note |
|---|---|---|
| lpe-rope-calc-implement-strict-flag | **BROKEN (printed pass)** | threads `errors` into `scan_ollama`/`scan_gguf_dir`, then calls `scan_lmstudio(root, errors)` WITHOUT changing its 1-arg signature. Reproduced on the run's branch: bare `python3 pe_scan.py` → `TypeError: scan_lmstudio() takes 1 positional argument but 2 were given`. Every default invocation crashes, `--strict` or not. Grader 4/5, `gates_triggered` empty |
| neon-rain-implement-reset-shortcut | FAIL | no write in 37 tools, context_overflow |
| isomer-document-quickstart | REAL | accurate run/logs/dashboard-access steps + port; gap: doesn't name docker-compose.yml / app.py (already named elsewhere in README) |
| isomer-implement-healthcheck | REAL | |
| lpe-rope-calc-document-typing | REAL | `f: BinaryIO` |
| neon-rain-document-modules | REAL | most complete of all arms (CooldownTracker, IOBuffer, ModalManager, …) |
| nothing-ever-happens-document-config | REAL | all 27 env reads, nothing invented |
| the-game-document-architecture | REAL | file:line refs exact (server.js:31/51/103/141, App.jsx:26/41/90) |
| the-game-implement-shuffle-shortcut | REAL | |
| nothing-ever-happens-manage-deps-audit | not graded | elimination already certain |

Gated 4-bit (P4b): K2 did NOT survive at 6-bit → no K2-4bit conversion/arm.
Strongest docs of any arm; the damage is one unpropagated signature change —
exactly the class of error `gates_triggered` and both graders miss.

# P4b — gated 4-bit track (output `r1_4bit/`)

4-bit preflight (`preflight_4bit/`): all three arms' plumbing passes
(structured tool calls); drill FAILED for the 35B-A3B-4bit (stuck in loop:
same tool calls 2 turns running) and Glimmer-4bit (max steps, the 6-bit
over-verification habit); 27B-4bit drill passed (6 steps, 23s).

strict-flag diffs were also EXERCISED, not just read (`strict_test.sh`: apply
to clean base; bare run, bad GGUF ± `--strict`, bad GGUF in the LM Studio
default path). Correct for: 35B-6bit, Glimmer-6bit, 35B-4bit, 27B-4bit.
K2-6bit: bare run → TypeError (elimination confirmed by execution).

## qwen3.6-35b-a3b-4bit-ref (m1/m4 INTERACTIVE MAIN) — ≤7 REAL / 1 THIN / 1 VACUOUS / 1 FAIL

Printed 9/10 (37/50), wall 11.3 min, 12 verify calls. **Measurably worse
than its own 6-bit build (9 REAL / 1 THIN):**

| fixture | grade | note |
|---|---|---|
| neon-rain-implement-reset-shortcut | **VACUOUS** | `e.shiftKey && e.key === 'r'` — Shift sends `'R'`; never fires (the 6-bit champion used `'R'` correctly) |
| nothing-ever-happens-document-config | **THIN** | omits all 23 `PM_NH_*` + `FUNDER_ADDRESS` |
| nothing-ever-happens-manage-deps-audit | FAIL | runaway output: 3 consecutive turns at the ~8,192-token cap (completion 8.6k→16.8k→25.0k), truncated-turn retries exhausted, no write |
| lpe-rope-calc-implement-strict-flag | REAL | exercised: all 4 cases correct |
| isomer-implement-healthcheck / document-typing / shuffle-shortcut | REAL | shuffle refactors `shuffle` into `useCallback` — sound |
| isomer-document-quickstart | REAL | names docker-compose.yml + app.py, port correct, setup intact |
| neon-rain-document-modules / the-game-document-architecture | not read in depth | docs; cannot raise the count above 7 |

## qwen3.6-27b-4bit-ref (m1/m4 FALLBACK) — ≥8 REAL verified, 0 THIN / 0 VACUOUS / 0 FAIL found

Printed 10/10 (40/50), wall 33.6 min, 21 verify calls. **No worse than — in
places better than — its 6-bit build:**

| fixture | grade | note |
|---|---|---|
| neon-rain-implement-reset-shortcut | REAL | best of ALL arms: accepts `'R'`/`'r'`, confirm dialog, PRESERVES the persona on restart |
| nothing-ever-happens-document-config | REAL | all 27 env reads, nothing invented (the 6-bit build omitted all 23 `PM_NH_*`) |
| lpe-rope-calc-implement-strict-flag | REAL | exercised: all 4 cases correct (incl. the LM Studio path its 6-bit build missed) |
| isomer-document-quickstart | REAL | restructured into numbered steps; ISOMER_SECRET, token_urlsafe, bootstrap retrieval, ISOMER_BOOTSTRAP_PASSWORD all preserved |
| isomer-implement-healthcheck | REAL | `@csrf.exempt` — `csrf = CSRFProtect(app)` defined at L57, before the route |
| document-typing / shuffle-shortcut | REAL | shuffle via `useRef` to dodge the stale closure |
| deps-audit / document-modules / document-architecture | not read in depth | docs/report; printed passes |

## muse-glimmer-30b-4bit — ≤8 REAL → does NOT clear the bar at 4-bit

Printed 7/10 (31/50), wall 27.6 min, 12 verify calls. Fails neon-rain (no
write, context_overflow — same as 6-bit) AND doc-config (no write in 30
tools, context_overflow — the fixture its 6-bit build was the best arm on).
the-game-document-architecture printed FAIL only on the `min_added_lines ≥8`
floor (6 lines) — even crediting it, ceiling is 8 REAL. 4-bit costs Glimmer
real capability; it is not a 4-bit candidate for m1/m4.

# R2 confirmation (output `r2/rep{1,2,3}`)

Method: at temp=0 most diffs are byte-identical across reps (compared with
`index` lines stripped). Identical diffs inherit the R1 grade; only CHANGED
diffs are re-graded (doc-config via envcheck.py, strict-flag executed via
strict_test.sh, the rest read).

## rep1 (14:45 → 16:19)

| arm | printed | wall | changed vs R1 | hand-verified |
|---|---|---|---|---|
| qwen3.6-35b-a3b-6bit-ref | 10/10 | 8.1 min | strict-flag (exercised: correct), doc-config (all 27, nothing invented) | **10 REAL** |
| qwen3.6-27b-6bit-ref | 10/10 | 30.1 min | neon-rain (`'R'`, confirm via `_handleReset`: REAL), doc-config (omits all 23 `PM_NH_*` again: THIN) | 9 REAL / 1 THIN |
| muse-glimmer-30b-6bit | 8/10 | 54.9 min | neon-rain (still no write: FAIL), doc-config (complete: REAL) | 9 REAL / 1 FAIL |

Variance lives in the hard fixtures (doc-config for all three, strict-flag,
neon-rain). The champion's R1 doc-config THIN did NOT recur — it was variance;
the 27B's doc-config gap recurs — systematic.

## R2 rep2 (2026-10-16, 16:19–17:45)

25/30 diffs byte-identical to rep1 (index lines stripped); only the 5 changed ones re-graded.

| arm | fixture | grade | note |
|---|---|---|---|
| champion 35B-6bit | doc-config | **THIN** | envcheck: 7 invented names (CLOB_API_KEY/_ID/_PASSPHRASE/_SECRET, CLOB_CHAIN_ID, HOST, SIGNATURE_TYPE) + `FUND_ADDRESS` (code reads `FUNDER_ADDRESS`, left undocumented). Same failure shape as R1 |
| 27B-6bit | doc-config | **REAL** | all 23 PM_NH_* documented this time; extras are prose mentions / log-level values, not invented vars. The "systematic THIN" call from R1+rep1 is RETRACTED: it is variance |
| 27B-6bit | neon reset | REAL | `shiftKey && (r\|R)` → `emit(action:request,{type:__restart})`, the existing restart path; listener removed in dispose |
| Glimmer-6bit | neon reset | FAIL | empty diff, max steps (30) at 0.53 peak — 3/3 runs |
| Glimmer-6bit | doc-config | **FAIL** | empty diff, max steps (30) at 0.56, 0 writes, 11 read_after_compact. Its one distinguishing win (complete config doc in R1+rep1) did not hold |

Totals: champion 9 REAL / 1 THIN (8.4 min) · 27B 10 REAL (31.7 min) · Glimmer 8 REAL / 2 FAIL (44.8 min).
Zero schema rejects / tool_rejects / textfallback drops / tool errors again.

doc-config across 3 runs: champion THIN/REAL/THIN · 27B THIN/THIN/REAL · Glimmer REAL/REAL/FAIL — the one fixture that moves, and it moves for every arm.

## R2 rep3 (2026-10-16, 17:45–19:02)

Novel diffs only (24/30 match an earlier run byte-for-byte):

| arm | fixture | grade | note |
|---|---|---|---|
| champion 35B-6bit | doc-config | **REAL** | envcheck clean: 0 undocumented; only extra is `POSTGRES_PASSWORD`, which is real (docker-compose.yml / .env.example) |
| 27B-6bit | doc-config | **THIN** | omits all 23 PM_NH_* again (R1 pattern) |
| 27B-6bit | neon reset | REAL | `shift && (r\|R)` → new `game:reset` with a wired listener → confirm dialog → `_startNewGame()`, gated to PLAYING |
| Glimmer-6bit | neon reset | FAIL | no branch created; max steps (30) |
| Glimmer-6bit | doc-config | FAIL | empty; max steps (30) |
| Glimmer-6bit | deps-audit | REAL | same three advisories (python-dotenv, web3, aiohttp) as rep1, terser. Diverged at step 8 because `cve_lookup` (live OSV) returned 20 more tokens: an environment nondeterminism source, not the model |

Totals: champion 10 REAL (8.0 min) · 27B 9 REAL / 1 THIN (25.1 min) · Glimmer 8 REAL / 2 FAIL (43.5 min).

### Four-run tally (R1 + R2 rep1–3), 6-bit arms

| arm | REAL / 40 | doc-config | neon | everything else | avg wall |
|---|---|---|---|---|---|
| champion 35B-6bit | 38 (+2 THIN) | THIN·REAL·THIN·REAL (2/4) | 4/4 | 32/32 | 8.1 min |
| 27B-6bit | 37 (+3 THIN) | THIN·THIN·REAL·THIN (1/4) | 4/4 | 32/32 | 29.0 min |
| Glimmer-6bit | 34 (+6 FAIL) | REAL·REAL·FAIL·FAIL (2/4) | **0/4** | 32/32 | 47.5 min |

Glimmer's R1 advance rested on doc-config; over four runs it ties the champion there (2/4), and its neon
failure recurs 4/4 → **eliminated under the 3-rep rule**. The champion and 27B have no failure that recurs in
≥2 runs other than doc-config THIN, which every arm shares.

## R2-4bit reps 2–3 (2026-10-16, 19:03–20:34) + the two previously unread R1 doc fixtures

R1 gaps closed: neon-rain-document-modules + the-game-document-architecture read for both 4-bit arms (all four
REAL: every subdirectory described accurately, line refs to `selectWithNeighbors()` / `shuffle()` correct), and
27B-4bit deps-audit REAL (python-dotenv, web3, aiohttp, all with OSV ids + fixed versions). Those diffs are
byte-identical in reps 2–3, so the grades carry.

| arm | fixture | R1 | rep2 | rep3 | note |
|---|---|---|---|---|---|
| 35B-4bit (m1/m4 MAIN) | neon reset | VACUOUS | VACUOUS | VACUOUS | **byte-identical all 3**: `e.shiftKey && e.key === 'r'` — Shift+R sends `'R'`, so it never fires |
| 35B-4bit | doc-config | THIN | THIN | THIN | rep2: 19 vars, omits all 23 PM_NH_*, invents 3 (`LATENCY_LOG_THRESHOLD_MS`, `PM_LIVE_RECOVERY_*`); rep3: omits PM_NH_* + `FUNDER_ADDRESS` |
| 35B-4bit | deps-audit | FAIL | REAL | REAL | R1 = 3 turns at the 8,192 cap. Reps 2–3 (identical) diverge at step 2 with the prompt 34 tokens longer, i.e. a tool output changed first (live `cve_lookup`): environment-triggered, 1/3 |
| 27B-4bit (m1/m4 FALLBACK) | neon reset | REAL | REAL | REAL | rep2 keeps persona; rep3 confirm dialog gated to PLAYING; both `r\|R` + wired listener |
| 27B-4bit | doc-config | REAL | REAL | REAL | rep2: no undocumented reads, extras real (POSTGRES_PASSWORD) or prose; rep3: 26/27, misses `PM_NH_EXIT_ON_ADVERSE_MOVE_PCT` (minor) |

Totals: 35B-4bit 7 · 8 · 8 = **23/30 REAL**, 10.1 min avg · 27B-4bit 10 · 10 · 10 = **30/30 REAL**, 34.9 min avg.

**Finding for the m1/m4 manifest (no change without user approval):** the interactive MAIN on m1/m4 carries two
REPRODUCIBLE defects (neon VACUOUS 3/3 byte-identical, doc-config THIN 3/3) that its 6-bit build does not have
(neon 4/4 REAL). The FALLBACK has none, at ~3.5× the wall. The 2026-07-30 MoE-first flip bought chat latency;
this is the correctness price, measured.

## F rep1 — ungated 4-bit head-to-head (2026-10-16, 20:35–21:12)

Preflight: all three ✓ drill (pytest + diff). ⚠ on LFM/K2 = precision-parity note only (refs keep 39 tensors at
8-bit; LFM keeps 1 at 6-bit, K2 none — K2's routers stay BF16). K2-4bit conversion verified: `mlp.gate` +
`v_router` BF16, no router scales, tokenizer files byte-equal to upstream.

### k2-horizon-mova-36b-a4b-4bit — 8 REAL / 1 VACUOUS / 1 FAIL, 15.1 min

| fixture | grade | note |
|---|---|---|
| lpe-rope-calc-implement-strict-flag | **VACUOUS** | executed: bad GGUF + `--strict` exits **0**. `--strict` is parsed and threaded into the scanners, but `main` never passes a `skipped` list, so nothing is collected and the exit code never changes. Its 6-bit build was BROKEN on the same fixture (TypeError) → K2 fails strict-flag at both precisions |
| neon-rain-implement-reset-shortcut | FAIL | empty diff; max steps (42 tool calls) |
| nothing-ever-happens-document-config | REAL | envcheck perfect: 27/27, nothing invented — best of any 4-bit arm (its 6-bit build also wrote the best docs) |
| isomer-document-quickstart | REAL | numbered steps, explains env_file + loopback publish, keeps nginx note, bootstrap password intact |
| isomer-implement-healthcheck | REAL | GET /health → jsonify; anonymous requests pass the `before_request` hook (no sid → early return) |
| the-game-implement-shuffle-shortcut | REAL | `'r'` per spec, skips INPUT/TEXTAREA/contentEditable, bound once; `shuffle` touches only setters so the [] closure is safe |
| lpe-rope-calc-document-typing | REAL | `f: BinaryIO` imported from `typing` (the 6-bit LFM's `from io` bug absent) |
| neon-rain-document-modules / the-game-document-architecture / deps-audit | REAL | accurate per-directory roles; correct server.js line refs; audit with OSV ids + fixed versions |

### laguna-xs-2.1-4bit — ≤6 REAL / 1 BROKEN / 1 THIN / 2 FAIL, 7.0 min → cannot clear the bar

| fixture | grade | note |
|---|---|---|
| neon-rain-implement-reset-shortcut | **BROKEN** (printed PASS) | renames the EXISTING `addEventListener`/`removeEventListener` to `addEventlistener` → TypeError in the constructor: click + hover die with it. Also `e.key === 'r'` under shift. Grader credited it |
| isomer-document-quickstart | THIN | cosmetic rewording; drops "loopback-only; put nginx in front for remote" |
| nothing-ever-happens-document-config | FAIL | empty; max steps |
| the-game-implement-shuffle-shortcut | FAIL | empty (8 writes, nothing kept); max steps |
| lpe-rope-calc-implement-strict-flag | REAL | executed: all 4 cases correct |
| remaining 5 | not read in depth | cannot lift it to ≥9 |

### lfm2.5-24b-a2b-4bit — ≤7 REAL / 1 BROKEN / 1 VACUOUS / 1 FAIL, 14.4 min → cannot clear the bar

| fixture | grade | note |
|---|---|---|
| lpe-rope-calc-implement-strict-flag | **BROKEN** | executed: bad GGUF WITHOUT `--strict` now exits 1 (default behaviour changed), and `--strict` crashes: `AttributeError: 'NoneType' object has no attribute 'source'` |
| neon-rain-implement-reset-shortcut | VACUOUS | keydown bound on the board `div` (never focused → never fires) and emits a bare `restart` bus event with no listener (it added an `action.type === 'restart'` branch that nothing routes to) |
| lpe-rope-calc-document-typing | FAIL | one annotation (`f: Any`) |
| nothing-ever-happens-document-config | REAL (nit) | 27/27 reads documented; one invented name `PM_NH_CATEGORY_KEYWORDS` (real: `PM_NH_MAX_POSITIONS_PER_CATEGORY_KEYWORD`, also documented) |
| remaining 6 | not read in depth | cannot lift it to ≥9 |

**vs the m1/m4 incumbents (3-run):** 35B-4bit main 7·8·8 REAL @ 10.1 min · 27B-4bit fallback 10·10·10 @ 34.9 min.
K2-4bit (8 REAL, 15.1 min) is level with the main on count, but its strict-flag defect recurs across precisions.
Reps 2–3 decide it. Differentiators to run if it stays level: engine (K2 = llm engine, Qwen3.6 = vlm — the
m1/m4 chat-latency question), longchat turn carry, m4 48 GB fit (19.7 GB).

## Grading-tool corrections (2026-10-16, ~22:45) — read before any grade below

1. **collect.py mis-attributed diffs.** It diffed `base..<branch name>`, but bench branch names are
   first-free (`…-config-md-at-the-6`): a run that left no commit had its name taken by the NEXT arm, and
   older names get reused later. Audit vs the bench-time stat in result.json: 14/310 wrong at first
   collection (all bench-empty runs shown with another arm's diff), 30/310 on re-collection.
   **No grade changed:** every non-empty diff read before the fix matched its bench stat, and every
   bench-empty run had been graded FAIL from the stat. Fix: resolve the run's own commit from the clone's
   HEAD reflog at the pr_state commit time (checkout fallback for model-made commits), and refuse
   (MISATTRIBUTED / "left no commit") whenever the stat disagrees. Now 0/310 mismatches (`attrcheck.py`).
2. **envcheck.py counted only `backticked` names**, under-reporting docs that list names in **bold**
   (LFM2.5's showed "4 documented" for a doc covering 21–24 of 23 PM_NH_*). Now backticks + bold + table
   cells, source read at base_sha. Re-ran over all 31 doc-config diffs: **every earlier THIN/REAL holds.**

## F reps 2–3 (21:12–22:30) — 3-run verdicts for the ungated 4-bit challengers

K2 / Laguna / LFM2.5 (all llm engine) diverge between reps on 7–9 of 10 fixtures; the Qwen3.6 arms (vlm)
on 1–3. Input to the determinism test.

| arm | fixture | R1 | rep2 | rep3 |
|---|---|---|---|---|
| K2-4bit | strict-flag (executed) | VACUOUS | **BROKEN** — LM Studio path `--strict` crashes `AttributeError: 'list' object has no attribute 'source'` | **BROKEN** — bare run crashes `ValueError: not enough values to unpack` |
| K2-4bit | neon reset | FAIL (empty) | FAIL (empty) | FAIL (empty) |
| K2-4bit | doc-config | REAL 27/27 | REAL 27/27 | FAIL (empty, max steps) |
| K2-4bit | quickstart | REAL | REAL (minimal: one paragraph; printed FAIL = line floor) | REAL |
| K2-4bit | shuffle / modules / deps-audit | REAL | REAL | REAL |
| LFM-4bit | strict-flag (executed) | BROKEN | **BROKEN** — `SyntaxError: unterminated string literal` | REAL |
| LFM-4bit | neon reset | VACUOUS | FAIL (empty) | VACUOUS — keydown on the board div (no tabindex anywhere → never focused) |
| LFM-4bit | document-typing | FAIL | THIN (`f: io.IOBase` — IOBase has no `read` in typeshed) | REAL (`IO[bytes]`) |
| LFM-4bit | doc-config | REAL (nit) | REAL (25/27) | THIN (invents `RECOVERY_*`, real names are `PM_RECOVERY_*`) |
| LFM-4bit | neon-rain-document-modules | — | THIN (ai/ "decides the hacker's last action" — it drives the server) | REAL |
| Laguna-4bit | neon reset | BROKEN | FAIL (empty) | FAIL (empty) |
| Laguna-4bit | shuffle | FAIL | FAIL | FAIL |
| Laguna-4bit | doc-config | FAIL | FAIL | THIN (0/23 PM_NH_*) |
| Laguna-4bit | quickstart | THIN | REAL (drops the nginx note only) | THIN (drops loopback/nginx, adds a false "overview of all companies") |
| all three | healthcheck, the-game-document-architecture | byte-identical to R1 in both reps | | |

**Tallies (3 runs, REAL / 30):**

| arm | REAL | recurring (≥2/3) | avg wall |
|---|---|---|---|
| 27B-4bit (m1/m4 fallback) | **30** | none | 34.9 min |
| 35B-4bit (m1/m4 main) | 23 | neon VACUOUS 3/3, doc-config THIN 3/3 | 10.1 min |
| K2-4bit | 23 | strict-flag 3/3 (2× BROKEN = crashes code that worked), neon FAIL 3/3 | 17.8 min |
| LFM-4bit | ≤21 | strict-flag BROKEN 2/3, neon non-REAL 3/3 | 12.7 min |
| Laguna-4bit | ≤19 | neon 3/3, shuffle FAIL 3/3, doc-config 3/3 | 7.2 min |

**Verdict: no ungated 4-bit challenger displaces either m1/m4 incumbent.** K2 ties the main on count but its
recurring defect is the worse class: it breaks code that ran (2/3), where the main's ships silent no-ops.
It is also 1.8× slower. Laguna and LFM2.5 recur on three fixtures each. The live m1/m4 question stays
main-vs-fallback (R2-4bit section).

## P4c harness-fairness ablations — H1 (max_steps 60) + H2 (LUXE_TIERED_COMPACT=0), 3 reps each (2026-10-16 22:30 → 10-17 04:57)

Question: are the challengers' failures on the three hard fixtures (strict-flag, neon reset, doc-config) an
artifact of luxe's step budget or compaction, i.e. would a fairer harness convert them while the champion
gains nothing? Collected with the reflog-resolving collect.py; attrcheck 0 mismatches / 108 diffs. strict-flag
EXECUTED (strict_test.sh, unique diffs deduped by hash), doc-config via envcheck.py (base 59b8874), neon READ.
Same standards as above (a key test of `'r'` under shift = VACUOUS; keydown on the un-focusable board div = VACUOUS;
the LM-Studio `--strict` gap = REAL-with-gap, as graded for the 27B in R1).

| arm | strict-flag H1 / H2 | neon reset H1 / H2 | doc-config H1 / H2 | REAL / 18 |
|---|---|---|---|---|
| champion 35B-6bit | R R R / R R R | R R R / R R R (byte-identical ×6: `'R'` + wired `game:restart`) | THIN R R / R R R | **17** |
| 27B-6bit | R R R / R R R (byte-identical, LM-Studio gap) | R R R / R R R | R THIN THIN / R THIN THIN | 14 |
| K2-6bit | R R **BROKEN** / R R R | VAC VAC FAIL / VAC VAC FAIL | R R R / R R FAIL | 10 |
| Glimmer-6bit | R R R / R R R (byte-identical, 705 s each) | FAIL ×3 / FAIL ×3 (max steps even at 60, peak 0.55) | FAIL R FAIL / R FAIL R | 9 |
| LFM-6bit | R R R / R R R (byte-identical) | R FAIL FAIL / VAC VAC VAC | THIN ×3 / THIN ×3 (byte-identical: omits all 23 PM_NH_* + PRIVATE_KEY/FUNDER_ADDRESS) | 7 |
| Laguna-6bit | R R R / R R R | FAIL FAIL **BROKEN** / **BROKEN** ×3 | FAIL ×3 / THIN FAIL FAIL | 6 |

Notes:
- K2 H1 rep3 strict-flag: bare `python3 pe_scan.py` → `ValueError: not enough values to unpack` — the SAME crash its
  4-bit build produced in F rep3. K2 neon: under the ablations it now WRITES (it was empty at 30 steps) but tests
  `e.key === 'r'` under shift in all 4 writes → never fires. More budget changed whether it wrote, not whether it was right.
- Laguna neon BROKEN = the same lower-cased rename seen at 4-bit: `addEventListener`→`addEventlistener` (and
  `preventDefault`→`preventdefault`) on the EXISTING click/mousemove listeners → TypeError in the constructor.
  4 of its 4 neon writes across both precisions do this — a systematic casing defect in this model, not variance.
- LFM H1 rep1 neon is genuinely REAL (`'R'` on document → wired `restart` → `_startNewGame`, no dispose cleanup: nit).
- Glimmer neon: 0/6 even with double the steps and no compaction — it reads until the budget ends. Not a harness artifact.
- Champion doc-config H1 rep1 THIN is the same CLOB_* invention shape seen in R2 rep2 (variance, now 1 in 3 of its 6-bit
  doc-config misses across 10 runs).

**Verdict:** neither lever converts a challenger. Champion 17/18 and 27B 14/18 are at or above their baseline rates on
these fixtures; every challenger stays below 11/18, and each one's failure mode is the same with or without the levers
(Glimmer never writes neon; K2 wrong key; Laguna casing rename; LFM omits PM_NH_*). The step budget and compaction
are ruled out as the cause of the eliminations — the failures are model behaviour.

## 6-bit eliminations + Glimmer-4bit, reps 2–3 (2026-10-17 04:57 → 07:00) — do the R1 eliminations recur?

Rule: re-open any single-run elimination whose defect does not recur in ≥2 of 3. attrcheck 0 / 240. Graded only
far enough to settle that (a rep with ≥2 non-REAL fixtures cannot reach the 9-REAL bar).

| arm | R1 defect | rep2 | rep3 | recurs? |
|---|---|---|---|---|
| K2-6bit | strict-flag BROKEN (`scan_lmstudio()` TypeError on every bare run) + neon FAIL | strict executed: `--strict` never added (argparse exit 2) — FAIL; neon empty FAIL → ≤8 | strict executed: **the identical TypeError** on a bare run — BROKEN; neon empty; doc-config empty → ≤7 | **yes, 3/3** (strict non-REAL 3/3, BROKEN 2/3; neon FAIL 3/3) |
| LFM-6bit | document-typing BROKEN (`from io import BinaryIO` → ImportError) + neon VACUOUS | **same `from io import BinaryIO`** — BROKEN; neon VACUOUS (keydown on the board div, no tabindex); quickstart FAIL; deps-audit empty → ≤6 | same BinaryIO — BROKEN; neon/neon-doc/shuffle/deps empty, quickstart FAIL → ≤4 | **yes, 3/3** (byte-identical typing break) |
| Laguna-6bit | neon VACUOUS + 1 FAIL | neon empty, doc-config empty → ≤8 | neon empty, doc-config empty → ≤8 | **yes, 3/3** (neon non-REAL 3/3, + `addEventlistener` BROKEN in the ablations) |
| Glimmer-4bit | ≤8 REAL, neon FAIL | neon empty, the-game-document-architecture printed FAIL → ≤8 | same → ≤8 | **yes, 3/3** |

Strict-flag for Laguna/LFM 6-bit exercised correct in both reps (exit 1 under `--strict`, defaults unchanged).
No elimination is re-opened.

## P4e determinism test — warm vs cold oMLX prefix cache (2026-10-17 07:00–07:11)

Champion × nothing-ever-happens-document-config, temp=0. Warm = the shared 50 GB SSD cache as the bench runs it;
cold = a fresh empty `ssd_cache_dir` per rep + oMLX restart (restored to default afterwards; health OK).
diverge.py = first step where (prompt_tokens, completion_tokens, tool calls) differ.

| pair | first divergence | prompt at that step | cached tokens at that step |
|---|---|---|---|
| warm rep1 vs rep2 / rep3 | step 3 | identical (4439) | 3021 vs 4096 — differs |
| warm rep2 vs rep3 | **step 29** | identical (17398) | **identical (17118)** |
| cold rep1 vs rep2 | step 3 | identical (4439) | **identical (3021)** |
| cold rep1 vs rep3 | step 3 | identical (4439) | **identical (3021)** |
| cold rep1 vs warm rep2 | step 6 (only the ORDER of two parallel read_file calls swapped) | identical | identical (5654) |

**Result: the prefix cache is NOT the source of rep-to-rep divergence.** Cold reps, each starting from an empty
cache on a freshly restarted server, diverge from each other at step 3 — as early as warm reps — with byte-identical
prompts and identical cache-hit counts; at step 3 the same input produced 30, 49 or 130 completion tokens. The
server's greedy decode is itself non-deterministic at near-ties (MLX GPU kernels/batching), so no cache setting
makes runs reproducible. The step-3 output also takes only a few discrete forms (30 vs 49 tokens recur across
warm and cold), consistent with a near-tie flipping, not drift.

Consequences: (1) the 3-rep rule stays — variance cannot be engineered out at the cache layer; (2) cold-cache runs
are not needed for harness A/Bs; (3) the earlier "warm cache explains it" hypothesis is retracted.

Bonus grades (envcheck): champion doc-config warm REAL / THIN (CLOB_* invented) / THIN (same) ; cold REAL / THIN
(CHAIN_ID/HOST/SIGNATURE_TYPE invented, FUNDER_ADDRESS missed) / THIN (omits 23 PM_NH_*). All 6 printed PASS.
Champion doc-config across every 6-bit run so far: 9 REAL / 16 (R2 2/4, H1 2/3, H2 3/3, det 2/6). 27B-6bit: 3 / 10.
