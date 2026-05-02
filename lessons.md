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

### [2026-04-30] Mono-only pivot — swarm/micro/phased deleted in the v1.0 simplification

**What happened**: After the 8-bit completion bake-off (2026-04-30) confirmed `mono__qwen3.6-35b-a3b-6bit` as the strongest configuration we'd produced (2/5 real PASSes, including the only production-quality `the-game` and `neon-rain` JS implementations across every prior bake-off), we ripped out the swarm, microloop, and phased execution modes entirely. The codebase shrank by ~3000 lines: deleted `src/luxe/agents/{microloop,phased,architect,synthesizer,validator,worker}.py`, `src/luxe/pipeline/`, `src/luxe/benchmark/`, `src/luxe/metrics/`, `src/luxe/escalation.py`, `src/luxe/mode_select.py`, `configs/{swarm,qwen,deepseek,mode}.yaml`, and 6 test files. `luxe maintain` no longer takes `--mode`; it always runs the monolith.

**Root cause**: Across 6 bake-offs with strict regrade + hand inspection, mono won at every model size we tested ≥14B. swarm tied at 14B (one swarm "pass" was flagged by strict gates and downgraded by hand) and lost outright at every other scale. micro hit 0/5 real at every scale. phased hit 0/5 real even with bounded retry — the architect rubber-stamped fabrication. The multi-agent designs were trying to compensate for small-model weaknesses; once the champion was a 35B-A3B MoE that can drive the agentic loop alone, the decomposition layer added latency and exploit surface (vacuous tests, role-name leaks, mass-deletion gaming) without quality gains.

**Fix / takeaway**: The architecture is now `Backend → run_single → run_agent` and the only config knob is which monolith model to load. The bench harness was simplified in lockstep: variant YAML now takes only `(model_label, model_id)` pairs (legacy `mode: mono` is accepted; `mode: swarm|micro|phased` raises a clear error). Three lessons we paid in real time:
  1. Don't keep "fallback" execution modes around as escape hatches when the data shows they don't beat the primary — they accumulate maintenance debt and confuse experiments.
  2. Once a champion model exists, the right move is to invest in fixture coverage and grader strictness, not in alternate orchestration topologies.
  3. The `ValidatorEnvelope` data classes (used by the citation linter) outlived their producer (the swarm validator); we moved them into `citations.py` rather than keep an empty `validator.py` shell. When a module is mostly dead but a sibling needs a sliver, inline the sliver and delete the module.

**Affected files**: `src/luxe/cli.py` (rewrite, ~40% smaller), `src/luxe/citations.py` (inlined ValidatorEnvelope), `src/luxe/run_state.py` (dropped stage/blackboard helpers + mode fields), `src/luxe/config.py` (default → `single_64gb.yaml`), `benchmarks/maintain_suite/run.py` (variant matrix simplified), `tests/conftest.py`. New default variant file: `benchmarks/maintain_suite/variants_v1_default.yaml`.

---

### [2026-04-30] Regex-present grader gamed three ways — added min_matches and min_added_lines

**What happened**: The 8-bit completion bake-off produced 6 raw PASSes across two variants. Hand-inspection downgraded 4 of them: (1) lpe-rope-calc — task asked for a module docstring + type hints on EVERY top-level function, model added zero docstrings and typed ONE parameter on ONE function; the regex `def \w+\(...: (str|int|...)` matched once and the test "passed". (2) isomer — task said "ADD a Quickstart section", model RENAMED "Quick Start" → "Quickstart" and stripped the ISOMER_SECRET setup the app requires to start; the regex `(?i)#+\s*Quickstart` matched. (3) nothing-ever-happens — task said "identify dependencies with KNOWN security advisories", model created a 2-file audit doc with "no known issues" listed for every dependency; the regex `(?i)# SECURITY` matched on the doc header. None of these three diffs implemented the asked task; all three matched the regex on the first added line.

**Root cause**: A regex against added diff lines verifies *something added* matches *a pattern*. It cannot verify *the task is complete*. For multi-call-site tasks ("type every function") a single match is the same as zero work. For document tasks, a one-line edit (or a section rename that matches the header pattern) is indistinguishable from a substantive write. For audit/manage tasks, header-keyword matching admits "I looked but found nothing" surveys as if they were findings.

