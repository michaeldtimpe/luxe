# LUXE_FAILED_OUTPUT, Part 1: maintain_suite no-harm (2026-11-20, m5)

**Verdict: no harm, and unexercised. Part 1 cannot say anything about efficacy.** In 60 runs no
bash call exited non-zero, so the flag never had output to show. Efficacy moves to the SWE-bench
slice, now the 3-arm A/B in `../swebench/image_exec_2026_11/` (control, fo, fo + image exec),
which supersedes this PLAN's Part 2.

Run: champion `Qwen3.6-35B-A3B-6bit`, 10 fixtures x 3 reps, arms `ctl` (shipped) and `fo`
(`LUXE_FAILED_OUTPUT=1`), interleaved, bench worktree at `9b48595` (PR #61 landed). Warm-up
`isomer-implement-healthcheck` passed (its rc=1 is the runner's v1-release exit with one
fixture). `arms.log`: 09:33 to 10:04, every arm rc=0. Analysis: `analyze.py champ` (`runs.json`).

## Gates

| gate | threshold | result |
|---|---|---|
| printed pass, fo | 30/30 | **30/30** (ctl 30/30) |
| hand-verified REAL | fo >= ctl | equal (see below) |
| new DAMAGING | 0 | 0 |
| overall wall | fo <= +15% vs ctl | **-8.8%** (853 s vs 935 s); matched 20/30 -8.2% |
| `fo_shown` in ctl (liveness) | 0 | 0 |
| `fo_shown` in fo | reported | **0 in 30 runs** |

## Why the arm is unexercised (checked, not assumed)

- The env reached the arm: `env:LUXE_FAILED_OUTPUT` is in `config_fp_fields` of 30/30 fo states
  and 0/30 ctl states.
- What the model saw (saved messages, 513 tool messages in fo): no tool result starts with
  `exit code` in either arm. Every bash failure was a validator refusal (fo: 24 chain/redirect,
  6 not allowlisted), which carries no output and is outside the flag's scope.
- So the flag's condition (error AND output both non-empty) never occurred. A maintain_suite
  no-harm run cannot exercise it; this matches PLAN.md's expectation.

## Outcome detail

- 8 of 10 fixtures produced the same patch in all 6 runs.
- `neon-rain-implement-reset-shortcut`: ctl produced 3 distinct patches, fo 1 (the same as ctl
  rep1). Since the flag never changed any content, this is temp-0 substrate variance, not the flag.
- `nothing-ever-happens-document-config`: 5 distinct docs across 6 runs (fo rep1 = ctl rep1). All
  six cover the same 32 of the 57 env vars the bot reads at startup
  (`../thinkingcap_4bit_2026_11/hv/document-config-envvars-57.txt`). All omit the 23 `PM_NH_*`
  overrides, so the arms are THIN alike. No doc names a variable the code does not read
  (`DEBUG`/`WARNING` hits are LOG_LEVEL values).
- Report fallbacks (abort report, post-write idle exit): ctl 8, fo 10. The extra two are
  neon-rain rep 2 (ctl's rep 2 took a different, longer path there) and document-config rep 2.
  Both are variance, and every fallback lists the right files.

## Not concluded

Wall -8.8% is not a flag effect (the content was identical); it is ordering and substrate drift
inside one session. No promotion decision follows from Part 1.

## Re-score note (2026-11-20)

Coverage above is 32 of the canonical **55** (not 57; two `scripts/`-only names were miscounted).
See `../doc_config_rescore_2026_11/REPORT.md` (canonical 55-name set from `scripts/doc_config_envcheck.py`; bench requirement R4 records it on every run since then).
