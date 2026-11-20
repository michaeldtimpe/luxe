# ThinkingCap 4-bit vs Qwen3.6-27B-4bit (2026-11-19, hand-verified 2026-11-20)

**Question:** is `ThinkingCap-Qwen3.6-27B-OptiQ-4bit` (tc4) a better m1 `luxe code`
model than the incumbent `Qwen3.6-27B-4bit` (ref4, m1's `code_main`)? This is the
4-bit follow-up the 6-bit report (`../thinkingcap_27b_2026_10/HAND-VERIFY.md`) said was
untested.

**Run:** m5, maintain_suite, 10 fixtures x 3 reps x 2 arms, models interleaved per rep,
shipped defaults, `--work-dir ~/.luxe/bench-workspace`, driver `run_models.sh`. Started
2026-11-19 12:51, `DONE` 15:42 (`arms.log`), every arm rc=0, no stalls.

## Verdict

**Faster, not better.** tc4 used 40% less wall and about half the output tokens. Hand-read
outcome quality is a wash with a lean against tc4: it is complete on one fixture where
ref4 is not, and incomplete on another where ref4 is nearly complete.

| | ref4 | tc4 |
|---|---|---|
| printed pass | 30/30 | 30/30 |
| hand-verified REAL | 27/27 read (deps-audit not read); strict-flag x3 with a gap | 27/30; strict-flag x3 with a gap |
| THIN | 0 | 3 (document-config, all reps) |
| step-limit (max steps 30) | 3 (strict-flag, every rep; draft PR) | 0 |
| wall, sum of 3 reps | 105.9 min | 63.3 min (-40%) |
| completion tokens, 3 reps | 165,587 | 85,493 (-48%) |
| prompt tokens, 3 reps | 4,212,612 | 2,910,646 (-31%) |
| gen tok/s | ~26 | ~22.4 |
| weights on disk | 15 GB | 19 GB |

Sources: per-run `diagnostics.json` and `diff.patch` under `tc4/` and `ref4/`;
`gen tok/s` from the comparison rows in `tc4.log` / `ref4.log`.

**Determinism:** both arms are fully deterministic except on document-config. Token counts
and diffs are identical across the 3 reps on every other fixture. So this is effectively
n=1 per fixture plus 3 samples of document-config.

**Engine:** both models carry `vision_config` and the same architecture
(`Qwen3_5ForConditionalGeneration`), so the 6-bit report's engine confound (ref on vlm,
ThinkingCap on llm) does not apply here. Checked from config.json, not from server logs.

**Weights:** OptiQ is a mixed-precision quant (4-bit base, 498 per-layer overrides). It is
4 GB larger and decodes ~14% slower. The whole wall win comes from generating fewer
tokens, and 31% fewer prompt tokens is the part that should carry over to m1, where the
dense 27B is prefill-bound.

## Per fixture

Read by hand against the fixture goal and the base source. Diffs are per run
(`base_sha_used..head_sha_used`, written by the harness), so attribution needs no reflog
reconstruction.

| Fixture | tc4 | ref4 | Evidence |
|---|---|---|---|
| isomer-implement-healthcheck | REAL | REAL | Identical route; `jsonify` already imported. |
| the-game-implement-shuffle-shortcut | REAL (nit) | REAL | tc4 binds once with `[]` deps; `shuffle` uses only setters + fetch, so the stale closure is safe. The repo has no inputs at all. Nit: Cmd/Ctrl+R also fires a shuffle before the page reloads. ref4 uses a ref and guards SELECT too. |
| neon-rain-implement-reset-shortcut | REAL | REAL | tc4 emits `action:request {type:'__restart'}`, the path ActionPanel already uses (`Game.js:291`); listener removed in `dispose()`. ref4 adds a `game:reset` event that duplicates the same confirm + `_startNewGame()`. |
| lpe-rope-calc-implement-strict-flag | REAL (gap) | REAL (gap), step-limit | Executed, see below. tc4 catches 3 of 4 failure classes; ref4 catches 2, and its unused `magic = f.read(4)` hunk is a half-applied edit consistent with hitting max steps. |
| lpe-rope-calc-document-typing | REAL | REAL | Byte-identical diff in both arms: `f: BinaryIO`. |
| isomer-document-quickstart | REAL | REAL | Port 27001 (`app.py:1138`), `127.0.0.1:27001` binding (compose line 13); .env/ISOMER_SECRET steps preserved. |
| neon-rain-document-modules | REAL | REAL | All 9 subdirs, names verified against `src/`, EventBus mentioned. The "planning, job, cleanup" lifecycle repeats the existing ARCHITECTURE.md line 15. |
| the-game-document-architecture | REAL | REAL | `selectWithNeighbors` (server.js:103), random slot in a 6-group, `selectedIndex`, 4 studio lists, `tmdbCache` all verified. |
| nothing-ever-happens-manage-deps-audit | REAL (nit) | not read | All 7 cited GHSA/CVE ids and fixed versions re-checked against OSV on 2026-11-20 (3.9.2, 3.9.4, 3.13.4 x2, 3.14.1, web3 7.15.0, dotenv 1.2.2). requirements.txt untouched. Nit: the header date "2025-07-09" is invented, the same date the 6-bit run invented. |
| nothing-ever-happens-document-config | THIN x3 | REAL x3 (2 with small gaps) | Coverage of the 57 env vars the bot reads at startup: tc4 34 / 34 / 31; ref4 55 / 57 / 50. Every tc4 rep omits all 23 `PM_NH_*` strategy overrides. |