**Fix / takeaway**: Added two optional fields to `expected_outcome` for `regex_present` checks in `benchmarks/maintain_suite/grade.py`:
  - `min_matches` (default 1) — pattern must hit in at least N distinct added lines. Defeats single-edit gaming on multi-call-site tasks.
  - `min_added_lines` (default 0) — diff must add at least N total lines across changed files. Defeats rename-only or one-line edits that technically match the regex.
Updated all 5 v1 fixtures with appropriate values (lpe-rope-calc `min_matches: 4`, isomer `min_added_lines: 8`, nothing-ever-happens `min_matches: 3` + version-string-or-CVE pattern). Added 5 new fixtures (10 total, the v1 release-gate floor) calibrated with these thresholds. The thresholds are fixture-author choices — fixtures with substantive scope set them aggressively; trivial single-edit fixtures leave them at defaults.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`_check_regex_present` signature), `benchmarks/maintain_suite/fixtures.yaml` (5 existing tightened, 5 new fixtures added).

---

### [2026-04-30] Orphan-file gate — close the last "tests pass for the wrong reason" hole

**What happened**: The granite-4.1-3b-bf16 bake-off (2026-04-30) raw-passed `neon-rain-implement-reset-shortcut` at 5/5. Hand inspection: the model created a NEW file `src/input/HtmlInputHandler.ts` next to the existing `HtmlInputHandler.js` in a JavaScript-only project. Nothing imported the new TypeScript file — `Game.js` still imports the original `.js` handler. `npm test` passed because the existing implementation was unchanged. The vacuous_test gate didn't fire because vacuous_test only inspects NEW *test* files, not new *source* files. This is exactly the orphan-file mode flagged in the [2026-04-29] entry as "uncaught by automation; surfaced as the architect's job in --mode phased" — and we deleted phased mode in the v1 simplification.

**Root cause**: A successful test run plus a non-empty diff plus a new file in the diff doesn't mean the new file is on the executed code path. The model can satisfy "added a file matching the pattern" and "tests pass" simultaneously by adding parallel-language duplicates (`.ts` next to `.js`, `.py` in a JS project) or by adding bare modules nothing wires in. Tests pass against the unchanged existing implementation.

**Fix / takeaway**: Added `check_orphan_file` in `benchmarks/maintain_suite/grade.py`. Only applies to `implement`/`bugfix` tasks (document/manage legitimately add standalone files like CONFIG.md or SECURITY-AUDIT.md). Two-prong detection — a NEW source file is orphan iff EITHER:
  1. A sibling file with the same stem exists in the same directory (e.g. `Foo.ts` next to `Foo.js`). Strong duplicate signal — catches the granite-3b case.
  2. The file's stem isn't referenced anywhere else in the post-edit repo (no import/require/path-string mentions it).
The gate fires on both `tests_pass` and `regex_present` outcomes — the regex case mattered too because a model can match a regex by adding an unwired source file just as easily as by adding the right edit. Five tests in `test_acceptance_grader.py` cover: duplicate stem, unreferenced new file, properly-wired new file (must NOT fire), document tasks (must NOT fire), and non-source additions like markdown (must NOT fire).

This closes the last automated grader hole identified across all bake-offs. The remaining false-positive surface area is fundamentally a hand-inspection problem (semantic correctness), not pattern matching.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`check_orphan_file`, `_added_files_in_diff`, `_is_source_path`, plus integration in `grade_fixture`'s `tests_pass` and `regex_present` branches), `tests/test_acceptance_grader.py` (5 new tests).

---

### [2026-05-01] Phase 1 prompt-shaping outcome — structural prompts trade implement for doc/manage; Branch B is the natural next step

**What happened**: Ran the 60-run prompt-shaping sweep specified in `~/.claude/plans/jiggly-baking-kahan.md` against the 10 v1 fixtures with 6 cells: `champ-baseline`, `champ-baseline-rp__rp105`, `champ-cot`, `champ-sot`, `champ-hads`, `champ-combined-rp`. None of the structural variants beat baseline overall — baseline scored 7/10, the others ranged 4-6/10. But hidden inside the totals: every prompt-shaped variant (cot, sot, hads, combined-rp) cleared a 4/4 implement ceiling, beating baseline's 3/4 on the implement category. The wins came at a cost: every structural variant lost 1-2 documents and 0-1 manage tasks vs baseline. Per-task-type breakdown:

