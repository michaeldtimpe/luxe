# document-config re-score (2026-11-20)

**Why.** `nothing-ever-happens-document-config` asks for "every environment variable the bot reads
at startup". Its printed PASS only counts UPPER_SNAKE tokens (R1, at least 15), so the real outcome
has always been decided by hand. Different hand-verifies used different reference sets: 23 (the
fixture's own note), 27 (`envcheck.py`, m5max_moe_r2 and tuning_2026_10), 32 (thinkingcap_27b),
57 (my count earlier today), and 55 (ctx_cal_tools_2026_10, validation_context_ab_2026_11).
Coverage numbers were not comparable across reports.

**What changed.**
- `scripts/doc_config_envcheck.py` is the one definition. `derive` walks `bot/**/*.py` at the
  fixture's base_sha (AST: `os.getenv`, `os.environ.get`, `os.environ[...]`, the `_env_*` helpers,
  `RiskConfig.from_env`'s `_f`) and finds **55** names. `scripts/` is excluded: it is not the bot,
  and it holds the only reads of `HEROKU_APP_NAME` and `POLYGONSCAN_API_KEY`, which is why my 57
  was wrong by 2. `score` reports whole-word coverage in a patch's added lines.
- The fixture gains **R4** (`kind: tokens_present`, the 55 names, `min_fraction: 1.0`). Like R1 to R3
  it is observation-only (`spec_validation` never gates PASS), so printed scores stay comparable
  with history. Every bench run now records `R4 N/55 tokens covered; missing ...` in `result.json`.
  The model sees R4 only under `LUXE_REPROMPT_ON_DOC=1` (off by default).
- `tests/test_spec_tokens_present.py` pins the kind, the read shapes, and R4 = the derived set
  (drift guard, runs where the fixture cache exists).

## Re-score

Every document-config patch still on disk: 196 files, 162 distinct after removing `_review`
copies of the same `diff.patch`. Data: `rescore.csv` (path, coverage, `PM_NH_*` count). Per
report and arm (n = distinct patches; identical reps collapse; "-" = empty patch):

| report | arm | n | coverage per distinct patch | empty |
|---|---|---|---|---|
| abort_report_ab_2026_11 | mono__champ | 3 | 32 32 32 | 0 |
| daisyrose_probe_2026_11 | mono__daisyrose-4bit | 1 | - | 1 |
| failed_output_ab_2026_11 | mono__champ | 5 | 32 32 32 32 32 | 0 |
| harness_ab_2026_10 | _pilot_online/codex/champ/rep1 | 1 | 54 | 0 |
| harness_ab_2026_10 | _pilot_online/opencode/champ/rep1 | 1 | 25 | 0 |
| harness_ab_2026_10 | _pilot_online/qwen/champ/rep1 | 1 | - | 1 |
| harness_ab_2026_10 | claude/champ/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | claude/champ/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | claude/champ/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | claude/glim/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | claude/glim/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | claude/glim/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | claude/q38/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | claude/q38/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | claude/q38/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex-cat/champ/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex-cat/champ/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex-cat/champ/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex-cat/glim/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex-cat/glim/rep2 | 1 | - | 1 |
| harness_ab_2026_10 | codex-cat/glim/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex-cat/q38/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex-cat/q38/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex-cat/q38/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex/champ/rep1 | 1 | 54 | 0 |
| harness_ab_2026_10 | codex/champ/rep2 | 1 | 54 | 0 |
| harness_ab_2026_10 | codex/champ/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex/glim/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex/glim/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex/glim/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex/q38/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex/q38/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | codex/q38/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | mcp/_qwen_deferred/champ/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | mcp/claude/champ/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | mcp/claude/champ/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | mcp/claude/champ/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | mcp/qwen/champ/rep1 | 1 | 54 | 0 |
| harness_ab_2026_10 | mcp/qwen/champ/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | mcp/qwen/champ/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | opencode/champ/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | opencode/champ/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | opencode/champ/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | opencode/glim/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | opencode/glim/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | opencode/glim/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | opencode/q38/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | opencode/q38/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | opencode/q38/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | qwen/champ/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | qwen/champ/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | qwen/champ/rep3 | 1 | 55 | 0 |
| harness_ab_2026_10 | qwen/glim/rep1 | 1 | 55 | 0 |
| harness_ab_2026_10 | qwen/glim/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | qwen/glim/rep3 | 1 | 49 | 0 |
| harness_ab_2026_10 | qwen/q38/rep1 | 1 | - | 1 |
| harness_ab_2026_10 | qwen/q38/rep2 | 1 | 55 | 0 |
| harness_ab_2026_10 | qwen/q38/rep3 | 1 | 55 | 0 |
| idle_capped_ab_2026_11 | mono__champ | 9 | 32 32 32 32 32 32 32 32 32 | 0 |
| m5max_moe_r2_2026_10 | mono__k2-horizon-mova-36b-a4b-4bit | 3 | 55 55 | 1 |
| m5max_moe_r2_2026_10 | mono__k2-horizon-mova-36b-a4b-6bit | 5 | 55 55 54 55 | 1 |
| m5max_moe_r2_2026_10 | mono__k2-horizon-mova-36b-a4b-6bit__ms60 | 3 | 55 54 55 | 0 |
| m5max_moe_r2_2026_10 | mono__laguna-xs-2.1-4bit | 2 | 26 | 1 |
| m5max_moe_r2_2026_10 | mono__laguna-xs-2.1-6bit | 2 | 55 | 1 |
| m5max_moe_r2_2026_10 | mono__laguna-xs-2.1-6bit__ms60 | 1 | - | 1 |
| m5max_moe_r2_2026_10 | mono__lfm2.5-24b-a2b-4bit | 3 | 47 43 53 | 0 |
| m5max_moe_r2_2026_10 | mono__lfm2.5-24b-a2b-6bit | 1 | 24 | 0 |
| m5max_moe_r2_2026_10 | mono__lfm2.5-24b-a2b-6bit__ms60 | 1 | 24 | 0 |
| m5max_moe_r2_2026_10 | mono__muse-glimmer-30b-4bit | 2 | 55 | 1 |
| m5max_moe_r2_2026_10 | mono__muse-glimmer-30b-6bit | 5 | 55 55 55 55 | 1 |
| m5max_moe_r2_2026_10 | mono__muse-glimmer-30b-6bit__ms60 | 2 | 55 | 1 |
| m5max_moe_r2_2026_10 | mono__qwen3.6-27b-4bit-ref | 3 | 55 55 54 | 0 |
| m5max_moe_r2_2026_10 | mono__qwen3.6-27b-6bit-ref | 7 | 55 32 32 32 32 55 32 | 0 |
| m5max_moe_r2_2026_10 | mono__qwen3.6-27b-6bit-ref__ms60 | 3 | 54 32 32 | 0 |
| m5max_moe_r2_2026_10 | mono__qwen3.6-35b-a3b-4bit-ref | 3 | 31 16 30 | 0 |
| m5max_moe_r2_2026_10 | mono__qwen3.6-35b-a3b-6bit-ref | 12 | 55 52 32 52 54 54 55 55 55 54 55 55 | 0 |
| m5max_moe_r2_2026_10 | mono__qwen3.6-35b-a3b-6bit-ref__ms60 | 3 | 54 55 55 | 0 |
| splash_engine_ab_2026_09 | omlx4/opencode_rep1 | 1 | 55 | 0 |
| splash_engine_ab_2026_09 | omlx4/opencode_rep2 | 1 | 55 | 0 |
| splash_engine_ab_2026_09 | splash4/opencode_rep1 | 1 | 55 | 0 |
| splash_engine_ab_2026_09 | splash4/opencode_rep2 | 1 | 55 | 0 |
| thinkingcap_27b_2026_10 | mono__qwen3.6-27b-6bit-ref | 3 | 55 43 50 | 0 |
| thinkingcap_27b_2026_10 | mono__thinkingcap-27b-6bit | 3 | 32 32 55 | 0 |
| thinkingcap_4bit_2026_11 | mono__ref4 | 3 | 55 55 49 | 0 |
| thinkingcap_4bit_2026_11 | mono__tc4 | 3 | 32 32 31 | 0 |
| tuning_2026_10 | mono__muse-glimmer-30b-6bit | 3 | 55 55 | 1 |
| tuning_2026_10 | mono__muse-glimmer-30b-6bit__sys-commit_first | 1 | - | 1 |
| tuning_2026_10 | mono__qwen3.6-35b-a3b-6bit | 3 | 53 54 53 | 0 |
| tuning_2026_10 | mono__qwen3.8-27b-8bit | 3 | 55 32 | 1 |
| tuning_2026_10 | mono__qwen3.8-27b-8bit__sys-commit_first | 2 | 55 | 1 |

## Where a recorded verdict changes

| report | recorded | re-scored | change |
|---|---|---|---|
| thinkingcap_27b_2026_10 (HAND-VERIFY, local only) | ThinkingCap "32/32 every rep, beats the reference" | TC 32 / 32 / 55, ref 55 / 43 / 50 | TC reps 1 to 2 THIN (28 REAL + 2 THIN, not 30/30); the reference led. Already noted there 2026-11-20 (with my 57-based numbers; superseded by these) |
| thinkingcap_4bit_2026_11 | tc4 34 / 34 / 31 of 57, ref4 55 / 57 / 50 of 57 | tc4 32 / 32 / 31 of 55, ref4 55 / 55 / 49 of 55 | Counts only; verdicts (tc4 THIN x3, ref4 REAL) unchanged |
| failed_output_ab_2026_11 | 32 / 57 | 32 / 55 | Counts only |
| m5max_moe_r2_2026_10 | "27/27" with `PM_NH_*` omission judged by hand | consistent | None. The hand verdicts already marked every `PM_NH_*` omission THIN; only the denominator differs |
| tuning_2026_10 | "envcheck 27/27 REAL" | 55 / 55 for those runs | None |
| ctx_cal_tools_2026_10, validation_context_ab_2026_11 | 55-name set already | same set | None |

## Observation: the champion's coverage in luxe's own loop

| when | report | champion coverage (of 55) |
|---|---|---|
| 2026-10-16 to 17 | m5max_moe_r2 (ref arm) | 55, 52, 32, 52, 54, 54, 55, 55, 55, 54, 55, 55 (+ms60: 54, 55, 55) |
| 2026-10-18 | tuning_2026_10 | 53, 54, 53 |
| 2026-10-20 | ctx_cal_tools (hand, 55-set) | 3 REAL, 3 THIN (fabricated vars) of 6 |
| 2026-11-03 | validation_context (hand, 55-set) | mixed REAL / THIN / DAMAGING |
| 2026-11-19 to 20 | idle_capped, abort_report, failed_output | **32 on 21 of 21 runs**, every one missing all 23 `PM_NH_*` |

The same model driven by other harnesses (harness_ab_2026_10: Claude Code, Codex, OpenCode, Qwen
Code) scores 54 to 55 almost every run. This fixture has always varied run to run. Since
2026-11-19 the champion takes the incomplete path deterministically, which may be the variance
settling on one branch under the current substrate, or a harness change between 11-03 and 11-19.
Not diagnosed. The queued idle re-run (`../idle_wide_rerun_2026_11/`) adds 6 champion samples,
and R4 now records it on every run.
