# Champion on oMLX 0.8.0: re-baseline + oversize-window arm (2026-11-20, m5)

**Why.** `../doc_config_rescore_2026_11/DIAGNOSIS.md` found that oMLX 0.8.0 (installed 2026-11-18)
changes temp-0 trajectories, so pre-11-18 baselines are not comparable with today's runs. This
run sets the 0.8.0 baseline (`ctl`, shipped defaults) and tests the cheapest fix for the
document-config gap that diagnosis traced: `ow` = `LUXE_READ_OVERSIZE_WINDOW=1` (opt-in since
2026-11-08; an over-cap whole-file read returns an outline + first window instead of a refusal).

**Run.** Champion `Qwen3.6-35B-A3B-6bit`, oMLX 0.8.0, bench at c2ac86a (records the R4 doc-config
coverage requirement), 10 fixtures x 3 reps interleaved, 15:13 to 15:45, every arm rc=0. Warm-up
passed. `analyze.py champ` (`runs.json`), R4 from each `result.json`, window firings counted in
saved messages.

## A. The 0.8.0 baseline (ctl)

| | 0.7.0 control, 2026-10-20 (`../ctx_cal_tools_2026_10/REPORT.md`) | 0.8.0 control, today |
|---|---|---|
| printed pass | 30/30 | 30/30 |
| wall, sum of 3 reps | 1,303 s | **892 s (-32%)** |
| prompt tokens, sum | 3.78 M | 2.99 M (-21%) |
| hand-verified REAL | 25/30 (3 neon-modules, 2 doc-config) | **27/30** (doc-config THIN x3) |
| document-config coverage (R4) | mixed, 3 REAL / 3 THIN | 32/55 x3 (deterministic) |

Shipped code also changed between the two dates (abort report, bench python PATH, cleanup turn
details), so this is a new reference point, not a clean server A/B. From here on, compare against
this row, not October.

## B. Oversize window (ow) vs ctl

**Correction (2026-11-20, evening): the confirmation in section C did not reproduce this. document-config
was 31, 29 and 28 of 55 under ow there, below ctl's 32/55. Read section C before citing the verdict below.**

**Original verdict: it fixes document-config (R4 32/55 to 55/55 in all three reps) at +19% prompt tokens, with no
REAL lost elsewhere (29 vs 27).** One fixture changed in a worse direction (neon rep 1; see below),
which is within this suite's run-to-run variance. Recommendation: promote for maintain/chat only
after a second 3-rep confirmation; the SWE-bench n=75 x 3 result (34.7 vs 35.7) still argues against
turning it on for the SWE-bench path.

| | ctl | ow |
|---|---|---|
| printed pass | 30/30 | 30/30 |
| hand-verified REAL | 27 | **29** |
| document-config R4 | 32/55 x3 | **55/55 x3** |
| wall, sum | 892 s | 892 s (matched-21 runs: +18.8%) |
| prompt tokens | 2.99 M | 3.55 M (+19%) |
| compaction fires | 70 | 84 |
| window served (saved messages) | 0 | 13 (document-typing 6, quickstart 3, strict-flag 3, neon 1); document-config's firing is absent from the log because compaction truncated that message, but R4's 55/55 shows the effect |

### Hand verification (fixtures whose patch differs between arms)

- **nothing-ever-happens-document-config.** ow: one patch x3, 55/55, nothing invented
  (`POSTGRES_PASSWORD` from docker-compose is a nit an earlier report also accepted; `DEBUG`/`WARNING` are
  LOG_LEVEL values). Spot-checked defaults match base: `PM_NH_MARKET_REFRESH_INTERVAL_SEC` 600,
  `PM_NH_FIXED_TRADE_AMOUNT_USD` 0.0, `PM_NH_MAX_ENTRY_PRICE` 0.65, `PM_RISK_MAX_TOTAL_OPEN_EXPOSURE_USD` 1500.0,
  `PM_RISK_KILL_COOLDOWN_SEC` 900.0. **REAL x3.** ctl: 32/55 x3, **THIN x3**.
- **lpe-rope-calc-document-typing.** ctl `IO[bytes]` plus tightened return types; ow `BinaryIO` on the one
  untyped parameter. Both REAL.
- **lpe-rope-calc-implement-strict-flag.** Executed with `../thinkingcap_4bit_2026_11/hv/strict_probe.py`:
  identical behavior in both arms (`--strict` exits 1 on bad magic, old version, short header; 0 on a truncated
  value; default exit 0). Only `pe_scan.py` changed, no litter. REAL (same gap) in both.
- **isomer-document-quickstart.** Equivalent comment rewrites naming docker-compose.yml, port 27001 from
  app.py, loopback binding. Both REAL.
- **neon-rain-implement-reset-shortcut.** ctl r1/r3 (`a1236be6b9`) and r2 route Shift+R through
  `HtmlInputHandler` with listener removal: REAL. **ow r1 is THIN:** it adds the listener directly in
  `Game.js`, never touches `src/input/` (the goal requires it) and never removes it. ow r2/r3 = `a1236be6b9`, REAL.
- The other 5 fixtures produced the same patch in both arms (REAL).

Totals: ctl 30 - 3 = **27**; ow 30 - 1 = **29**.

## C. Confirmation rep (2026-11-20, 20:28 to 21:03)

