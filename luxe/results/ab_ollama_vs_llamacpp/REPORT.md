# A/B benchmark — `ollama_q4km` vs `llamacpp_q4km`
## Per-candidate verdicts
- **qwen2.5-32b-instruct** — no meaningful difference
- **qwen2.5-7b-instruct** — no meaningful difference
- **qwen2.5-coder-14b** — no meaningful difference

## Detail (Δ% rows: TTFT lower-is-better; decode higher-is-better)

| benchmark | candidate | decode_delta_pct | llamacpp_q4km_decode_tok_s | llamacpp_q4km_pass_pct | llamacpp_q4km_peak_rss_gb | llamacpp_q4km_ttft_s | n | ollama_q4km_decode_tok_s | ollama_q4km_pass_pct | ollama_q4km_peak_rss_gb | ollama_q4km_ttft_s | ttft_delta_pct |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| decode_throughput | qwen2.5-32b-instruct | +3% | 7.9 | 100.0 | 25.65 | 17.458 | 3 | 7.6 | 100.0 | 38.22 | 17.941 | -3% |
| decode_throughput | qwen2.5-7b-instruct | -1% | 33.2 | 100.0 | 6.3 | 3.654 | 3 | 33.5 | 100.0 | 5.53 | 3.715 | -2% |
| decode_throughput | qwen2.5-coder-14b | +4% | 17.6 | 100.0 | 20.61 | 7.627 | 3 | 16.9 | 100.0 | 11.86 | 7.775 | -2% |