| Cell | impl (4) | doc (5) | manage (1) | Total |
|---|---|---|---|---|
| baseline | 3 | 3 | 1 | 7 |
| cot/sot/hads/combined-rp | 4 | 1-2 | 0-1 | 5-6 |
| baseline-rp | 3 | 1 | 0 | 4 |

**Root cause**: the structural prompts (CoT plan-first, SoT skeleton-first, HADS strict FIRST/THEN/ONLY-AFTER) all push the model toward action — call `edit_file`, plan-then-act, stop deliberating. That framing is exactly right for implement work and exactly wrong for documents (which need prose deliberation) and manage tasks (which need analytical depth before any edit). The action push was one-size-fits-all when the right answer is per-task-type.

A side observation: `champ-baseline-rp__rp105` (sampling-only, no prompt change) scored WORSE than baseline at 4/10. `repeat_penalty=1.05` on a code-gen workload pushed the model to invent identifier divergence (per the risk we'd flagged in `jiggly-baking-kahan.md`). Sampling penalties hurt without prompt-shaping help; they don't compose the way the rp+structural cell hoped.

**Fix / takeaway**: Branch B (per-task-type overlays). Implemented in commit `b81b628`: `prompts.py` gains a `TaskOverlay` dataclass + `TASK_OVERLAYS` registry; `RoleConfig` gains a `task_overlay_id` field; `run_single` resolves prompt ids per task type via `resolve_prompt_ids()`. The seed overlay `implement_via_cot` maps `implement` and `bugfix` to the `cot` PromptVariant; document/manage/review/summarize fall through to baseline. New variant file `variants_task_type_overlay.yaml` runs a 2-cell sweep: `champ-baseline-control` + `champ-implement-via-cot`. The control cell deliberately re-runs baseline alongside the overlay so the noise floor is captured in the same wall window — Phase 1 baseline swung 5→7 between identical runs, so single-run deltas of 1 fixture are noise.

The hypothesis is testable in 1-3 hours of bench wall: if the overlay composes baseline's doc+manage performance with the structural-variant implement ceiling, the cell projects to 8/10 and ships v1.0. If it ties baseline at 7/10, sampling variance is dominating and we either re-run for sample size or fall through to Branch C (calibration relax). If it scores below baseline, the overlay leaks structural framing into doc/manage somehow and the implementation needs revisiting.

**Affected files**: `~/.claude/plans/task-type-overlays.md` (new), `src/luxe/agents/prompts.py` (TaskOverlay + registry + resolve_prompt_ids), `src/luxe/config.py` (task_overlay_id field), `src/luxe/agents/single.py` (dispatch), `benchmarks/maintain_suite/run.py` (Variant + overlay write + loader), `benchmarks/maintain_suite/variants_task_type_overlay.yaml` (new), `tests/test_prompts.py` + `tests/test_config.py` (overlay tests).

---

### [2026-05-01] Multi-variant `v1_release_gate` was checking total passes across all cells

**What happened**: The Phase 1 sweep finished with 33 total passes across 60 runs and the bench summary printed `v1 release : YES (needs ≥8 of ≥10 passing)`. That was wrong — no single cell hit 8/10, so no variant is promotable. The gate was telling us we could ship when we can't.

**Root cause**: `summarize()` in `benchmarks/maintain_suite/grade.py` set `v1_release_gate = passed >= 8 and total >= 10` against the FLAT results list. For multi-variant runs that flat list aggregates across cells. 33 passes ≥ 8 and 60 ≥ 10 → gate says YES even though every individual cell is below 8/10. The bug was latent in single-variant runs (where the flat list IS one cell) but surfaced as soon as we ran the prompt-shaping matrix.

**Fix / takeaway**: `summarize()` now takes an optional `per_variant: dict[str, list[FixtureResult]]` argument. When provided, the gate is True iff some cell has ≥8/10 fresh passes. When omitted (single-variant or no-variant runs), the original flat threshold still applies for back-compat. The print site in `run.py` shows which cells cleared (`cleared by: <vid>`) or reports `per-cell ≥8/10 — no cell cleared`. `summary.json` now carries `v1_release_gate_per_variant` for downstream tooling. Three new regression tests in `tests/test_acceptance_grader.py`: no cell cleared (the actual Phase 1 case), one cell cleared, and single-variant unchanged.

The lesson behind the lesson: aggregate metrics over a variant matrix are often nonsense for ship decisions. The same shape of bug would apply to any "≥X total" gate run over multi-cell sweeps — average wall, average tokens, etc. None of those should be summed across variants without thought.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`summarize()`), `benchmarks/maintain_suite/run.py` (call site + print site), `tests/test_acceptance_grader.py` (3 regression tests).

