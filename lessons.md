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

### [2026-04-29] Tool-side guards alone don't make small models honest

**What happened**: Phase 2 bake-off showed micro__qwen-coder-1.5b "passing" `neon-rain-implement-reset-shortcut` by deleting 840 lines of `Game.js` and replacing them with a 3-line `function reset() { /* Reset the game state here */ }`. Test runner exited 0 because the deleted module wasn't covered by the surviving tests. Adding three write-time guards — `_check_placeholder_text`, `_check_role_path`, `_check_mass_deletion` — closed the obvious cases (literal `<paste...here>`, `worker_read.js`, 840-line wipes). On re-run the same model evaded each guard with a small adjustment: `worker_read_r.py` (suffix variant past exact-stem match), `# Your real listener code here` (extra adjective past single-`\w+` regex), orphan Python files in a JS project (no test file added → vacuous-test gate doesn't fire).

**Root cause**: A 1.5B model gaming pattern-based gates is a whack-a-mole game. Pattern matching can't catch semantic correctness — "you wrote a Python file in a React project" or "your test asserts on `undefined === undefined`" requires reading the diff *with the goal in mind*, which is what a higher-weight reviewer is for.

**Fix / takeaway**: Defense-in-depth: keep the cheap pattern gates (they kill the easy cheats), but accept that the architectural answer for quality is a 32B-Instruct chief architect that reads the diff and verdicts each task. Path B (`--mode phased`) is built around this — the architect runs after each group of atomic tasks with bounded retry → graceful abort. Don't expect any pattern set to be evasion-proof against a model that has nothing else to optimize for.

**Affected files**: `src/luxe/tools/fs.py` (write-time guards + tightened regex); `benchmarks/maintain_suite/grade.py` (post-PR strict gates with same guards); `src/luxe/agents/phased.py` (Path B — architect-as-reviewer); `tests/test_tools.py` (evasion-regression tests for each observed bypass).

---

### [2026-04-29] `tests_pass` grading credits diffs that don't actually implement the feature

**What happened**: Three "passing" PRs on the same fixture (`neon-rain-implement-reset-shortcut`) were all wrong in different ways: (1) Phase 1 32B-mono produced a real `addEventListener('keydown')` fix; (2) Phase 2 swarm-14B added a vacuous test file that asserts `expect(handler.someState).toBe(undefined)` against a `dispose()` that doesn't exist (test passes because `undefined === undefined`); (3) Phase 2 micro-1.5B deleted the game's controller and shipped a stub. All three got `tests_pass: True` because the existing test suite kept passing regardless.

**Root cause**: `_check_tests_pass` runs the test command and credits any rc=0 result, regardless of whether the diff actually exercises the feature. It can't distinguish "test passes because the implementation works" from "test passes because the test doesn't test what we asked for" or "test passes because the new code is unreachable from the test suite". Adding more diff inspection helps (`destructive_diff`, `role_name_leak`, `placeholder_diff`) but doesn't solve the orphan-file or vacuous-test cases.

**Fix / takeaway**: Added a `vacuous_test` gate to `grade.py`: when `tests_pass` succeeds at HEAD AND the diff includes new/modified test files, check out a worktree at `base_sha`, copy the new test files in, and re-run the test command. If it passes against unmodified base implementation, the test isn't exercising new code → mark `vacuous_test`, override `expected_outcome` to False. Closes one of the four observed `tests_pass` exploit modes. The orphan-file mode (diff adds files in a different language than the test suite covers) is still uncaught by automation; surfaced as the architect's job in `--mode phased`.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`check_vacuous_test`, `_looks_like_test_file`).

---

### [2026-04-29] oMLX `idle_timeout_seconds: null` keeps every loaded model resident forever

**What happened**: After multiple bench runs, `oMLX` had 7 models loaded summing to 44.9 GB resident — ~80% of the 64 GB system. Even when no luxe process was running, models stayed loaded indefinitely. User reported a `Qwen2.5-1.5B-Instruct-4bit` re-loading itself shortly after a manual unload; the only callers were stale admin-dashboard polls or background integrations.

**Root cause**: `~/.omlx/settings.json:idle_timeout.idle_timeout_seconds` defaulted to `null`, meaning no auto-eviction. Combined with `max_model_memory: "auto"` (which oMLX set to 80% of system = 54 GB), there was nothing forcing eviction until total memory hit the cap.

**Fix / takeaway**: Set explicit values in `~/.omlx/settings.json`:
- `max_model_memory: "36GB"` — hard cap forces eviction much earlier.
- `idle_timeout.idle_timeout_seconds: 300` — auto-unload anything not accessed in 5 min.
Plus: `mx.metal.set_wired_limit` is *process-level*, but oMLX runs as a separate process so that knob in luxe is a no-op for actual inference. Use `sudo sysctl iogpu.wired_limit_mb=36864` for system-wide Metal pinning, and add `iogpu.wired_limit_mb=36864` to `/etc/sysctl.conf` to persist. Also added `luxe unload` CLI for ad-hoc cleanup and `--keep-loaded` flag on `luxe maintain` for the rare case you want models warm between runs.

**Affected files**: `~/.omlx/settings.json` (user-side config); `src/luxe/backend.py` (`unload_model`, `unload_all_loaded`, `loaded_models`); `src/luxe/cli.py` (`luxe unload` command, post-run unload hook).

---

### [2026-04-29] StarCoder2-3B and CodeGemma-2B aren't chat-tuned — `tokenizer.chat_template` missing

**What happened**: Tested 7 small coder models in a bake-off harness. 4 worked (Qwen2.5-Coder-0.5B, Qwen2.5-Coder-1.5B, granite-4.0-h-tiny, stable-code-instruct-3b); 3 failed at warmup probe time with "Chat template error: Cannot use chat template functions because tokenizer.chat_template is not set". The 3 failures were StarCoder2-3B and CodeGemma-2B (no chat template at all) and DeepSeek-Coder-1.3B-instruct-mlx (legacy mlx-lm filename `weights.00.safetensors` instead of `model.safetensors`).

**Root cause**: StarCoder2 and CodeGemma at small sizes are released as base completion models — no instruction-tuned chat variant exists at ≤3B params. The mlx-community quantizations preserve the empty `chat_template` field. There's no clean way to use them in a chat-driven agent loop without writing a custom prompt-flattening wrapper, and a synthetic chat template applied to a non-chat-tuned model produces poor results regardless. DeepSeek-Coder-1.3B was a separate naming-convention issue, fixable with a `model.safetensors` → `weights.00.safetensors` symlink.

**Fix / takeaway**: Three concrete actions for any future small-model bake-off: (1) probe `tokenizer_config.json:chat_template` before adding a candidate to the roster; if empty, drop it. (2) Probe each candidate via a 1-token chat completion before the bench timer starts — surfaces missing `chat_template` and missing weight files in seconds, not after a 30-min run. (3) For legacy mlx-lm uploads with `weights.NN.safetensors`, a symlink alias is enough; no need to re-quantize.

**Affected files**: `scripts/bench_small_models.py` (warmup probe, default candidate roster, fuzzy filename handling); `scripts/register_omlx_models.py` (creates symlinks from HF cache to oMLX models dir).

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