### strict-flag, executed

Each arm's rep1 patch was applied to the base `pe_scan.py` and run on five crafted files
(`hv/strict_probe.py` recreates them). Values are exit codes; "default" is without
`--strict`.

| Case | base | tc4 --strict | ref4 --strict | tc4 default |
|---|---|---|---|---|
| good GGUF | 0 | 0 | 0 | 0 |
| bad magic | 0 | **1** | 0 | 0 |
| version 1 | 0 | **1** | 0 | 0 |
| header cut at 10 bytes | 0 | **1** | **1** | 0 |
| value truncated mid-entry | 0 | 0 | **1** | 0 |

Neither arm is complete. tc4 misses the inner `except Exception: return meta` path, which
returns partial metadata as success. ref4 misses non-GGUF and old-version headers. Both keep
the default exit 0. I'd rank tc4's coverage higher (a bad-magic `.gguf` is the clearest case
of missing header metadata), but neither is fully REAL against the goal text.

### document-config, the 57-var set

`hv/document-config-envvars-57.txt` holds every quoted env name read under `bot/`
(`_env_*`, `os.getenv`, `RiskConfig.from_env`'s `_f`, and the multi-line
`_env_int("PM_NH_...", ...)` calls). `scripts/wallet_history.py`'s `TARGET_WALLET` and
`WALLET_ADDRESS` are excluded because they aren't read by the bot. The `PM_NH_*` overrides are
read at startup: `bot/main.py:125` calls `load_nothing_happens_config()`, which builds
`NothingHappensConfig` from `_env_*("PM_NH_...")` (config.py:188 onward). Every name any
of the six docs mentions is in the set, so neither arm invented a variable.

## Correction to the 6-bit report

`../thinkingcap_27b_2026_10/HAND-VERIFY.md` says ThinkingCap-6bit covered "32/32 env vars
in all 3 reps vs the reference's 32/30/27" and "beats it on one (`document-config`)".
That count used a narrow regex that misses the multi-line `PM_NH_*` and `PM_RISK_*` reads.
Re-scored against the 57-var set from that run's own `r1/*/diff.patch`:

| | rep1 | rep2 | rep3 |
|---|---|---|---|
| ThinkingCap 6-bit | 34 | 34 | 55 |
| Qwen3.6-27B-6bit ref | 57 | 43 | 50 |

Caveat (added 2026-11-20 pm): other reports scored this fixture with an `envcheck.py`
against a 27-var set (tuning_2026_10, m5max_moe_r2_2026_10, which names one `PM_NH_*` var
as a miss). That script is no longer on disk and its definition can't be reconstructed (it is
not `.env.example`, which has 12). The 57 here is the literal goal text traced through the
code. Document-config coverage numbers are therefore not comparable across reports. The
champion's own docs in `../failed_output_ab_2026_11/` cover 32/57 on every rep in both arms.

So the 6-bit reference, not ThinkingCap, was ahead on document-config, and two of
ThinkingCap's three reps were THIN. The 6-bit "30/30 REAL" becomes 28 REAL + 2 THIN.
No other 6-bit verdict is affected. The finding is the same in both quantizations:
**ThinkingCap tends to stop documenting at the `PM_NH_*` block.** Shorter thinking
appears to cost coverage on the one enumerate-everything fixture.

## Equal on both arms (not counted)

- `the-game-*`: `npm test` fails at base because package.json has no `test` script, so
  both arms open a draft PR there. That is the fixture environment, not either model.
- No `gates_triggered` on any of the 60 runs.

## Recommendation

Not a clear win, so **m1's manifest stays unchanged** pending a decision. What a switch
would buy and cost on m1:

- for: ~31% less prefill and ~48% less decode per task; on m1 the 27B-4bit is the slow
  path (prefill-bound), so this is the most likely benefit to carry over.
- against: 4 GB more weights (19 vs 15 GB) inside m1's ~36 GB wired budget next to the
  MoE main, and a reproducible coverage gap on enumerate-everything tasks (3/3 here, 2/3 at 6-bit).
- not yet measured: m1 wall itself (the planned m1 slice).

## Re-score note (2026-11-20, later the same day)

The 57-name set above over-counted by 2 (`HEROKU_APP_NAME`, `POLYGONSCAN_API_KEY` are read only in
`scripts/`, not by the bot). Against the canonical 55: tc4 32 / 32 / 31, ref4 55 / 55 / 49; 6-bit
ThinkingCap 32 / 32 / 55, 6-bit reference 55 / 43 / 50; the champion 32. No verdict changes.
See `../doc_config_rescore_2026_11/REPORT.md` (canonical 55-name set from `scripts/doc_config_envcheck.py`; bench requirement R4 records it on every run since then).
