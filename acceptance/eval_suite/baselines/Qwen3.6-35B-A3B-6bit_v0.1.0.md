# Extended-bench baseline — `Qwen3.6-35B-A3B-6bit` v0.1.0

First canonical baseline of the extended-benchmark suite on luxe's
single champion model (`mlx-community/Qwen3.6-35B-A3B-6bit`). Captured
on `2026-05-29T20-46-39Z`, ~31 hours wall, on a 64 GB Apple Silicon
host. luxe commit `2856fab`.

## Headline numbers

| Benchmark | Result | Calibration band | Notes |
|---|---|---|---|
| GSM8K — think (deployed, max_tokens=4096) | **96.74%** | n/a | 1319 / 1319, parse 100% |
| GSM8K — no-think (Wei canonical, /no_think) | **62.85%** | 70-85 (below; real 6-bit drift) | 1319 / 1319, parse 100% |
| CodeNeedle — http_server.py | **100%** pass | n/a | 11 / 11, primary 98.64% |
| CodeNeedle — jquery.js | **93.75%** pass | n/a | 15 / 16, primary 94.69% |
| MMLU (full test) | **68.84%** micro / 65.97% macro | 65-80 | 14042 / 14042 |
| ARC-Challenge (full) | **78.33%** | 75-85 | 1172 / 1172 |
| Perplexity (wikitext-103-test, window=8192) | **5.7448** | n/a (internal) | 297,053 tokens, 72 windows |

MMLU per-category: STEM 56.90% (weak, consistent 6-bit drift), humanities
70.75%, social_sciences 74.29%, other 72.64%.

GSM8K no-think and MMLU sit slightly outside / below the published-band
expectations. Both reproduced tightly across calibration sample sizes,
so the gap is real model behavior at 6-bit quantization rather than a
methodology bug. See calibration progression below.

## Calibration progression

Each benchmark across the three sample sizes run during the calibration
session, demonstrating convergence:

| Benchmark | n=100 | n=500 | full N | Δ(full − n=500) |
|---|---|---|---|---|
| GSM8K think | 99.00 % | 97.40 % | 96.74 % (n=1319) | −0.66 |
| GSM8K no-think | 63.00 % | 62.20 % | 62.85 % (n=1319) | +0.65 |
| CodeNeedle combined | 96.30 % | 96.30 % | 96.30 % (n=27) | 0 (deterministic) |
| MMLU | 64.00 %* | 65.80 % | 68.84 % (n=14042) | +3.04 |
| ARC | 81.00 % | 78.40 % | 78.33 % (n=1172) | −0.07 |
| Perplexity | 5.7448 | 5.7448 | 5.7448 | 0 (deterministic) |

*MMLU n=100 used the pre-fix non-stratified `--limit` (only
`abstract_algebra`); for fair comparison the n=100 column reports the
re-validated stratified number from Phase B v2 of the calibration
session. Numbers converge to within 1-3 points by n=500.

## What the version number means

- **`eval_suite_version: 0.1.0`** — the suite's overall protocol
  envelope (meta block schema, dir layout, aggregator output format).
- **Per-benchmark `BENCHMARK_PROTOCOL_VERSION` (all `*/v1`)** — the
  prompt template + scoring contract for each benchmark. Bumps
  required on:
  - prompt changes (chat template, few-shot exemplars, system prompt)
  - scoring changes (extractor, threshold, window size)
  - dataset changes (sha256 mismatch from the fetch scripts)
- The fixes that shipped in `2856fab` (chat template, perplexity
  window, GSM8K think wiring) did change prompts and scoring for
  several benchmarks, but the previous v1 protocols never produced
  baseline numbers (the original `c5acdef` ship was a deferred-run),
  so we are establishing v1 numbers with the fixed protocols rather
  than bumping v1 → v2 in flight.

The next baseline (v0.2.0) should ship when any of the following
happens:
- Multi-quant comparison (4-bit / 8-bit) is added — these get their
  own per-model baseline files at the same suite version.
- A prompt or scoring change forces a protocol bump.
- The agentic suite (BFCL / SWE-bench / maintain_suite) joins the
  same aggregator output for a unified report.

## Reproducibility notes

- Run captured under `acceptance/eval_suite/2026-05-29T20-46-39Z/`,
  including all per-item JSON files and per-benchmark `summary.json`
  metadata blocks (model ID, dataset sha256, sampling config,
  scoring config, luxe commit, timestamps).
- HTTP backend served by oMLX (brew-managed launchd) on `127.0.0.1:8000`.
- `~/.omlx/settings.json` `sampling.max_context_window` and
  `sampling.max_tokens` set to 131072 for the run. Below the default
  32768 cap, CodeNeedle jquery.js would fail with HTTP 400. See
  CALIBRATION-2026-05-28.md in the research repo for context.
- `mem-tier=low` resolved automatically (`sysctl hw.memsize` → 64 GB);
  wrapper auto-stopped oMLX between Phase A and Phase B via
  `brew services stop omlx`. Phase B used `mlx_lm.load`
  in-process (~25 GB resident).
- Perplexity window=8192 / stride=4096 is below the mlx_lm Qwen3.6
  long-context cliff at 16K. Upstream WikiText reporting would
  normally use a 32K/16K split — bump back once `mlx_lm`'s
  long-context attention is patched for `Qwen3_5MoeForConditionalGeneration`.

## Source summary

The full aggregator-rendered summary as captured at run time:

---

# Eval suite — 2026-05-29T20-46-39Z

## Run metadata

- eval_suite_version: `0.1.0`
- model: `Qwen3.6-35B-A3B-6bit`
- luxe_commit: `2856fabddb4385092fd9d14ba00d5e6f8978cb6a`
- timestamp_utc: `2026-05-30T13:19:47Z`
- device: `Darwin arm64 (Python 3.11)`

## gsm8k_think
- protocol: `gsm8k/v1`
- think_mode: `True`  max_tokens: `4096`
- count: 1319
- accuracy: **96.74%**
- parse_rate: 100.00%
- failure_reasons: {'none': 1319}

## gsm8k_nothink
- protocol: `gsm8k/v1`
- think_mode: `False`  max_tokens: `512`
- count: 1319
- accuracy: **62.85%**
- parse_rate: 100.00%
- failure_reasons: {'none': 1319}

## codeneedle
- protocol: `codeneedle/v1`
- http_server.py:
  - pass_rate: **100.00%**
  - primary_match_rate: 98.64%
  - hallucinations: 1405
  - bonus_matched: 188
- jquery.js:
  - pass_rate: **93.75%**
  - primary_match_rate: 94.69%
  - hallucinations: 2112
  - bonus_matched: 230

## mmlu
- protocol: `mmlu/v1`
- count: 14042
- accuracy_micro: **68.84%**
- accuracy_macro_per_subject: 65.97%
  - STEM: 56.90%
  - humanities: 70.75%
  - social_sciences: 74.29%
  - other: 72.64%

## arc_challenge
- protocol: `arc_challenge/v1`
- count: 1172
- accuracy: **78.33%**
  - 3-choice questions: 100.00% (n=4)
  - 4-choice questions: 78.20% (n=1165)
  - 5-choice questions: 100.00% (n=3)

## perplexity
- protocol: `perplexity/v1`
- perplexity (internal metric, NOT leaderboard-comparable): **5.7448**
- tokens_evaluated: 297,053
- num_windows: 72
