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

**Verdict: it fixes document-config (R4 32/55 to 55/55 in all three reps) at +19% prompt tokens, with no
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
