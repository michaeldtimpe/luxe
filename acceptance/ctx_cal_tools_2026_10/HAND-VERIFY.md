# Hand-verify — LUXE_CTX_CAL_TOOLS no-harm (2026-10-20)

The diffs were dumped per run from each bench clone's reflog to
`m5:~/luxe-bench-tmp/calt/diffs/<fixture>.<arm><rep>.diff`. Code was run in
`m5:~/luxe-bench-tmp/hv_calt/`.

Nine fixtures produced one diff, shared by all six runs. Each verdict applies to both arms.

| fixture | verdict | evidence |
|---|---|---|
| isomer-document-quickstart | REAL | README gains the compose/port explanation; the port 27001 loopback binding is correct |
| isomer-implement-healthcheck | REAL | `@app.route("/health")` → `jsonify({"status": "ok"}), 200` on the existing app |
| lpe-rope-calc-document-typing | REAL | `IO[bytes]` on `_read_gguf_value`, `dict[str, Any]` on `_pe_from_gguf`; import added |
| lpe-rope-calc-implement-strict-flag | REAL | **ran it**: a dir holding a truncated v3 header, a v1 header and an empty `.gguf` → no flag rc 0, `--strict` rc 1 listing the two GGUF-magic files. Nits: an empty/non-magic `.gguf` is not counted as bad; a dead duplicate `return 0` |
| neon-rain-document-modules | THIN | the subdirectory section is accurate for the tree except `phases/ — ... (draw → planning → job → cleanup)`: `PHASE` in `src/constants.js` is draw/planning/job only. This is the stale-ARCHITECTURE.md "cleanup phase" from harness_ab_2026_10 |
| neon-rain-implement-reset-shortcut | REAL | Shift+R → `game:restart` → `_startNewGame()`, the same call the Restart button makes; the old handler is disposed per game, so listeners don't stack; tests pass |
| nothing-ever-happens-manage-deps-audit | REAL | online `cve_lookup` results; three upgrades with a fixed version and rationale; other packages listed as clean |
| the-game-document-architecture | REAL | README "How it Works" matches `shuffle()` → `/api/shuffle` → `selectWithNeighbors` |
| the-game-implement-shuffle-shortcut | REAL | `r` keydown → `shuffle()`, skipping inputs and contentEditable; listener cleaned up |

The neon-rain-document-modules THIN is the same diff in both arms, so it pre-dates this
lever.

## nothing-ever-happens-document-config — six distinct CONFIG.md files

Each file was checked against the names the code actually reads (`os.getenv` /
`_env_*` / `_f` at the parent commit).

| run | verdict | notes |
|---|---|---|
| ctl1 | REAL | omits only script-only vars (`HEROKU_APP_NAME`, `POLYGONSCAN_API_KEY`) |
| ctl2 | THIN | invents `CLOB_API_KEY`, `CLOB_API_KEY_ID`, `CLOB_API_PASSPHRASE`, `CLOB_API_SECRET`, `CLOB_CHAIN_ID`, `HOST`, `SIGNATURE_TYPE`, plus `FUND_ADDRESS` (the code reads `FUNDER_ADDRESS`), with fabricated `bot/config.py:1xx` citations |
| ctl3 | THIN | same fabrication as ctl2 |
| cal1 | THIN | same, plus `POLYGON_CHAIN_ID`/`POLYGON_NETWORK`; omits `PAPER_STATE_PATH` |
| cal2 | REAL | as ctl1 |
| cal3 | REAL | as ctl1 |

The two shapes appear 3 and 3 across the arms (ctl 1R/2T, cal 2R/1T). This is the fixture's
existing variance, not a lever effect.

**Totals:** ctl **25/30 REAL** (30 − 3 neon-modules − 2 doc-config), cal **26/30 REAL**
(30 − 3 − 1). Every not-REAL is THIN. None is FAIL, DAMAGING or VACUOUS.

## Glimmer neon-reset ×3

- **calt1 — REAL.** Shift+R emits `action:request {type: '__restart'}`, which goes through
  the existing meta-action handler (confirm, then `_startNewGame()`). `npm test` passes: all
  personas 3/3. Stopped at step 30 and was labelled incomplete, although the edit is
  complete.
- **calt2, calt3 — FAIL.** No write in 30 steps.