**Run.** Same arms, same model and server (oMLX 0.8.0), bench at db72a33 (adds the cleanup-turn data
candidates and parser fixes, #65 to #68, and SWE-bench image exec, which maintain never sets). Label `conf`,
3 reps interleaved, every arm rc=0, no warm-up run (ctl rep 1 was not slow: 316 s vs 426 to 436 s for reps
2 and 3). `analyze.py conf` (output saved as `runs_conf.json`; `runs.json` is still the `champ` run).
R4 is from each `result.json`, cross-checked with `scripts/doc_config_envcheck.py score`.

**Verdict: not confirmed. The window does not fix document-config; keep it opt-in.** The first run's
55/55 x3 was one trajectory repeated three times (byte-identical reps), not a property of the lever.

| | ctl | ow |
|---|---|---|
| printed pass | 30/30 | 30/30 |
| hand-verified REAL | 26 | 27 |
| document-config R4 | 32, 32, 32 of 55 | **31, 29, 28 of 55** |
| window served (saved messages) | 0 | 3, 3, 4 per document-config run |
| wall, sum | 1,178 s | 851 s (matched-21 runs: +9.8%) |
| prompt tokens | 3.48 M | 3.25 M (matched: +1.4%) |
| compaction fires | 72 | 70 |

### Why the first run looked like a fix

Both runs serve the window on the first `read_file bot/config.py` (14,483 B against the 13,653 B cap).
What decides R4 is what the model does next:

- `champ` ow (55/55): it paged `bot/config.py` with `offset=` 100, 200 and 300, reaching the
  `_env_int("PM_NH_...")` block at line 200 onward.
- `conf` ow (28 to 31): it read only lines 1 to 50 of `bot/config.py` again, then built the list from
  `grep os.getenv`. That grep cannot see the `_env_int`/`_env_float`/`_f` readers, which are exactly the
  23 missing `PM_NH_*` names. Reps 2 and 3 also dropped `DRY_RUN`, `LIVE_TRADING_ENABLED` and
  `FUNDER_ADDRESS`, which ctl kept.

So the document-config gap is model behavior: the champion takes grep for the os.getenv pattern as the
full inventory. The read path is not what limits it, which agrees with the 2026-11-08 SWE-bench verdict.

### Run-to-run variation went up

Patches that differ across the three reps of one arm: 3 of 20 arm-fixture cells in `champ`
(healthcheck ow, neon-reset ctl and ow), 6 of 20 in `conf` (the same three plus healthcheck ctl and
document-config in both arms). In `champ`, document-config was byte-identical x3 in both arms, which is
why its 55/55 looked solid. Same server, weights and arm settings, so substrate state (for example the
prefix cache after hours of uptime) is the likely source; this run did not isolate it. In practice, an
arm whose reps came out identical counts as n=1 on that fixture, and a lever needs two independent
sessions before promotion.

### Hand verification (patches that differ from section B)

- **document-config.** All six THIN on R4; no invented variables (score output lists only missing names).
- **neon-rain-implement-reset-shortcut.** ctl r1 (emits `game:restart`, handled with the existing
  confirm modal) REAL; ctl r3 (emits `game:start` with no data; base `Game.js:292` already calls
  `_startNewGame()` this way) REAL; **ctl r2 THIN**: listener added directly in `Game.js`, never touches
  `src/input/` and never removes it, which is the same defect ow r1 had in section B, here on the control
  side. ow r1 to r3 route through `HtmlInputHandler` with removal: REAL.
- **lpe-rope-calc-implement-strict-flag.** New patches in both arms (`7daa217e67` ctl, `9b027a35e6` ow),
  run with `../thinkingcap_4bit_2026_11/hv/strict_probe.py`: `--strict` exits 1 on bad magic, old version
  and short header, 0 on a truncated value, default exit 0. Only `pe_scan.py` changed. REAL (same gap)
  in both.
- **isomer-implement-healthcheck.** The route is identical in all six. `b56cdae8e3` (ctl r2/r3, ow r2/r3)
  is the known patch from `../abort_report_ab_2026_11/HAND-VERIFY.json` (REAL, commits a scratch
  `_test_health.py`); ctl r1 commits the same file empty. ow r1 is clean. All REAL; the litter is a
  separate gap (below).
- **isomer-document-quickstart, lpe-rope-calc-document-typing.** Equivalent rewrites of the section B
  patches (compose file, port 27001 from app.py, loopback; `IO[bytes]` vs `BinaryIO`). REAL.
- The other four fixtures produced the same patch in both arms. REAL.

Totals: ctl 30 - 3 (document-config) - 1 (neon r2) = **26**; ow 30 - 3 = **27**.

### Across both sessions

| | ctl | ow |
|---|---|---|
| hand-verified REAL, 60 runs | 53 | 56 |
| document-config REAL | 0/6 | 3/6 (all from one trajectory) |
| neon-reset THIN | 1/6 | 1/6 |

The REAL difference comes entirely from document-config in the first session. No promotion for
maintain/chat; the oversize window stays opt-in.

### Gap found on the way (not caused by the window)

`_test_health.py` is committed in 10 of the 12 healthcheck runs across both sessions and both arms, and it is never offered to
the cleanup turn: `cleanup.find_candidates` skips code files whose stem starts with `_`, and so does the
`orphan_file` gate (`benchmarks/maintain_suite/gates.py:685`). The exemption exists for `__init__.py`-style
files, but it also lets `_scratch.py` through. A fix would narrow the exemption to dunder names in both
places; not done here.
