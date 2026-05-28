# Extended benchmark suite

Broad-capability evaluations layered on top of luxe's agentic benchmarks
(BFCL / SWE-bench / maintain_suite). Five benchmarks live here, intended
to surface regressions in factual recall, reasoning, math, long-context
behavior, and raw language-modeling quality that the agentic suite would
miss until they cascade into a downstream task failure.

## Benchmarks

| Benchmark | What it measures | Scoring | Backend |
|---|---|---|---|
| **MMLU** | 57-subject factual / reasoning breadth | First-token logprob over A/B/C/D | mlx_lm (in-process) |
| **ARC-Challenge** | Grade-school science reasoning, 0-shot | First-token logprob over A/B/C/D/E | mlx_lm (in-process) |
| **GSM8K** | Multi-step grade-school math, 8-shot CoT | Generation + numeric extraction | HTTP oMLX |
| **CodeNeedle** | Long-context positional recall on code | Verbatim line-match (vendored upstream scorer) | HTTP oMLX |
| **Perplexity** | Held-out LM quality (internal regression metric) | Sliding-window logprob aggregation | mlx_lm (in-process) |

Two access paths exist because **oMLX silently drops `logprobs` / `top_logprobs`** on both `/v1/chat/completions` and `/v1/completions` (verified 2026-05-27). Benchmarks that need logprobs load `mlx_lm` directly via `benchmarks/_eval_common/mlx_direct.py`.

**Sequencing constraint:** the mlx_lm path loads the 35B 6-bit weights (~25 GB) into this process. On a 64 GB machine, stop the oMLX server before running MMLU / ARC / Perplexity, or schedule them after the HTTP-Backend benchmarks. `scripts/run_eval_suite.sh` does this in the right order.

**Perplexity caveat:** treated as an *internal longitudinal regression metric*, not leaderboard-comparable. Quantized local inference, chat-template wrapping, and tokenizer specifics make apples-to-apples comparison to published WikiText PPL papers unreliable. Useful for tracking *changes* over time on a fixed setup.

## Architecture

```
benchmarks/
  _eval_common/              # pure-function utilities, no Backend coupling
    dataset.py               # cache_dir + sha256_verify + jsonl_load
    extract.py               # extract_gsm8k_answer, extract_choice_letter, strip_think_blocks
    choices.py               # format_mc_prompt for variable-choice-count MCQs
    fewshot.py               # deterministic sampler + canonical GSM8K 8-shot
    logprob.py               # plan_sliding_windows + perplexity aggregator
    meta.py                  # build_run_meta — embeds version/sha/timestamp in every summary
    mlx_direct.py            # in-process mlx_lm Backend (logprob path)
  gsm8k/                     # run.py + adapter.py + grade.py
  codeneedle/
    upstream/                # vendored esprima parser + SequenceMatcher scorer
    fixtures/                # http_server.py, jquery.js (vendored from upstream)
    manifest.json            # frozen 11+16 sampled needle functions (seed=42)
  mmlu/                      # 5-shot per-subject, with category breakdown
  arc_challenge/             # 0-shot, variable choice count (3/4/5)
  perplexity/                # sliding-window over WikiText-103 test split
```

`HTTP Backend` (`src/luxe/backend.py`) is **unchanged** — no risk to existing BFCL / SWE-bench / maintain_suite. Pre-existing tests still pass.

## Reproducibility

Every `summary.json` embeds a `meta` block (collected by `_eval_common/meta.py`):

```json
{
  "eval_suite_version": "0.1.0",
  "benchmark_protocol_version": "mmlu/v1",
  "model_id": "Qwen3.6-35B-A3B-6bit",
  "benchmark_dataset_sha256": "...",
  "luxe_commit": "...",
  "sampling": {...},
  "scoring": {"method": "...", "choice_token_ids": {...}},
  "timestamp_utc": "...",
  "device": "..."
}
```

Without this block, longitudinal comparisons can't tell "model regressed" apart from "tokenizer / chat-template / quant changed."

Each benchmark declares `BENCHMARK_PROTOCOL_VERSION` in its `run.py`. Bump it on any change to prompt wording, few-shot examples, extraction regex, or scoring math. Longitudinal charts must filter on protocol version — pre-bump and post-bump scores are NOT directly comparable.

CodeNeedle's manifest (the sampled function pool) is also frozen and committed. Re-running `scripts/build_codeneedle_manifest.py` = bumping the protocol version.

## Usage

```bash
# One-time setup
pip install -e .[extended-bench]
python scripts/fetch_gsm8k_data.py
python scripts/fetch_arc_data.py
python scripts/fetch_mmlu_data.py
python scripts/fetch_wikitext_data.py

# Run the suite
bash scripts/run_eval_suite.sh                       # everything
bash scripts/run_eval_suite.sh --bench gsm8k,mmlu    # subset
bash scripts/run_eval_suite.sh --limit 100           # quick partial pass
```

Per-benchmark invocation:

```bash
python -m benchmarks.gsm8k.run --output acceptance/gsm8k/<run_id>
python -m benchmarks.codeneedle.run --output acceptance/codeneedle/<run_id>
python -m benchmarks.mmlu.run --output acceptance/mmlu/<run_id>
python -m benchmarks.arc_challenge.run --output acceptance/arc/<run_id>
python -m benchmarks.perplexity.run --output acceptance/perplexity/<run_id>
```

Output: per-item JSONs + `summary.json` with the meta block. `scripts/aggregate_eval_suite.py` reads all per-bench summaries and writes a unified `summary.md`.

## Calibration expectations (Qwen3.6-35B-A3B-6bit)

Treat these as ballpark, not parity targets. Quantization + chat template + few-shot ordering all shift numbers a few points. Investigate only if results are *wildly* off — that usually indicates a scoring/prompt bug.

| Benchmark | Healthy range | Investigate if |
|---|---|---|
| MMLU (5-shot, logprob) | 65–80% | <50% → likely choice-token whitespace bug |
| ARC-Challenge (0-shot, logprob) | 75–85% | <60% → check variable-choice-count handling |
| GSM8K (8-shot CoT) | 70–85% | <50% → check `<think>`-stripping or `####` regex |
| CodeNeedle | baseline-relative | first run = baseline |
| Perplexity | baseline-relative | first run = baseline |

ARC at 0-shot will be 3–8 points below published 25-shot leaderboard numbers — this is expected, not a bug. Calibrate against lm-eval-harness 0-shot, not the leaderboard.

## Test discipline

102 offline unit tests (`tests/test_eval_common_*`, `tests/test_{gsm8k,codeneedle,mmlu,arc_challenge}.py`). All pure-function; no Backend imports, no network. Run in ~0.1s.

Live-model smoke tests (`tests/test_mlx_direct_smoke.py`) are gated by `@pytest.mark.live_model` — skipped by default. Run manually after stopping oMLX:

```bash
pytest -m live_model
```

## Limitations (in-scope)

- Local 35B inference makes full runs slow (~hours). On-demand CLI, not CI-gated.
- Single-quant evaluation only (per CLAUDE.md's single-champion policy).
- CodeNeedle measures *positional recall*, not long-context reasoning or multi-hop tracking.
- Perplexity is internal-only; do not publish.

## Out of scope

- Multi-quant fan-out / cross-quantization eval matrices
- Hosted-API comparisons (luxe is local-oMLX only)
- Automated regression thresholds in CI
- Patching oMLX to surface logprobs (would unify the two backend paths; deferred)
