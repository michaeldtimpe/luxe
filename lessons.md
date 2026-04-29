# Lessons Learned

> **Note:** Entries below pre-date the v1.0 implementation work. New
> lessons from the v1.0 acceptance bench (Phase 8) belong in this file
> as they're observed; see the README's [Benchmark workflow](README.md#benchmark-workflow)
> section for context. Old entries reference `swarm`/`src/swarm/`; mentally
> substitute `luxe`/`src/luxe/` when reading.

A running log of mistakes, surprises, and hard-won insights from building and testing the swarm pipeline. Each entry records what happened, why it happened, and what we changed.

---

## Format

Each entry follows this structure:

```
### [DATE] Short title

**What happened**: Description of the problem or surprise.

**Root cause**: Why it happened — the assumption that was wrong, the edge case missed, etc.

**Fix / takeaway**: What we did about it and the general principle for next time.

**Affected files**: Which parts of the codebase were involved.
```

---

## Entries

### [2026-04-28] Kimi K2 does not fit in 32 GB — MoE memory model misunderstanding

**What happened**: We planned to build a Kimi model family config alongside Qwen and DeepSeek for benchmarking. Research revealed that the smallest Kimi K2 variant requires 538 GB at 4-bit quantization. Despite having only ~32B active parameters per token, the full 1T parameter set (384 experts) must reside in memory.

**Root cause**: MoE "active parameter" counts are misleading for memory planning. The active count determines compute throughput (fast inference because only 8 of 384 experts fire per token), but **all expert weights must be loaded into RAM**. A 1T-total / 32B-active MoE model takes the same memory as a 1T dense model — the savings are in compute, not storage. The research doc's "future upgrades" section listed Kimi K2 for 128+ GB machines, but we overlooked that when scoping the 32 GB benchmark suite.

**Fix / takeaway**: Only two model families (Qwen, DeepSeek) are viable for 32 GB pipelines. When evaluating MoE models for memory-constrained deployments, always check **total parameter count × quantization bits**, not active parameters. The only Kimi model that fits under 18 GB is Kimi-VL-A3B-Thinking (9 GB), which is a vision-language model unsuitable for code tasks.

If a third family is needed for comparison, Llama 3, Gemma 3, or Phi 4 could substitute — all have MLX-quantized variants spanning 1B–27B.

**Affected files**: `configs/` — no Kimi config created; `scripts/download_models.sh` — Kimi section omitted.

---

### [2026-04-28] DeepSeek-Coder-V2-Lite lacks native tool calling template

**What happened**: DeepSeek-Coder-V2-Lite-Instruct is the only code-specialized DeepSeek model that fits in 32 GB (8.23 GB, MoE with 2.4B active params). However, it does not have a structured tool calling template in its oMLX/MLX tokenizer config. The R1-Distill-Qwen models inherit Qwen's tool calling support, but Coder-V2-Lite uses a basic chat template.

**Root cause**: The MLX community quantization of DeepSeek-Coder-V2-Lite was created before tool calling templates were standardized. The model can still follow tool-calling instructions in its system prompt, but it outputs tool calls as text (JSON in response) rather than structured `tool_calls` objects.

**Fix / takeaway**: The agent loop's `_parse_text_tool_calls()` function handles this case — it recovers tool calls from `<tool_call>` tags and bare JSON in response text. The DeepSeek config will work, but the `worker_read` role (which uses Coder-V2-Lite) may have a higher schema reject rate than Qwen equivalents. Monitor `schema_rejects` in benchmarks to quantify the impact.

**Affected files**: `configs/deepseek_32gb.yaml` — uses Coder-V2-Lite for worker_read despite the template gap; `src/swarm/agents/loop.py` — text recovery path is critical for this model.

---

### [2026-04-28] `huggingface-cli` deprecated — use `hf` instead

**What happened**: The download script used `huggingface-cli download` but the `huggingface_hub` package (v1.12+) has deprecated that command in favor of the new `hf` CLI. Running the script produced `Warning: huggingface-cli is deprecated and no longer works` and all downloads silently failed.

**Root cause**: The `hf` binary is installed alongside `huggingface_hub[cli]` but only into the venv's `bin/` directory. The script ran with `#!/usr/bin/env bash` which didn't activate the venv, so `hf` wasn't on PATH. The old `huggingface-cli` entrypoint now just prints a deprecation notice and exits non-zero.

**Fix / takeaway**: Updated `scripts/download_models.sh` to: (1) auto-activate the project venv, (2) search for `hf` first, then fall back to the venv's `hf` by explicit path, then `huggingface-cli` as last resort. When writing shell scripts that depend on venv-installed tools, always activate the venv or use the full path to the binary.

**Affected files**: `scripts/download_models.sh`

---

### [2026-04-28] oMLX model IDs don't include HuggingFace namespace prefix

**What happened**: `swarm check` showed all 9 models as missing (✗) even though oMLX had discovered all of them. The configs referenced `mlx-community/Qwen2.5-3B-Instruct-4bit` but oMLX's `/v1/models` API reported `Qwen2.5-3B-Instruct-4bit` — no namespace prefix.

**Root cause**: oMLX reads from `~/.omlx/models/` and uses the directory structure under `mlx-community/` as a filesystem path, but only the leaf directory name becomes the model ID in the API. The symlinks we created were `~/.omlx/models/mlx-community/Qwen2.5-3B-Instruct-4bit → ...`, so the model ID is just `Qwen2.5-3B-Instruct-4bit`. The configs had been written using the full HuggingFace repo ID format.

**Fix / takeaway**: Stripped the `mlx-community/` prefix from all model IDs in both `configs/qwen_32gb.yaml` and `configs/deepseek_32gb.yaml`. When configuring model IDs for oMLX, always use the name as reported by `/v1/models`, not the HuggingFace repo identifier. Run `swarm check` after any config change to verify model resolution.

**Affected files**: `configs/qwen_32gb.yaml`, `configs/deepseek_32gb.yaml`

---

<!--
Anticipated categories based on the research doc:

- **Context management**: Issues with token estimation, elision timing, pressure thresholds
- **Model behavior**: Unexpected tool-call formats, fabrication patterns, role confusion
- **Tool dispatch**: Schema validation edge cases, tool-call recovery failures
- **Pipeline flow**: Architect decomposition quality, escalation loops, validator coverage
- **Performance**: Model swap latency, throughput surprises, cache effectiveness
- **Configuration**: Threshold tuning, role config adjustments, temperature effects
-->