---

### [2026-05-01] Phase 0 — grader had two latent bugs silently inflating PASS counts

**What happened**: While hand-grading `acceptance/v1_temp0_probe_b` (10-fixture, temp=0 deterministic baseline), we found that `result.json` recorded `diff_additions: 0, diff_deletions: 0` on every fixture even when the actual `git diff base..HEAD --shortstat` showed e.g. `+8/-839`. We also found `gates_triggered: []` on every fixture — including a probe_a-era run that genuinely deleted 837 lines of `Game.js` to make `npm test` return rc=0. The strict gates (`destructive_diff`, `role_name_leak`, `placeholder_diff`) that RESUME.md described as "in place" were not firing on any fixture.

**Root cause**: Two independent omissions in `benchmarks/maintain_suite/grade.py`:

- **Bug 1**: `FixtureResult.diff_additions` and `diff_deletions` were declared on the dataclass (defaulting to 0) but never populated anywhere. There was no call to `git diff --shortstat` in the grader at all. The fields existed as placeholders for downstream tooling that never landed.
- **Bug 2**: `apply_strict_gates()` was correctly implemented (lines 329-354) but was **never invoked from `grade_fixture()`**. The function wired up `check_destructive_deletion` / `check_role_name_leak` / `check_placeholder_text` and returned the right shape, but the call site was missing. Only the outcome-conditional gates (`vacuous_test`, `orphan_file`) — which fire post-outcome-pass on a different code path — ever ran.

Both bugs were latent across every prior bake-off in `RESUME.md`'s history table. PASSes that involved destructive diffs or placeholder stubs were silently credited.

**Fix / takeaway**: Three commits.

- **Commit 1** — added `_diff_shortstat(repo_path, base_sha) -> tuple[int, int]` and `_diff_added_text(repo_path, base_sha, changed_files) -> str` helpers near `_changed_files`. Both tolerate empty diff and missing base_sha.
- **Commit 2** — wired `_diff_shortstat()` into `grade_fixture()` so `result.diff_additions` / `result.diff_deletions` reflect real shortstat output.
- **Commit 3** — added the `apply_strict_gates()` call in `grade_fixture()` after the shortstat capture. When any gate fires on a write task, `expected_outcome_passed = False` and the gate detail prepends to `expected_outcome_detail`. The gates are skipped for read-mode tasks (review, summarize) — they shouldn't produce diffs in the first place; that's a separate upstream concern.

Sidecar regrade against `acceptance/v1_temp0_probe_b` after Commit 3: exactly one fixture flipped — `neon-rain-document-modules` (4P→1F) via `destructive_diff` (118 deletions / 22 additions = 5.36×, above the 5.0× threshold). The model rewrote a pre-existing 132-line `ARCHITECTURE.md` to 36 lines of new (task-appropriate) content. The gate firing here is the correct behavior; the fixture itself is suspect because the task wording says "Create" but the file already exists at base_sha. That's a Phase 2 fixture-surgery issue, not a gate-tuning issue. Other 9 fixtures graded the same way before and after.

The deeper lesson: **infrastructure that was "added" can still be unwired**. Bug 2 wasn't a code mistake — `apply_strict_gates` is well-formed. The mistake was the integration glue. RESUME.md saying "Strict gates currently in place" had a piece-by-piece checklist of WHICH gates exist; what it didn't track was whether they were CALLED. For any future "we added gate X" claim: assert in tests that the gate fires from the expected entry point on the expected inputs, not just that the gate function returns the right value when invoked directly.

True real-PASS at temp=0 against the fixed grader: **5/10** (was printing 6/10). Per task type: implement 4/4, document 1/5, manage 0/1.

**Affected files**: `benchmarks/maintain_suite/grade.py` (`_diff_shortstat`, `_diff_added_text`, `grade_fixture` populates diff stats and invokes `apply_strict_gates`), `tests/test_acceptance_grader.py` (10 new tests across the three commits), `scripts/regrade_local.py` (new — sidecar tool for re-grading existing acceptance runs without re-running luxe).

