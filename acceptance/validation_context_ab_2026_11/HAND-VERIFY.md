# Hand-verify: validation_context_ab_2026_11

Every run was classified, not just the printed passes. Runs were grouped by
fixture and `git patch-id`; each unique patch was read against its goal text
("would a competent engineer accept this?"). Base files were checked on m5
with `git show <base_sha>:<path>` wherever a claim depended on them.
Verdicts: `HAND-VERIFY-champ.json` (48 patches, 120 runs) and
`HAND-VERIFY-gemma.json` (26 patches, 60 runs). The first pass was done by a
review agent; its DAMAGING and FAIL calls were re-checked by hand (see below).

Classes: REAL (does the goal, correct), THIN (on target, materially
under-delivers), VACUOUS (passes without the work), FAIL (does not do the
goal), DAMAGING (repo worse than base, with evidence).

## Champion, per run

| Arm | REAL | THIN | FAIL | DAMAGING |
|---|---|---|---|---|
| ctl | 28 | 1 | 0 | 1 |
| menu | 25 | 5 | 0 | 0 |
| pack | 28 | 1 | 1 | 0 |
| both | 25 | 4 | 0 | 1 |

| Fixture | ctl | menu | pack | both |
|---|---|---|---|---|
| isomer-document-quickstart | REAL x3 | THIN x3 | REAL x3 | THIN x3 |
| isomer-implement-healthcheck | REAL x3 | REAL x3 | REAL x3 | REAL x3 |
| lpe-rope-calc-document-typing | REAL x3 | REAL x3 | REAL x3 | REAL x3 |
| lpe-rope-calc-implement-strict-flag | REAL x3 | REAL x3 | REAL x2, FAIL x1 | REAL x3 |
| neon-rain-document-modules | REAL x3 | REAL x3 | REAL x3 | REAL x3 |
| neon-rain-implement-reset-shortcut | REAL x3 | REAL x3 | REAL x3 | REAL x3 |
| nothing-ever-happens-document-config | REAL, THIN, DAMAGING | REAL, THIN, THIN | REAL, REAL, THIN | REAL, THIN, DAMAGING |
| nothing-ever-happens-manage-deps-audit | REAL x3 | REAL x3 | REAL x3 | REAL x3 |
| the-game-document-architecture | REAL x3 | REAL x3 | REAL x3 | REAL x3 |
| the-game-implement-shuffle-shortcut | REAL x3 | REAL x3 | REAL x3 | REAL x3 |

Evidence for non-REAL champion calls:

- isomer quickstart, menu and both (THIN): 2 to 3 changed comment lines in
  the Quick Start ("see docker-compose.yml", the port from app.py); ctl and
  pack restructure it into steps and keep every setup line.
- document-config THIN: omits all 23 to 25 `PM_NH_*` strategy variables, or
  (both r2) `PRIVATE_KEY`, `FUNDER_ADDRESS`, `LIVE_TRADING_ENABLED`, `DRY_RUN`;
  55 startup variables exist at base.
- document-config DAMAGING, ctl r1: `PM_RISK_MAX_TOTAL_OPEN_EXPOSURE_USD`
  documented as `10000.0` and `PM_RISK_KILL_COOLDOWN_SEC` as `300.0`; base
  `bot/risk_controls.py:22,25` has `1_500.0` and `900.0` (re-checked by hand).
- document-config DAMAGING, both r1: documents `PM_NH_FIXED_TRADE_AMOUNT`
  (base reads `PM_NH_FIXED_TRADE_AMOUNT_USD`, `bot/config.py:223`) and an env
  var the code reads from the config file instead.
- strict-flag FAIL, pack r3 (step-limited): `bad_files = [] if args.strict
  else None`, then `(bad_files or []).append(path)`; an empty list is falsy,
  so failures go to a throwaway list and `--strict` never exits non-zero
  (re-checked by hand).

## Gemma, per run

| Arm | REAL | THIN | DAMAGING | worse than base (printed PASS) |
|---|---|---|---|---|
| ctl | 10 | 8 | 12 | 12 (9) |
| menu | 5 | 16 | 9 | 9 (9) |

DAMAGING evidence (patch dir in the verdict file):

- 003 healthcheck ctl: commits a collected `test_health.py` that imports
  `requests` (not in requirements) and starts `app.py` without the required
  `ISOMER_SECRET`.
- 007 strict-flag ctl: `scan_hf_cache` now returns a tuple but the default
  path still does `records += scan_hf_cache(root)`; dedupe raises
  `AttributeError` on every default run.
- 008 strict-flag menu: `ap.add.argument("--no-default-paths", ...)` crashes
  at startup (re-checked by hand).
- 010 document-modules ctl: every added line prefixed with a line number
  (`+1\t# Architecture` ... `+132\t`); no Subdirectories section.
- 012 reset-shortcut menu: emits `'__restart'`, which nothing listens for,
  and replaces the Restart button's `showConfirm(...)` with a direct
  `_startNewGame()`.
- 021 architecture ctl: deletes the existing `## Features` section.
- 025 shuffle-shortcut menu: deletes the mount effect's `shuffle();` and the
  splash `setTimeout`, so the app never leaves the splash screen
  (re-checked by hand).

Gemma's Checks-section fabrication (7/30 menu runs claiming `git_diff` ran
with zero `git_diff` calls) is counted in `REPORT.md`, from `runs.json`
`false_claims` plus a hand read of each flagged section.
