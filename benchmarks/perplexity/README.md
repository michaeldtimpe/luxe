# Perplexity

**Internal regression metric only — not leaderboard-comparable.**

Sliding-window perplexity over WikiText-103-raw test split, computed via
`mlx_lm` in-process (oMLX silently drops logprobs). Tokenization, chat
template, and quantization differ from published lm-eval-harness setups,
so the number here is **not** comparable to papers' WikiText PPL figures.

Use it for tracking *changes over time on a fixed setup* — e.g.,
quantization regressions often show here before agentic failure rates
move. A drop of 5–10% in perplexity vs. the committed baseline is the
threshold to investigate.

## Sequencing

Loads the 35B 6-bit model in-process (~25 GB). On the 64 GB single-machine
configuration, stop the oMLX server first or schedule after BFCL /
maintain_suite / GSM8K / CodeNeedle (which use the HTTP Backend).

## Windowing

Window = 32768 tokens (Qwen3.6 context cap as served).
Stride = window // 2.

Window 0 evaluates tokens [1, window). Window i (>0) evaluates tokens
[last_eval_end, window_end). No token is double-counted; tokens 0 and
the first token of any non-overlapping window past 0 are skipped (they
have no in-window prior context).

## Bump conditions for `perplexity/v1`

- Corpus file changed (different WikiText version)
- Tokenizer changed
- Window or stride parameters changed
- Aggregation math changed

Any of these = bump to `perplexity/v2`. Old values are not comparable.