---

### [2026-05-01] Don't read `origin/<branch>` in offline-cache repos — use the local branch ref

**What happened**: While diagnosing probe_b results, we read `git diff base_sha..origin/luxe/implement/<branch>` against the offline fixture cache to inspect each run's pushed diff. Several "PASSes" appeared destructive: `neon-rain-implement-reset-shortcut` showed `+8/-839` (Game.js gutted to 3 lines), `the-game-implement-shuffle-shortcut` showed only Python stubs in a JS repo with a commit message reading "(No changes were made as no findings survived validation.)", `isomer-document-quickstart` showed -148 line README rewrite. We classified 3 of 6 PASSes as false positives, called real-PASS 3/10, and built a strategic argument that the grader was 50% inflated and probe_b was largely gamed.

When the sidecar regrade tool ran the grader directly against the actual local branches in the cache (via `git clone --local`), it reported entirely different diff shapes: `+11/0` for neon-rain-reset (a clean `keydown`/`game:restart` event-bus wiring), `+14/-1` for the-game-shuffle (a real App.jsx keydown handler with INPUT/TEXTAREA guards), `+4/-3` for isomer-quickstart (genuinely terse, but not destructive).

**Root cause**: The fixture cache at `~/.luxe/fixture-cache/<repo>` was originally cloned from GitHub (pre-2026-05-01 offline switch). It still had remote-tracking refs pointing to GitHub: `refs/remotes/origin/luxe/implement/<branch>` carrying old commits from prior runs (some of which WERE genuinely destructive, e.g. the `2026-04-29` neon-rain run that did gut Game.js). After the 2026-05-01 offline switch, `repo_url` in `fixtures.yaml` became a local path, and luxe's `git push` during a run targeted `origin = /local/path`. That push lands in the cache's LOCAL branch namespace — `refs/heads/luxe/...` — not the remote-tracking namespace. Local and remote-tracking refs with the same branch name are now divergent: local = newest run's commit, remote-tracking = stale GitHub state.

`git diff base..origin/<branch>` resolves to the remote-tracking ref. Reading those was reading the historical state, not the current run. `git clone --local <cache>` (used by the sidecar) copies the cache's LOCAL branches to the new clone's `origin/<branch>` namespace — which is why the sidecar gets the right answer while a direct cache read does not.

**Fix / takeaway**: When inspecting the latest run's commit in an offline-cache fixture repo, **use the local branch name, not `origin/<branch>`**. Either:

- `git -C <cache> diff <base>..<branch>` (uses the local ref); or
- `git -C <cache> diff <base>..refs/heads/<branch>` to be explicit.

Generalizing: any time a workflow's "remote" is the same physical disk as the workflow's actor (offline cache, local mirror, etc.), the local-vs-remote-tracking distinction in git collapses in semantics but persists in the ref namespace. Stale `origin/...` refs from a past life of the repo will linger and silently mislead reads. The defense is to either prune them (`git remote prune origin` or `git push --prune`) or to never rely on `origin/<branch>` resolution in such setups.

The investigation cost: this misreading drove a half-day's worth of strategic analysis (a "real-PASS 3/10, grader 50% inflated, the model is gaming aggressively" thread) that turned out to be wrong. The actual probe_b real-PASS is 5/10, the grader's PASSes are mostly genuine, and the model's behavior at temp=0 is significantly better than the misreading suggested. Specifically, the strategic implication for Branch B's 8/10 ship-gate math changes — a 5/10 baseline is closer to the gate than 3/10, and the per-task ceiling at temp=0 is implement 4/4 (already saturated, which obsoletes the `implement_via_cot` overlay).

The methodology rule: **before drawing strategic conclusions from grading data, regrade through the same code path the bench uses**. The sidecar regrade tool (`scripts/regrade_local.py`) is now the canonical way to do this without paying the agent-loop's wall-time cost. Manual `git diff` against a cache repo is fine for code-shape spot checks but is NOT a substitute for sidecar regrade when classifying real PASS vs false positive.

**Affected files**: `scripts/regrade_local.py` (new tool that does this correctly); none of the corrected `git diff` semantics required code changes — it's a workflow change.
