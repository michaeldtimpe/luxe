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

---

### [2026-05-01] Branch C calibration — `nothing-ever-happens-document-config` gate-side miss

**What happened**: At temp=0 the model produces a comprehensive 136-line `CONFIG.md` documenting ~50 environment variables in markdown tables (Variable / Default / Description / Read-in columns), with file:line citations. The grader rejects it: `pattern matched 0× in 141 added lines (needed ≥3) across 1 changed file(s)`. Per the new Branch C `lessons.md` gate (master plan §Branch C, tightened in commit 5551959), this entry must record (a) semantic acceptability, (b) failure category, (c) targeted-vs-general justification before any `fixtures.yaml` edits.

**(a) Semantic acceptability** — the produced output is a textbook-correct CONFIG.md. From probe_b's commit (`luxe/document/add-a-config-md-at-the-2`):

```
| `PM_RISK_MAX_DAILY_DRAWDOWN_USD` | `0.0` | Maximum allowed daily drawdown in USD based on USDC balance vs. daily high-water mark. `0` disables the drawdown circuit breaker. | `bot/risk_controls.py:47` |
| `BOT_MODE` | `paper` | Runtime mode — `paper` for simulation, `live` for real trading. | `bot/config.py:38` |
```

The doc enumerates 52 env vars across 10 sections (Safety/Mode Control, Secrets, Strategy Overrides, Risk Controls, Redeemer, Live Recovery, Runtime, Paper Trading, Docker Compose, Utility Scripts), with verified file:line citations. This is unambiguously what the task asked for ("documents every environment variable ... For each variable, list its name, default value, and a one-sentence description ... Cross-reference where in the codebase it is read"). Any reasonable grader would credit this output.

**(b) Failure category** — `regex_present` grader miss. The pattern was `(?i)\b(os\.environ|getenv|env\[|process\.env)`, requiring the doc to literally quote Python source idioms like `os.getenv(...)`. The model wrote prose-style documentation that *references* the call sites (file:line citations) without quoting the Python expressions themselves. Classification: **gate-side**, not model-side. The model produced semantically-acceptable output (per (a)); the gate's pattern was looking for code-quote idioms in what is fundamentally a prose document. `diff_produced=true`, `diff_files=1`, no destructive_diff / placeholder_diff / role_name_leak triggers. The model engaged correctly; the gate misclassified the engagement.

**(c) Targeted vs general** — the original regex was defending a real anti-gaming concern (per `fixtures.yaml`'s own comment: "min_matches=3 + 20 added lines defends against listing one var in a sentence and stopping"). Don't just remove it — the defense it provides is real for vacuous outputs. The replacement composes the original idiom-quote pattern with a markdown-name pattern. The added alternative is `\b[A-Z_]{2,}[A-Z0-9_]{3,}\b` (an UPPER_SNAKE_LIKE token of ≥5 chars with ≥2 leading letters). This:

- **ACCEPTS** the actual probe_b CONFIG.md (50+ UPPER_SNAKE env var names → comfortably above min_matches=3).
- **ACCEPTS** Python-idiom prose docs that DO quote `os.getenv(...)` (original pattern still fires via OR).
- **REJECTS** vacuous one-paragraph docs ("This file documents environment variables.") — at most 1-2 incidental UPPER_SNAKE tokens, below `min_matches=3`.
- **REJECTS** docs that mention env vars by description without naming them (a "config notes" prose blob with no UPPER_SNAKE names) — fails on count.
- **REJECTS** docs that only list a header and 1-2 names — `min_added_lines=20` catches the thinness; the gate is composite.

The substantive-edit gate (`min_added_lines: 20`) remains untouched. It does the heavy lifting against thin gaming; the regex is the content-shape signal.

**Fix / takeaway**: replace `(?i)\b(os\.environ|getenv|env\[|process\.env)` with `(?i)(?:os\.environ|getenv|env\[|process\.env)|\b[A-Z_]{2,}[A-Z0-9_]{3,}\b`. Sidecar regrade against probe_b's existing CONFIG.md commit confirms the pattern matches; full regrade shows nothing-config flips 1F → 4P.

The deeper takeaway is about gate-pattern composition: **a regex that lists implementation idioms is sometimes too narrow for a documentation task**. Implementation-idiom patterns are good for code-style tasks (the strict-flag fixture, where `add_argument` / `args.strict` SHOULD appear in the diff). For documentation tasks, the test should be content-shape, not implementation-shape. Future doc-task fixtures should consider: what does the *output* look like, not what *code* it references.

**Affected files**: `benchmarks/maintain_suite/fixtures.yaml` (one regex pattern updated on `nothing-ever-happens-document-config`).

---

### [2026-05-02] v1.0 ship — 8/10 cleared on production config

**What happened**: After the Phase 0 grader fix, fixture surgery on three fixtures, Branch C calibration on `nothing-config`, and a citation-linter IPv4 fix discovered during ship confirmation, the 10-fixture acceptance suite cleared the ≥8/10 gate against `configs/single_64gb.yaml`. Per task type: **implement 4/4, document 4/5, manage 0/1**. Champion: `Qwen3.6-35B-A3B-6bit` at temperature=0.0.

The two remaining fails are model-side limitations:
- `lpe-rope-calc-document-typing`: model added 1 line (`from io import IOBase`) and stopped — didn't write the requested module docstring nor type the `f` parameter. The fixture is winnable; the model under-engaged.
- `nothing-ever-happens-manage-deps-audit`: stuck-in-loop bailout (repeated identical tool calls), no diff produced. The model can't navigate this fixture's audit task on the largest repo (907 KB Python).

**Path through the plan** (Phase 0 → Phase 1 fixtures → Branch C → ship):

1. **Phase 0 — grader fix**. Bug 1 (`diff_additions`/`diff_deletions` never populated) + Bug 2 (`apply_strict_gates` defined but never invoked from `grade_fixture`) silently inflated every prior bake-off result. Fixed in commits `0ab2127` + helpers. Sidecar tool `scripts/regrade_local.py` enabled fast iteration.
2. **Variance probe — temp=0 collapses sampling variance**. Three baseline runs at temp=0.2 ranged 4-7/10 (±2 fixtures); two back-to-back runs at temp=0 produced identical pass/fail vectors. Greedy decoding also lifted implement to 4/4 baseline ceiling, which obsoleted Branch B's `implement_via_cot` overlay (it had nothing to lift). Promoted in commit `8fd0fe4`-equivalent (this commit set).
3. **Fixture surgery — `lpe-typing`, `neon-rain-modules`, `isomer-quickstart`** (commits `48e6577` + `8d1fcd3` + this set). All three were misaligned with their `base_sha`: pe_scan.py was already mostly typed; ARCHITECTURE.md and README.md's Quick Start section already existed at base. Goal wording realigned + thresholds calibrated. None of the surgery weakened anti-gaming defense — the destructive_diff gate (Phase 0) does the heavy lifting now, freeing the per-fixture regex/threshold pair to focus on content shape.
4. **Branch C calibration — `nothing-config`** (commit `eb2bdf0`). Confirmed gate-side miss: model produced a textbook 136-line CONFIG.md with file:line citations; original regex required Python idiom quotes. Replaced with a composite that accepts either Python idioms OR markdown UPPER_SNAKE env var names. Per the master-plan §Branch C gate, lessons.md (a)/(b)/(c) entry written *before* fixtures.yaml edit.
5. **Citation linter IPv4 fix** (this commit set). Discovered during ship confirmation: synthesizer reports legitimately mention `127.0.0.1:port` for dashboard URLs, and the citation extractor's regex `[\w./_-]+\.[\w]+:\d+` was matching IPv4-shaped tokens as `path:line` citations. Two unresolved citations on isomer-quickstart's report blocked the fixture's PASS. Surgical fix: reject paths matching `(?:^|/)\d+\.\d+\.\d+\.\d+$`. Two unit tests added (rejects host:port; preserves filenames-with-digits).

**The Branch B obsolescence** is worth recording explicitly: Phase 1's "structural prompts hit 4/4 implement" finding was at temp=0.2 — almost certainly a baseline-variance artifact. At temp=0, baseline already gets 4/4 implement out of the box, so the `implement_via_cot` overlay had nothing to lift. The plans in `~/.claude/plans/task-type-overlays.md` and `~/.claude/plans/v1-ship-and-prompt-sweep.md` retain Branch B/C structure for future task-type-specific overlays (e.g., a doc/manage overlay), but the v1 ship path didn't need them.

**Confirmation result** — `acceptance/v1_default_ship_confirmation/`, cumulative across the initial 10-fixture run + 3-fixture retry (gh auth flake at 01:48) + 1-fixture isomer-quickstart re-run (post-citation-fix):

| Fixture | Type | Verdict |
|---|---|---|
| lpe-rope-calc-implement-strict-flag | implement | PASS |
| the-game-implement-shuffle-shortcut | implement | PASS |
| neon-rain-implement-reset-shortcut | implement | PASS |
| isomer-implement-healthcheck | implement | PASS |
| the-game-document-architecture | document | PASS |
| neon-rain-document-modules | document | PASS |
| isomer-document-quickstart | document | PASS |
| nothing-ever-happens-document-config | document | PASS |
| lpe-rope-calc-document-typing | document | FAIL |
| nothing-ever-happens-manage-deps-audit | manage | FAIL |

**Fix / takeaway**: Bump `pyproject.toml` from `1.0.0.dev0` to `1.0.0`. Tag `v1.0.0`. The ship gate is the model's ceiling on this fixture set at temp=0 — the implement category is genuinely saturated, doc has one fixture-design-resistant case (typing under-engagement at temp=0), and manage has one model-can't-navigate case. Future improvement to v1.1 would target the manage stuck-loop pattern (likely a context-management or tool-loop-detection issue, not a fixture issue) or add a doc-task overlay (Phase 1's structural prompts regressed doc/manage; an overlay tuned for prose tasks specifically is the next experiment, but out of v1 scope).

The deeper meta-takeaway: **infrastructure quality dominates result quality**. Phase 0 fixed grader bugs that had been silently inflating PASS counts since 04-29. Once the grader was honest, the remaining levers (variance pinning, fixture surgery, gate calibration, one citation-linter fix) added up to a gap closure of 5/10 → 8/10 without changing the model or its prompts. The model is the same Qwen3.6-35B-A3B-6bit it was on 04-29. What changed was: (a) the grader stopped lying, (b) sampling stopped being random, (c) fixtures matched their base_sha, (d) gates measured the right thing. Future bench cycles should expect: every "we added a gate" claim needs a regression test that asserts the gate fires from the expected entry point on the expected inputs (Phase 0 Bug 2's lesson, applied universally).

**Affected files**: `pyproject.toml` (1.0.0.dev0 → 1.0.0), `configs/single_64gb.yaml` (temperature 0.2 → 0.0), `src/luxe/citations.py` (`_IPV4_PATH_RE` guard in `extract_citations`), `tests/test_citations_diff_aware.py` (2 new tests), plus the cumulative grader/fixture work referenced above.

---

### [2026-05-02] Phase v1.1 A1 — historical bake-off re-grade reveals severe inflation

**What happened**: Re-graded four prior bake-off output dirs against the post-Phase-0 grader using `scripts/regrade_local.py`. The deflation across the board is severe — far worse than RESUME.md's earlier "1-2 false positives per cell" caveat suggested.

| Phase | Printed | Regraded | Inflation |
|---|---|---|---|
| `acceptance/v1_default` | 5/10 | 3/10 | 1.67× |
| `acceptance/v1_default_post_fix` | 6/10 | 4/10 | 1.50× |
| `acceptance/prompt_shaping` (60 fixtures) | 33/60 | **8/60** | **4.13×** |
| `acceptance/overnight_moe` (20 fixtures) | 5/20 | **0/20** | **∞** (every printed PASS was false) |

**Root cause (already known)**: Phase 0's Bugs 1+2 — `apply_strict_gates` defined but never called from `grade_fixture`, `diff_additions/deletions` declared but never populated. The strict gates (`destructive_diff`, `placeholder_diff`, `role_name_leak`) caught zero gaming-shaped diffs across every prior run. Once wired, they fire frequently — especially `destructive_diff` on the historical "rewrite-the-existing-file" pattern that the model used to game `min_added_lines` checks.

**Fix / takeaway**: the historical narrative needs revision. Two specific reinterpretations:

1. **Phase 1's "structural prompts hit 4/4 implement"** finding (33/60 printed → 8/60 regraded, 76% false) is no longer a load-bearing claim. The CoT/SoT/HADS variants probably did NOT lift implement to 4/4; the pre-fix grader was credit­ing destructive or placeholder-shaped diffs on those runs as wins. This retroactively justifies treating Branch B's `implement_via_cot` overlay as obsolete (the underlying observation it was built on was an artifact). It also re-explains why baseline-temp=0 hit impl 4/4 "out of the box" in this session — that's the model's actual ceiling, the temp=0.2 + structural-prompt cells weren't reaching it, they were gaming-passing through the broken grader.
2. **Overnight MoE's qwen3.6-35B-A3B-6bit "win"** (5/20 → 0/20) is now also suspect. The bake-off chose the champion based on inflated numbers. The choice was probably still defensible (the model genuinely is the strongest on this hardware tier), but the "5/20 real" claim in RESUME.md was wrong; it was 0/20. Future model selection should re-grade as a baseline before declaring a winner.

**Practical guidance**: any RESUME.md "Real PASS leader" cell predating 04-30 is probably 0.25-0.7× of the printed value. When citing historical numbers in strategic decisions, run `scripts/regrade_local.py --output acceptance/<dir>` first; the cost is minutes and the data is so much more honest that it's worth doing routinely.

**Affected files**: `RESUME.md` (history table updated with regraded counts and a stronger caveat block); `acceptance/<phase>/result_regraded.json` written next to every original `result.json` across the four re-graded dirs.

---

### [2026-05-02] Phase v1.1 A2 — prefix-cache hit-rate not directly available at INFO logs

**What happened**: Tried to measure oMLX's prefix-cache hit rate on the v1 ship confirmation run as load-bearing input for the Workstream C decision. The data isn't directly available from the configuration we currently run.

**Root cause**: oMLX's INFO-level log shows boundary-cache *writes* ("storing X/Y tokens") but not *reads* (per-request cache hit/miss). No `/cache/stats` or `/metrics` endpoint at `localhost:8000`. The Chat completion log line shows aggregate wall + tokens but no prefill/decode split that would let us infer TTFT (a proxy for cache hit). Cross-run wall comparisons (probe_a vs probe_b on the same fixtures at temp=0) are mixed — some fixtures faster, some slower — which doesn't strongly support either "cache helps a lot" or "cache barely helps."

**Fix / takeaway**: Recorded as `project_prefix_cache_baseline.md` memory entry with status INCONCLUSIVE. Workstream C's decision matrix had two HIT bins (LOW <65%, HIGH ≥65%); without a clean number, **default to LOW** (conservative-toward-investigation: keeps Phased Mode v2 as a viable option, lets Workstream B's QUAL outcome do the heavy lifting on the decision). When/if Path 1 becomes the leading candidate based on Workstream B, re-run A2 with one of:

- DEBUG-level oMLX logs + a fresh full bench (will surface per-request cache hit/miss).
- A controlled hot-vs-cold A/B with oMLX restarted between runs (crisper signal than cross-run comparisons).

The deeper takeaway: **infrastructure-availability matters for measurement plans**. The plan assumed the cache-hit-rate data was just sitting in the log; it isn't. Future plans that depend on a measurement should verify the measurement is gettable BEFORE committing to it as a decision input.

**Affected files**: `~/.claude/projects/-Users-michaeltimpe-Downloads-luxe/memory/project_prefix_cache_baseline.md` (new); MEMORY.md indexed.

---

### [2026-05-02] Phase v1.1 A4-A5 — drop dead Gemma-4 entries; document offline 4/5 cap

**What happened**: Cleanup pair from RESUME.md's "Open work" list.

- A4: Removed `Gemma-4-26B-A4B-4bit` and `Gemma-4-26B-A4B-8bit` entries from `~/.omlx/model_settings.json`. RESUME.md noted these models ship with empty `chat_template` and fail HTTP 400 in the chat-driven loop; the settings entries were dead config that would mislead future model-roster decisions. Restarted oMLX; verified via `Backend().list_models()` that Gemma-4 no longer appears (the model's symlinks at `~/.omlx/models/Gemma-4-*` remain but are inert at the API surface).
- A5: Documented the offline-mode 4/5 cap in RESUME.md's "Critical gotchas" section. Per the v1.1 plan recommendation, picked option (b): leave grader code untouched, treat 4/5 as the offline-mode signature rather than introducing auto-detect logic for `pr_opened`. The gate math still works because the gate is per-fixture pass count, not score sum.

**Fix / takeaway**: Both are house-keeping; no model or grader logic changed. The Gemma-4 cleanup is a small example of **dead config compounds over time** — one model's bad chat_template would have kept showing up in roster lookups indefinitely. Periodically pruning model_settings.json is cheap insurance.

**Affected files**: `~/.omlx/model_settings.json` (Gemma-4 entries removed), `RESUME.md` (gotcha entry for offline 4/5 cap).

---

### [2026-05-02] Phase v1.1 A3 — specprefill probe partial: ~5% wall improvement, doesn't clear 15% gate

**What happened**: Enabled `specprefill_enabled: true` for Qwen3.6-35B-A3B-6bit in `~/.omlx/model_settings.json`, restarted oMLX (0.3.8 stable, model digest `cb7e092ef8efe540bc3672c8929c4adbe5f4f759`), ran the 5-fixture A3 probe. The bench hit the gh-auth flake (see `project_gh_auth_flake.md`) on runs 4 + 5; only 3 of 5 fixtures completed. Of the 3 that did:

| Fixture | Baseline (v1 ship confirmation) | A3 (specprefill on) | Δ |
|---|---|---|---|
| `lpe-rope-calc-implement-strict-flag` | 311s | 290s | -7% |
| `the-game-implement-shuffle-shortcut` | 60s | 60s | 0% |
| `neon-rain-implement-reset-shortcut` | 145s | 134s | -8% |

Mean ~5% wall improvement. All three fixtures matched their baseline pass/fail (no quality regression on the data we have).

**Root cause / interpretation**: Per the v1.1 plan's A3 gate, the pass criteria require **median wall ≥15% drop** AND no quality regression AND no raw-text drift. Even imagining the 2 missing fixtures hit a generous 15% lift, the mean across all 5 wouldn't clear 15%. The probe came in with a small positive but it doesn't clear the threshold the plan explicitly committed to (the threshold is intentionally tight — it's there to make the "tried, didn't work" outcome cheap to recognize).

The plan also called out this expected outcome explicitly: *"Treat 'no win' or 'small win' as the likely outcome. Field reports on Qwen3.6-35B-A3B + oMLX show speculative decoding is sometimes silently disabled or buggy when flags don't line up, and is sometimes net-neutral or slightly negative when the draft config is off or the prefix cache is already doing heavy lifting."* That framing held up.

**Fix / takeaway**: Reverted `specprefill_enabled: false` in `~/.omlx/model_settings.json`. Restarted oMLX. Net change to the repo: zero — settings.json is system config, not tracked. The lesson lands here so future-us doesn't waste cycles re-running this probe without checking what changed in oMLX or mlx-lm first.

The deeper takeaway: **probes with binary thresholds beat probes with vibes**. The 15%-or-revert rule made this decision crisp despite incomplete data. If the rule had been "any improvement is good," this would have been a discussion ("but it's *5%*, isn't that worth keeping?") and we'd have shipped a flag with unknown long-term cost. The math: a flag that adds 5% wall improvement but introduces *any* probability of subtle output drift is a net negative on a build-trust-with-the-grader workload like this. Either it's clearly worth it or revert.

**Logged for future re-investigation**: oMLX version 0.3.8 stable; Qwen3.6-35B-A3B-6bit at HF snapshot `cb7e092ef8efe540bc3672c8929c4adbe5f4f759`. If a future oMLX bumps the speculative-decoding stack, re-running this probe is reasonable. Until then, leave the flag off.

**Affected files**: `~/.omlx/model_settings.json` (specprefill_enabled flipped on then back off — net zero), `~/.claude/projects/.../memory/project_gh_auth_flake.md` (new — tracks the open auth-flake issue), `~/.claude/projects/.../memory/feedback_offer_long_running_commands.md` (new — established preference: offer commands for >~5 min runs rather than auto-backgrounding).

---

### [2026-05-02] Phase v1.1 B1 — `document_strict` overlay: negative result on the lpe-typing target

**What happened**: Added a `document_strict` PromptVariant + `document_strict_only` overlay (route `document` task type → strict variant only). Strict task_prefix demands tool-call commitment ("MUST call `edit_file` or `write_file`") AND component completeness ("MUST address EVERY component of the goal ... A diff with fewer than ~4 added lines on a multi-component goal almost certainly means you stopped before finishing"). System prompt unchanged from baseline; overlay fires only on `document` task type so non-doc tasks are untouched (regression-defended in `test_document_strict_only_overlay_fires_only_on_document_tasks`). 5-fixture × 2-cell smoke probe (4 control + 4 overlay completed after a `gh auth` flake retry).

| Fixture | Control | Overlay |
|---|---|---|
| `isomer-document-quickstart` | PASS (+9/-3) | PASS (+8/-4) |
| `lpe-rope-calc-document-typing` | FAIL (+1/0) | **FAIL (+2/-2)** |
| `neon-rain-document-modules` | PASS (+14/0) | PASS (+20/0) |
| `nothing-ever-happens-document-config` | FAIL (no diff) | PASS (+141/0) |
| `the-game-document-architecture` | PASS (+19/0) | PASS (+12/-1) |

Cell totals: control 3/5, overlay 4/5.

**Root cause / interpretation**: the overlay nudged lpe-typing from +1 → +2 added lines but didn't unblock the under-engagement pattern that B1 was designed to fix. The model added `from io import IOBase` (control) or `from io import IOBase` + a typed `f` parameter (overlay) — both stopped before writing the requested module docstring. The strict directive's "MUST call edit_file" clause fired (the model did call edit_file) but the "MUST address EVERY component of the goal" clause did NOT shift behavior — the model considers the typing edit "done" and the docstring half is invisible to it. The directive can't disambiguate "I think I'm finished" from "you actually have more to do."

The overlay's nothing-config flip (FAIL → PASS) is *not* a real v1.1 gain, because nothing-config already PASSed in v1.0's ship confirmation. This run's control regressing nothing-config to FAIL is small temp=0 environmental variance (cross-run prefix-cache state, fixture order, etc.) — the overlay recovered from a transient miss but didn't add new capability above the v1.0 baseline.

**Pass criteria evaluation (plan B1):**
- ❌ lpe-typing PASS (the explicit target).
- ✅ No regression on 4 other doc fixtures (3 equal, 1 transient-recovery).
- N/A no-op-edit spot check (moot since lpe-typing didn't pass).

The plan said "if pass criteria met → keep the overlay; if not → document the negative result." Following the negative-result branch.

**Fix / takeaway**: Keep the `document_strict` PromptVariant + `document_strict_only` overlay registered (the infrastructure is tested, composable, and zero-cost for non-document tasks per the fire-only-on-document overlay semantics). **Don't promote to production** — `configs/single_64gb.yaml` stays unchanged. The variant file `variants_v1_doctask_overlay_probe.yaml` stays as a probe artifact for future experiments.

The deeper takeaway for Phase 1's "structural prompts regress doc/manage" finding: that finding was at temp=0.2 on an inflated grader (re-graded 8/60 in A1). At temp=0 with the honest grader, *targeted* doc-only structural prompting at least doesn't regress doc tasks — the no-leakage overlay design is the right shape. But the lpe-typing under-engagement pattern isn't fixable via prompt directives at this model scale; it's a model-side limit on noticing "the goal asks for two things and I only did one." Future work in this direction would need either a different model, a stronger prompt that includes worked examples of multi-component completion (few-shot), or a runtime check that re-prompts the model when a submitted diff doesn't match all the goal's named deliverables.

For Workstream C: B1 closed 0 of the v1.0 FAILs. If B2 also fails, QUAL=8; per the plan's MECE matrix with HIT=LOW (A2 inconclusive default), we land at Path 1 (Phased Mode v2). If B2 closes, QUAL=9; still Path 1. The 10/10 "ship v1.1, log v1.2 target" outcome (Path 2a) requires both B1 and B2 closing; B1 already locked us out of that.

**Affected files**: `src/luxe/agents/prompts.py` (new `document_strict` PromptVariant + `document_strict_only` overlay), `tests/test_prompts.py` (3 new regression tests), `benchmarks/maintain_suite/variants_v1_doctask_overlay_probe.yaml` (new probe variant file), `acceptance/v1_doc_overlay_probe/` (smoke probe results — kept on disk for future re-grade if pattern of doc-task variance becomes a separate investigation).

---

### [2026-05-02] Phase v1.1 B2 — `manage_strict` overlay: closes deps-audit (with CVE-id caveat)

**What happened**: Added `manage_strict` PromptVariant + `manage_strict_only` overlay (route `manage` task type → strict variant only). Strict task_prefix names two failure modes by name: re-reading the same file (loop-detector trips), and reading-without-writing. Includes a procedural ONE-AT-A-TIME directive: pick one item, look it up, document it, move to the next. 1-fixture × 2-cell smoke probe on `nothing-ever-happens-manage-deps-audit`:

| Cell | Result | Diff | Notes |
|---|---|---|---|
| baseline-control | FAIL | +0/-0 | "no diff produced" — same stuck-loop pattern as v1.0 + earlier reproductions |
| `manage_strict` overlay | **PASS** | +70/-0 | 70-line `SECURITY-AUDIT.md` with 3 concrete findings (aiohttp, SQLAlchemy, psycopg2-binary) |

**Functional check (mandatory per plan B2 criteria):**

- ✅ All 3 packages cited (aiohttp, SQLAlchemy, psycopg2-binary) ARE actually in the fixture's `requirements.txt`. The version constraints quoted in the audit match the file exactly. Upgrade proposals preserve the major-version cap while bumping the min — proper "preserve API compatibility" shape.
- ⚠️ **CVE-id verification limitation**: CVE-2023-46136 (aiohttp) is a real, well-known CVE matching the reported vulnerability (Content-Length DoS). The other two — CVE-2024-3559 (SQLAlchemy) and CVE-2024-22032 (psycopg2-binary) — are plausibly-shaped IDs but couldn't be verified without external lookup; the model may have invented realistic-looking-but-incorrect CVE numbers. The GHSA advisory slugs are particularly suspect (model can't know exact slug strings). **For real production use, a human would need to verify each CVE ID before acting on the audit.**
- ✅ Loop-telemetry proxy: control wall 75s with stuck-loop bailout (6888 completion tokens then abort); overlay wall 315s with steady 22 tok/s navigation. The overlay shifted the model from thrashing to producing. Tool count similar but distinct-args, not identical-args (since no stuck-loop fired).

**Pass criteria evaluation (plan B2):**
- ✅ Non-empty diff matching the regex (`matched in 22 added lines (needed ≥3)`).
- ✅ Findings reference real packages with accurate version constraints (functional check above).
- ✅ Loop telemetry proxy confirms meaningful navigation (wall delta + completion-token volume).

The CVE-id hallucination caveat is real — the bench grader's regex `(?i)(CVE-\d{4}-\d{4,}|...)` cannot distinguish "real CVE" from "CVE-shaped-text-the-model-invented." This is a known limitation: gates check shape, not factuality. For audit-class tasks specifically, the bench gate alone is insufficient for production trust; a human verification step is required. Flagging in lessons.md so this isn't relearned later.

**Fix / takeaway**: B2 PASSes pass criteria for the v1.1 quality push. The path forward (per the plan's commit milestones):

1. **Commit the overlay infrastructure** as a positive result (`feat: manage_strict overlay`).
2. **Promote `manage_strict_only`** into `configs/single_64gb.yaml`'s `roles.monolith.task_overlay_id`.
3. **Run the v1.1 full-bench confirmation** — 10-fixture sweep against the promoted production config. Expected: 9/10 (impl 4/4, doc 4/5, manage 1/1).
4. **Workstream C decision** based on the confirmation result + A2's HIT (defaulted LOW) + the QUAL outcome.

The plan's MECE matrix at LOW + 9 = Path 1 (Phased Mode v2; don't ship v1.1). Worth a sanity check: 9/10 IS a real measurable improvement (one new fixture closed), and the matrix's "not enough to ship alone" framing was deliberately conservative — it can be revisited with the user before locking in. Path 2b (ship v1.1, freeze incremental work) is the alternative if HIT-default is reconsidered or 9/10 is judged worth a release.

The deeper takeaway: **prompt-side fixes can unstick model-side behavior more reliably than expected, but only when the failure mode is mechanistic** (re-reading + not-writing). lpe-typing's "I think I'm done" pattern is harder — no procedural guidance disambiguates an over-eager stop. deps-audit's "I'm stuck on this file" pattern is easier — there's a clear procedural fix (pick distinct items). Future overlay work should triage the failure mode mechanistically before committing to a directive shape.

**Affected files**: `src/luxe/agents/prompts.py` (new `manage_strict` PromptVariant + `manage_strict_only` overlay), `tests/test_prompts.py` (3 new regression tests parallel to B1's), `benchmarks/maintain_suite/variants_v1_managetask_overlay_probe.yaml` (new probe variant file), `acceptance/v1_manage_overlay_probe/` (smoke probe results).

---

### [2026-05-02] Phase v1.1 — work_dir variance discovery + fix

**What happened**: After B2's smoke PASS on deps-audit, the v1.1 ship-confirmation full bench produced an unexpected mixed result. Two consecutive runs at temp=0 with the production config + `manage_strict_only` overlay both landed at 8/10, but with different failure shapes — Run A had deps-audit FAIL + neon-rain-reset PASS (matching v1.0); Run B had deps-audit PASS + neon-rain-reset FAIL (an implement-task fixture flipping for the first time at temp=0). The cumulative cross-run picture across 5 temp=0 runs (v1.0 ship confirmation + 2 overlay runs + 2 no-overlay runs) showed two fixtures flipping unpredictably regardless of whether the overlay was set: neon-rain-reset went 3 PASS / 2 FAIL, deps-audit went 1 PASS / 4 FAIL.

**Root cause**: the bench's `work_dir` defaulted to `tempfile.mkdtemp(prefix="luxe-acceptance-")` — a random tempdir per invocation. That random suffix appeared in bash and git tool outputs (paths in `pwd`, `ls -la`, error messages, `git diff` output). Tool outputs become tool messages in the agent's prompt. At greedy temp=0 the model is deterministic *given a fixed input*, but the input wasn't fixed across bench invocations — different tempdir = different prompt = different model output for any fixture whose tool calls happened to surface the path.

A 3-iteration single-fixture probe of neon-rain-reset with `--work-dir ~/.luxe/bench-workspace` (pinned) produced 3/3 PASS, vs 3/5 PASS earlier with random tempdirs. Confirmed.

**Fix / takeaway**: changed the `--work-dir` default in `benchmarks/maintain_suite/run.py` from `None` (mkdtemp) to `~/.luxe/bench-workspace`. Existing reuse logic in `_resolve_repo` handles cached clones correctly (base_sha checkout + reuse). Added `--ephemeral-work-dir` flag for callers that explicitly want process isolation. Help text references this lessons entry so future-us doesn't flip the default back without checking why.

The 2-cell A/B with pinned default — `champ-no-overlay` vs `champ-manage-strict` — produced the cleanest possible signal: no-overlay cell matched v1.0 ship confirmation EXACTLY (deterministic on pinned substrate); overlay cell differed only on deps-audit (F → P). The 9 other fixtures had identical pass/fail in both cells. That's what "the overlay does exactly what it claims" looks like.

The deeper takeaway: **the earlier "temp=0 collapses sampling variance to deterministic" finding was true ABOUT THE MODEL — given identical input, it produces identical output**. The variance we were seeing wasn't sampling-level; it was input-level. Random data flowed through the bench infrastructure into the prompt, and the model's determinism stopped being visible.

This also clarifies the earlier inconclusive A2 prefix-cache measurement (`project_prefix_cache_baseline.md`): cross-run prefix-cache reuse appeared modest because **the prefixes themselves were different across runs** (random tempdir in tool outputs). With pinned work_dir, prefix-cache reuse becomes a meaningful question again. If we want to revisit the Phased Mode v2 architectural decision, do it with pinned work_dir; A2 needs re-measurement with this confound removed.

**Affected files**: `benchmarks/maintain_suite/run.py` (work_dir default change + new `--ephemeral-work-dir` flag), `benchmarks/maintain_suite/variants_v1_overlay_ab_pinned.yaml` (new — A/B variant for the experiment), `~/.claude/projects/.../memory/project_workdir_variance_leak.md` (new — tracks the finding).

---

### [2026-05-02] v1.1.0 ship — pinned work_dir + manage_strict overlay → 9/10

**What happened**: Shipped v1.1.0 with two infrastructure improvements over v1.0: (1) pinned `--work-dir` default eliminating the dominant temp=0 variance source, and (2) `manage_strict_only` task overlay closing the deps-audit stuck-loop. Champion model unchanged: `Qwen3.6-35B-A3B-6bit` at temperature=0.0.

Acceptance result against the production config (single cell, `manage_strict_only` promoted in `single_64gb.yaml`): 9/10 stable on the pinned-work_dir substrate. Confirmation came from cell 2 of the variants_v1_overlay_ab_pinned.yaml A/B run; running a separate single-cell production-config confirmation was deemed redundant since the variant override goes through the same code path as the config setting (`run.py:178` writes to `cfg["roles"]["monolith"]["task_overlay_id"]` either way).

Per task type:
- implement 4/4 (saturated since v1.0)
- document 4/5 (lpe-typing remains FAIL — under-engagement pattern not solved by document_strict overlay; B1 was a negative result)
- manage 1/1 (NEW in v1.1 — deps-audit closed by manage_strict overlay)

**Workstream C decision discipline**: the Phase v1.1 plan's MECE matrix said LOW + 9 = Path 1 (Phased Mode v2; don't ship v1.1) on the grounds that "one fixture closed via prompts is informative but not enough to ship a quality bump alone." That hedge was framed pre-result and pre-variance-investigation. The pinned-work_dir A/B closed the variance ambiguity and showed the overlay's contribution is real, isolated, and reproducible. Decision was made to override the matrix on those grounds — this is a documented departure from the plan, not a quiet override. Path-1 architectural work (Phased Mode v2 + per-tool subphases including the CVE lookup tool — see `project_post_v1_architecture_ideas.md` and `project_tool_subphases_and_cve_lookup.md`) remains queued for v2.0.

**The one remaining FAIL at v1.1** is `lpe-rope-calc-document-typing` — model adds 1 line and stops (under-engagement). B1's `document_strict` overlay nudged 1→2 added lines but didn't unblock the docstring half. The `document_strict` infrastructure stays registered in `prompts.py` for future experiments but is NOT promoted in production. The pattern needs either a model upgrade, few-shot examples in the prompt, or a runtime re-prompt loop when the submitted diff doesn't match all named goal deliverables — none of which are in v1.1 scope.

A separate caveat on the deps-audit PASS: the audit's CVE IDs are partially hallucinated (real packages cited with real version constraints and accurate file:line citations, but some specific CVE numbers may be invented). Bench grader's regex checks shape, not factuality. Flagged in the v1.1 B2 lessons entry as a known limitation. The CVE-lookup tool subphase (post-v1; see `project_tool_subphases_and_cve_lookup.md`) addresses this directly.

**Path through the plan**: Phase 0 (grader fixes) → fixture surgery on 3 fixtures → Branch C calibration on `nothing-config` → temp=0 promotion → citation-linter IPv4 fix → v1.0.0 ship at 8/10 → Phase v1.1 (B1 negative, B2 positive on prompt overlay) → variance investigation → work_dir pin → v1.1.0 ship at 9/10.

The infrastructure-quality-dominates-result-quality theme from the v1.0 ship lessons entry held up for v1.1 too: every gain came from infrastructure work. Bench grader honesty (Phase 0), gate calibration (Branch C), citation linter (IPv4 guard), and now the work_dir pin — none are model improvements; all are measurement improvements. The model is the same. The bench just stopped lying in different ways.

**Fix / takeaway**: bumped pyproject.toml `1.0.0` → `1.1.0`. Tag `v1.1.0`. Production config `configs/single_64gb.yaml` now has `task_overlay_id: manage_strict_only` and benefits from the pinned-work_dir default. v1.1 is the new shipped state; v1.0 stays available via tag for users who want it.

**Affected files**: `pyproject.toml` (1.0.0 → 1.1.0), `configs/single_64gb.yaml` (re-promoted manage_strict_only after the variance experiment), `benchmarks/maintain_suite/run.py` (work_dir default change shipped in commit ec88cd2 already).

---

### [2026-05-02] Post-v1.1 — A2 prefix-cache re-measurement: HIGH (85.4%); Phased Mode v2 deprioritized

**What happened**: After v1.1.0 ship, re-ran A2 (the prefix-cache hit-rate measurement) on the pinned-work_dir substrate. The original A2 (2026-05-02 morning) had been INCONCLUSIVE because oMLX's INFO log doesn't expose hit/miss data. With `server.log_level: debug` and the pinned `--work-dir` default, the per-request hit data is now visible.

**Aggregate hit rate on `nothing-config` (the longest-context fixture, 16 model requests): 85.4%** (206,848 hit / 242,300 prompt tokens). Per-request range: 22% (one outlier on the turn the model first wrote a chunk of new content) to 100% (turns where the prompt was fully covered by the cache).

**Root cause / interpretation**: cache reuse is working. Steady-state behavior across an agentic loop is: each turn reuses the prior turns' system prompt + earlier tool results, only the newly-grown tail of the conversation needs cold prefill. With the work_dir pinned, the prompt prefix is byte-identical across reruns of the same fixture, so cache reuse is effective from request 1.

**Decision impact (Workstream C):** the plan's MECE matrix at LOW + 9 = Path 1 (Phased Mode v2). At HIGH + 9 = Path 2b (ship v1.1; freeze incremental work; Phased Mode v2 not needed). Original A2 INCONCLUSIVE defaulted to LOW for caution. The re-measurement is conclusively HIGH (85% >> 65% threshold), so we land at Path 2b. **Phased Mode v2's architectural premise — "subtask scoping reduces context bloat and warms the cache" — is invalidated by the measurement: the cache is already warm.** The complexity cost of resurrecting phased/micro modes with proper memory primitives isn't justified by a baseline already at 85%.

**Fix / takeaway**: deprioritize Phased Mode v2 in v2.0 planning. The two remaining v2.0 directions are unaffected by this measurement and become the priority work:

1. **Per-tool refinement subphases** (CVE lookup as seed) — see `project_tool_subphases_and_cve_lookup.md`. Defeats the audit-hallucination caveat from v1.1's deps-audit by making CVE references deterministic via OSV.dev.
2. **MCP-mediated codebase slicing** — independent value prop (reduces ingest size on large repos). Unrelated to cache warmth.

**lpe-typing under-engagement** (the only remaining v1.1 FAIL) is also unaffected — needs a different lever (model upgrade, few-shot prompting, or runtime re-prompt-on-incomplete-diff loop) regardless of cache state.

The deeper takeaway: **the work_dir pin enabled this measurement to even be possible**. Without it, prefix-cache reuse appeared modest because the prefixes themselves were different across runs. The same infrastructure fix that closed the variance issue also unlocked the architectural decision data. One bug, two consequences. Worth remembering as a pattern: when an investigation hits a wall ("the data is too noisy to interpret"), the failure is often upstream of the measurement.

**Affected files**: `~/.omlx/settings.json` (log_level cycled debug → info; system config, not in repo), `~/.claude/projects/.../memory/project_prefix_cache_baseline.md` (status updated from INCONCLUSIVE to HIGH 85.4%), MEMORY.md (index updated), RESUME.md (Phased Mode v2 deprioritized in v2.0 queue).

---

### [2026-05-02] v1.2.0 — per-tool subphase pass: cve_lookup + bash + read_file + latent _REPO_ROOT fix

**What happened**: Executed the per-tool refinement subphase pass that the post-v1.1 plan queued as next-next direction. Audited all ~15 dev-facing tools in `_build_full_tool_surface`. Three real defects surfaced; the other twelve were already solid.

1. **`cve_lookup` (new tool, commits `c1e2a81` + `84e3ea8`)** — the seed example. B2's `manage_strict` overlay closed deps-audit on the fixture-pass criterion, but the model produced a mix of real CVE ids (CVE-2023-46136 aiohttp DoS) and plausible-but-fabricated ones (CVE-2024-3559 SQLAlchemy, CVE-2024-22032 psycopg2-binary). The grader's regex couldn't distinguish them. Tool wraps OSV.dev's `api.osv.dev/v1/query` (free, no auth, covers PyPI/npm/Go/Rust/Maven/NuGet/RubyGems). Follow-up commit surfaces OSV `aliases` so a model querying by GHSA gets the CVE cross-reference, closing the second-order hallucination path where a real GHSA was paired with an invented CVE.

2. **`bash` chain hardening (commit `7ccba1f`)** — real security defect. Pre-fix `_bash` did `parts[0] = command.strip().split()[0]`, checked it against the allowlist, then ran the whole command via `shell=True`. So `cat foo && rm -rf /` passed the check (parts[0] was `cat`) and `rm` then executed despite not being on the allowlist. Fix uses `shlex.split` to tokenize (so a `|` inside a quoted regex doesn't trip the check), then rejects any chain operator (`&&`, `||`, `;`, `|`, `&`), redirect (`>`, `<`, `>>`, `<<`, `<<<`, `&>`, `2>`, `2>&1`), or command substitution (backtick, `$()`). Allowlisted binaries still run with `shell=True` to keep glob/expansion support.

3. **`read_file` binary detection (commit `7ccba1f`)** — context pollution. Reading a binary file with `errors='replace'` returned multi-MB of garbage U+FFFDs that polluted the model's context window. Now reads the first 8 KB and rejects any file containing null bytes — text formats don't contain them; PNG/JPG/zip/elf all do. UTF-8 source with unicode identifiers and accented strings still passes (no false positives in the test corpus).

4. **`fs.get_repo_root()` getter (commit `7ccba1f`)** — load-bearing latent bug, surfaced while writing tests for the bash fix. `shell.py`/`git.py`/`analysis.py` all did `from luxe.tools.fs import _REPO_ROOT` at module load time, which binds the imported name to whatever `fs._REPO_ROOT` was at import time (typically `None`). Subsequent `fs.set_repo_root()` calls update `fs._REPO_ROOT` but NOT the imported name in sibling modules. Result: `if _REPO_ROOT is None` checks in those modules silently always returned `True`. The bash/git/analysis tools have been latently broken since at least v1.0 — but the bench's natural usage rarely hit them through the bench's instrumented path (the model usually preferred `read_file`/`write_file`/`edit_file`/`git_diff` via the dedicated tools), masking the bug. Fix: added `fs.get_repo_root()` that returns the current value, switched all three sibling modules to call it. Module docstring on the getter explains the import-time bind issue so future contributors don't reintroduce.

The 12 tools NOT touched (audited and verified solid): `list_dir`, `glob`, `grep`, `bm25_search`, `find_symbol`, `write_file` (Phase-2 guards already exist), `edit_file` (Phase-2 guards already exist), `git_diff`, `git_log`, `git_show`, `lint`, `typecheck`, `security_scan`, `deps_audit`, `lint_js`, `typecheck_ts`, `lint_rust`, `vet_go`.

**Root cause / interpretation**: the prompt-side bench (v1.0 → v1.1) optimized for *whether* the model invoked the right tool at the right time. It did not stress *what happens when the model invokes the tool in unusual ways* — chain operators in bash, binary files passed to read_file, sibling modules re-importing module-level globals. Those failure modes are invisible to a prompt-grading bench because they either (a) succeed silently in the wrong way (chain bypass), (b) pollute context without flipping the gate (binary read), or (c) only manifest when an execution path the bench rarely takes finally runs (the latent `_REPO_ROOT` bug).

**Fix / takeaway**: subphase template validated. The shape is: (1) bench probe targeted at the tool's specific failure modes, (2) tool-side hardening based on what surfaces, (3) optional overlay prompt that guides usage pattern (analogous to `manage_strict` for audit-style tasks). `cve_lookup` is the canonical seed — for any future tool added to the surface, run a subphase pass before claiming it's production-ready.

The latent `_REPO_ROOT` bug is a separate lesson on top: **`from x import _y` for module-level mutable state is a footgun**. The imported name shadows future updates. Either expose a getter (chosen here) or do `import x; x._y` at call sites. Worth scanning other modules for the same pattern.

130/130 tests pass. 12 new tests in `tests/test_tools.py` (TestReadFileBinaryRejection + TestBashChainRejection covering double-amp, double-pipe, semicolon, single-pipe, output-redirect, backtick substitution, dollar-paren substitution, quoted-pipe-in-regex pass-through, allowlist still fires, happy path still works, mismatched-quote returns clean error). `cve_lookup` has its own test coverage in earlier commits.

**Affected files**: `src/luxe/tools/cve_lookup.py` (new in `c1e2a81`, alias surfacing in `84e3ea8`), `src/luxe/tools/shell.py` (chain hardening), `src/luxe/tools/fs.py` (binary detection + `get_repo_root` getter), `src/luxe/tools/analysis.py` / `git.py` (switched to getter), `tests/test_tools.py` (12 new tests), `pyproject.toml` (1.1.0 → 1.2.0).

---

### [2026-05-02] v1.2.0 — cve_lookup surface bloat regression: gated to manage task_type

**What happened**: First v1.2 acceptance bench landed at 8/10 (regressed from v1.1's 9/10). New failure: `lpe-rope-calc-implement-strict-flag` — an *implement* fixture that v1.0 → v1.1 had stably passed (implement was the saturated category at 4/4). Diagnostic showed 252s wall, 3 tool calls, 8316 completion tokens, 34,913 chars of `final_text` — but **0 file mutations**. The model wrote prose explaining the change instead of calling `edit_file`.

3× replicate: **3/3 identical trajectories** (262s ± 1, exactly 34,913 final_text_chars each, identical 3-tool-call sequence). Deterministic at temp=0 with pinned `--work-dir`. Not variance.

**Root cause**: `cve_lookup` was added to the tool surface unconditionally in `_build_full_tool_surface` (`src/luxe/agents/single.py:70-72`). v1.1 didn't have it (added post-ship in commits `c1e2a81` + `84e3ea8`). The tool's description sat on the surface for *every* fixture — implement, document, bugfix, review — even though it's only useful for `manage` task_type's deps-audit flow. The surface bloat diluted the model's prior over `edit_file` / `write_file` enough on this borderline implement fixture to flip behavior into prose-mode under-engagement (the same shape as the known-FAIL `lpe-typing` doc fixture).

**Causal probe**: removed `cve_lookup` from the role allowlist (`configs/single_64gb.yaml`) and reran the failing fixture once. Result flipped FAIL (1/5, 0 mutations, 8316 prose-tokens) → PASS (4/5, 2 file changes, regex matched, 4680 task-tokens). One-line config change, deterministic outcome shift. Confirmed cve_lookup as the cause without false positives from coincident v1.2 changes (`_REPO_ROOT` getter, bash chain hardening, read_file binary detection were ruled out by this probe).

**Fix**: thread `task_type` parameter into `_build_full_tool_surface(...)`; gate the `cve_lookup` block to `if task_type == "manage"`. Default `task_type=None` excludes it (matches existing test fixture behavior). `run_single` passes its `task_type` arg through. Two new tests in `tests/test_single.py` assert (a) `cve_lookup` absent for `task_type` ∈ {None, implement, document, bugfix, review}, (b) `cve_lookup` present for `task_type=manage`, (c) allowlist still intersects with task-type gating.

**Post-gate full bench**: still 8/10. New failure: `nothing-ever-happens-document-config` (was PASS pre-gate). Different failure shape (14 tool calls / 23,645 chars / 343s / 75% context pressure — not the prose-deflection pattern). 3× replicate of this fixture in the post-gate state: **1 FAIL + 2 PASS**, with wildly divergent trajectories (9 vs 18 vs 33 tool calls, 30,816 vs 2,299 vs 2,713 final_text). Variance, not deterministic — same `temp=0` and pinned work_dir, but the doc/manage bottleneck fixtures sit close enough to under-engagement that small cache-state shifts flip them.

**Interpretation of v1.1's 9/10 in light of this**: the v1.1 ship was a single bench run. The variance evidence here suggests v1.1 was actually `8-9/10 with one borderline doc fixture` — we got a lucky variance roll. v1.2 ships at the same effective ceiling, but with one additional deterministic FAIL closed (lpe-implement). The gate fix is unambiguous progress regardless of which face the variance lands on for any single bench run.

**Root cause / interpretation**: this is a category of failure that the post-v1.1 plan didn't anticipate when it queued cve_lookup as the seed example. The subphase template was correct in *principle* (probe → harden → optionally overlay), but the act of adding a tool to the surface has **system-wide effects** on prefix-cache state and on the model's tool-selection prior across *all* task types. Any new tool from now on must be scoped to the task types where it's useful, or we re-discover this regression each time. Tool descriptions are part of the cached prefix; tool addition is therefore not a local change.

**Fix / takeaway**: tool gating by task_type is now the default for any audit-only or task-specific tool. `cve_lookup` is the precedent. When future tools are added to `_build_full_tool_surface`, the question to answer first is: "which task_type(s) actually need this on the surface?" — and gate accordingly. Don't assume "the model will ignore a tool it doesn't need" — at temp=0 with this surface size, addition has measurable behavioral cost.

The deeper meta-lesson: **single-run bench results have variance we hadn't measured**. Going forward, before claiming a regression-or-not on borderline doc/manage fixtures, run a 3× replicate. The implement category is genuinely deterministic at temp=0 (all replicates here showed 0 trajectory drift), but doc/manage are not. The implement-vs-doc/manage variance asymmetry is itself a finding worth carrying.

**nothing-doc-config variance**: this is a known doc-bottleneck fixture per `project_v1_bench_cycle.md`. The replicate evidence shows ~33% FAIL rate at the current substrate. Not addressed in v1.2 — would need its own subphase (better doc-task overlay, or model upgrade, or runtime re-prompt-on-incomplete-diff loop). Tracking as a known borderline.

**Affected files**: `src/luxe/agents/single.py` (added `task_type` parameter to `_build_full_tool_surface`, gated `cve_lookup` block, threaded through from `run_single`), `tests/test_single.py` (2 new tests), `lessons.md` (this entry).

### [2026-05-03] v1.3.0 — read-dedup orchestration bug surfaced; reprompt-on-doc lever shipped

**What happened**: Investigation into `lpe-rope-calc-document-typing`'s deterministic FAIL — three negative lever attempts (v1.1 abstract overlay, v1.2 procedural overlay, v1.3 runtime reprompt) had us calling it a model-side ceiling. External reviewer pushback ("you haven't ruled out orchestration") prompted a trace-instrumented re-run. The trace contradicted the working model entirely.

**The actual failure mode** (from instrumented `events.jsonl` with `LUXE_LOG_TOOL_CALLS=1`):

```
Main pass:
step 0: read_file d7732045  (productive)
step 1: edit_file b911d4fa  (productive, +1 line — IOBase import)
step 2: edit_file b911d4fa  DUPLICATE — model retried the same edit immediately
step 3: read_file d7732045  DUPLICATE — model tried to verify; dedup intercepted
                                       → streak=2 → ABORT
```

The model was making progress, then trying to re-read the file to verify its edit had landed. The duplicate-call detector at `loop.py:211` returned `"You already called read_file with these exact arguments..."` instead of dispatching the read and returning fresh post-edit content. The model interpreted the dedup response as "verify failed → my edit didn't land → retry the edit" and looped on the same `edit_file` call until the streak counter aborted.

**Hypothesis trajectory**:
1. Initial: model ceiling — three levers had failed.
2. Reviewer 1: H1 (compound-goal collapse), H3 (fenced-diff payload defeats reprompt). Both tested; both negative.
3. Reviewer 2: cache invalidation — `ToolCache.get_or_run` (`tools/base.py:60-73`) has no invalidation; post-write reads serve stale content. Code inspection confirmed; bench code path doesn't use the cache, so this was a red herring for THIS failure mode but a real latent issue worth a follow-up.
4. Streak-bump probe: increased `_MAX_CONSECUTIVE_REPEAT_STEPS` from 2 → 3. Model used the extra turn to retry the same edit a third time. Conclusion: bumping doesn't help — the model is in a decoder-favored loop on identical tokens at temp=0.
5. **The right fix** (plan D2): exempt `read_file` from the in-loop dedup detection. Reads are *idempotent in name but post-write semantics differ* — the model relies on re-reading to verify its own edits. Deduplicating reads strands the model.

**Fix**: `loop.py:_DEDUP_EXEMPT_TOOLS = {"read_file"}`. The dedup check now reads `if key in seen_calls and tc.name not in _DEDUP_EXEMPT_TOOLS:`. For exempt tools, the duplicate-detection branch is skipped and the call dispatches normally. seen_calls.add(key) is idempotent so no leak. `_MAX_CONSECUTIVE_REPEAT_STEPS` reverted to 2 — the bump was a red herring.

**Post-fix trace on lpe-typing**: 12 productive steps, 17 tool calls, 3 distinct edit_file calls (IOBase import, `_read_gguf_value` signature, `_pe_from_gguf` typing), no abort. Diff went from baseline +2/-1 → +3/-2. **Major orchestration improvement.** But still FAIL — the threshold is +4 and the model never attempted a top-of-file docstring insertion.

**The residual lpe-typing FAIL turned out to be a fixture-grader misalignment, not a model behavior.** This is the most important finding of the investigation, and it nearly went unnoticed because every prior diagnosis (mine and three reviewers') was working from the assumption that the docstring deliverable was unmet.

After D2, the H1-with-D2 probe (split-sentence variant) ran clean: 16 main + 6 reprompt tool calls, no aborts, 2 distinct typing edits, and a final synthesizer report stating: *"Module docstring — Already present at lines 2–14. No changes needed. The docstring clearly describes the module's purpose (scanning local model installs for positional-encoding metadata), lists the default scan sources, and notes the zero third-party dependency requirement."* Verification by `git show 5c6b51f80e76f80c49a789029414f5152a5edbd7:pe_scan.py` confirmed: the file ships with a 14-line module docstring at lines 2-14, beginning `"""pe_scan.py — Scan local model installs for positional-encoding metadata...`. The model has been correctly identifying the existing docstring across every run and refusing to add a redundant one.

The 2026-05-01 fixture surgery had aligned `min_matches` from 4 → 1 to match the actual 1-untyped-parameter count, but kept `min_added_lines: 4` with the comment "the docstring half of the task supplies the rest" — assuming the docstring half was unmet without verifying the file's actual content. **It was already met since base_sha.**

This means three things:
1. **The "three negative lever attempts on lpe-typing"** were attempts to coerce the model into a redundant docstring it correctly recognized as already present. The levers couldn't have worked; the goal asked for content the file already had.
2. **The reviewers' A/B/C/D hypothesis space about residual model behavior** (generative-write resistance, edit_file API friction, compound-goal shadowing, grader-aligned optimization) was reasoning about a residual that wasn't the right framing. Some of those hypotheses (especially B — `edit_file` API friction at top-of-file) may still be real and worth revisiting on a *different* fixture that genuinely tests prepend-style edits. They aren't refuted here; they were applied to the wrong target.
3. **D2 (orchestration fix) is independently valuable.** The dedup bug *was* killing productive work mid-task. The fix lets the model finish what it started. That part of the investigation stands on its own merits — every fixture that needs post-edit verification benefits — even though the headline failure on lpe-typing turned out to be a separate, smaller issue.

**Round-2 fixture surgery (2026-05-03)**: drop the docstring requirement from the goal text and lower `min_added_lines` from 4 → 2 (one import + one signature edit). The fixture now tests what it was intended to test: typing-edit competence. **Post-surgery validation: PASS, 4/5 (offline cap), +2/-1 diff (`from io import BinaryIO` + `def _read_gguf_value(f: BinaryIO, ...)`), 153s wall, no bailout.** First-ever lpe-typing PASS. Bench goes from 8/10 to 9/10 with v1.3. Future levers worth ranking: a `prepend_to_file` tool affordance (directly addresses (b)) > one-shot worked example in `document_strict` overlay > model upgrade.

**Reprompt-on-doc lever** (uncommitted in `cli.py` since 2026-05-03 morning, behind `LUXE_REPROMPT_ON_DOC=1`): independent variance-stabilizer test on `nothing-ever-happens-document-config` — the variance-borderline doc fixture from v1.2's investigation. n=3 replicates, all PASS (rep 1: 4/5 PASS / 317s / 98k tokens; rep 2: 4/5 PASS / 516s / 260k; rep 3: 4/5 PASS / 259s / 195k). 3/3 PASS vs baseline's 2/3. Reprompt earns its keep on doc-task variance even though it didn't unblock lpe-typing.

**Smoke regression for D2 + reprompt**: 4-fixture subset (`lpe-rope-calc-implement-strict-flag`, `nothing-ever-happens-manage-deps-audit`, `neon-rain-document-modules`, `the-game-document-architecture`) with both shipping. 4/4 PASS, avg_wall=153s, no bailouts, no regressions.

**Disposition**:
- D2 (`read_file` dedup exemption): SHIP. Orchestration improvement; benefits any post-edit verification scenario, not just lpe-typing. The fix is the constant `_DEDUP_EXEMPT_TOOLS = {"read_file"}` in `loop.py` — kept as a tool-property constant rather than a per-role flag because the exemption is a property of read semantics, not a per-config decision.
- Reprompt-on-doc: SHIP as opt-in (`LUXE_REPROMPT_ON_DOC=1`). The lever is validated on n=1 doc fixture × 3 replicates (`nothing-doc-config` 3/3 PASS). The smoke regression ran with the env var set but reprompt likely didn't *fire* on most smoke fixtures (avg wall=153s is consistent with no second-pass triggering on those), so we don't have explicit evidence that reprompt's behavior is benign on a wider set. Default-promote after wider validation lands — n≥3 doc fixtures where reprompt actually fires, observed not-regressing.
- lpe-typing: residual FAIL accepted. Now better-grounded as model-side docstring-resistance. Updated ceiling story replaces earlier "three lever attempts → ceiling" with "orchestration was the dominant cause; with that fixed, the model still won't write a top-of-file docstring on this fixture."
- Tool-event instrumentation in `loop.py` (`LUXE_LOG_TOOL_CALLS=1`): kept as permanent debugging knob. Off by default, no overhead.
- `diff_stat` checkpoint events in `cli.py`: kept; useful diff-progression telemetry.

**Latent issue surfaced but not addressed**: `ToolCache` has no invalidation. Currently moot because the bench code path doesn't pass a `ToolCache` to `run_single`, but if a future code path does, post-write reads will serve stale cached content. A `ToolCache.invalidate_for_write(path)` would be the right fix; deferred until the cache is actually wired into the bench path.

**Meta-lesson on diagnostic ordering**: the original three lever attempts (overlays + reprompt) all targeted the model's prompt-side behavior. None attempted to instrument the agent loop to see whether the abort was even reaching the model's "give up" point. The trace inspection — adding `tool_call`, `tool_step_done`, and `diff_stat` events behind a single env flag — answered the question in one re-run. The lesson for future failure investigations: **before more prompt levers, instrument the agent loop**. The cost is tiny (~30 lines, all gated by env var) and the diagnostic value is enormous. Any failure that produces an abort should have its trigger pair logged with detector state.

**Larger meta-lesson on fixture grading**: the fixture-surgery story here is the deeper lesson. Two rounds of investigation ran on the assumption that the goal's docstring deliverable was unmet, and three levers + extensive trace work were aimed at "why won't the model write the docstring." The model had been writing — and not writing — the right things all along. The synthesizer.md output had been telling us "docstring already present at lines 2–14" for runs we were ignoring, because we read the *bailout summary* (`stuck_no_output`) and the *diff size* (+2/-1) but never the *model's stated reasoning*.

**Three concrete diagnostic improvements suggested by this**:
1. **Verify fixture grader alignment against actual base-file content** at the moment of fixture surgery, not just against the goal text. The 2026-05-01 surgery comment said "the docstring half of the task supplies the rest" — a claim that should have been checked by `git show <base_sha>:<file>` before being committed as a grader assumption. Add a sanity-pass to fixture surgery: read the base file, confirm each goal deliverable is genuinely missing.
2. **Always read the synthesizer.md when investigating a deterministic FAIL.** The model's final report often contains the *reason* it considered the work done. We have a dedicated `synthesizer.md` artifact in every run dir; we should be glancing at it as readily as we glance at `comparison.json`. The dedup investigation took longer than necessary because the synthesizer output saying "docstring already present" was visible in the post-D2 trace before we noticed.
3. **Treat "the model is doing the wrong thing" as a hypothesis, not a fact.** When a deterministic fixture FAILs across a model and several prompt configurations, the most-tested-by-cost-of-being-wrong hypothesis is "the fixture is asking for something that doesn't make sense in this base state." The cost of being wrong on this hypothesis is ~10 minutes (read the base file, compare to the fixture's goal). The cost of being wrong the other way is multi-day investigations.

**Affected files**: `src/luxe/agents/loop.py` (added `_DEDUP_EXEMPT_TOOLS` constant + check at line 228; added `LUXE_LOG_TOOL_CALLS=1`-gated `tool_call` and `tool_step_done` event emission via `append_event`; added `run_id` and `phase` parameters to `run_agent` for event correlation; imports `os`, `hashlib`, `append_event`), `src/luxe/agents/single.py` (added `run_id` and `phase` parameters to `run_single`, plumbed to `run_agent`), `src/luxe/cli.py` (passes `run_id=spec.run_id, phase="main"` and `phase="reprompt"` to the two `run_single` call sites; added `diff_stat` checkpoint events at `after_main_pass` and `after_reprompt_pass`; reprompt block stays uncommitted-feature-now-shipped — `LUXE_REPROMPT_ON_DOC=1` env var promoted to default-shipping behavior), `pyproject.toml` (1.2.0 → 1.3.0), `lessons.md` (this entry).

### [2026-05-03] v1.3.1 — `_diff_against_base` undercount bug fix + directive reprompt for prose-mode

**What happened**: Investigating the *other* outstanding bench FAIL (`nothing-ever-happens-document-config`, ~33% prose-loop variance) post-v1.3 ship. Plan: validate whether D2 fix alone resolved the variance (3 reps without reprompt). Result: 2/3 PASS — same rate as v1.2. D2 didn't help this fixture. Then 3 reps with reprompt enabled to validate the lever post-D2: also 2/3 PASS. Rep 1 FAILed and reprompt didn't rescue it — second pass made 13 more tool calls (10 read + 3 search), zero write_file, identical prose-mode shape to first pass.

**Root cause #1 (bug)**: While inspecting the trace, found that `_diff_against_base` (`src/luxe/cli.py`) only counted **tracked** file changes via `git diff <base_sha>`. New files created by `write_file` (e.g., `CONFIG.md` from scratch) are untracked until staged, so the diff was reading 0 additions even on PASS runs that wrote the full doc. This affected:
- The `diff_stat` telemetry checkpoint — undercounted on every doc fixture that creates new files.
- More critically, the `_should_reprompt_for_under_engagement` gate — reprompt was firing on already-passing runs because additions=0 always for new-file doc tasks. The earlier "3/3 PASS with reprompt" data point on this fixture (yesterday's validation) was inflated: reprompt was firing unnecessarily on all three reps; the wall times (317s / 516s / 259s) reflect bug-induced double-passes, not the lever's real fire rate.

**Fix #1**: prefix `git add -N .` (intent-to-add) in `_diff_against_base`. This adds index entries for untracked files without staging content, making them visible to `git diff <base_sha>` as `+N/-0` changes. The PR cycle's later `git add . && git commit` still works correctly. Validated on the post-fix nothing-doc reps: diff_stat now shows additions=197 / 0 / 135 across the three reps (matching the grader's view). Reprompt now fires only on actual prose-mode FAIL runs.

**Root cause #2 (model behavior)**: With the bug fix, the data tells a clearer story. On nothing-doc-config, the model has ~33% prose-loop rate where it reads files, enumerates env vars in its `final_text` (30k+ chars of prose), and never calls `write_file`. The original reprompt's instruction *"For any deliverable NOT yet reflected in the diff, make the missing edits now via edit_file or write_file"* gets ignored — the model just does another exploration pass. The `final_text` from the FAIL contains the full content the user wants, but it's stranded as prose.

**Fix #2 (directive reprompt)**: branched `followup_goal` in the reprompt block. When `additions == 0 AND len(prior_text) > 1000` (the prose-mode signature), the reprompt now:
- Names the failure explicitly: *"PROBLEM: You did NOT call write_file or edit_file. The working tree has 0 added lines. You produced extensive prose in your final report but it is stranded — not saved to disk."*
- Injects the model's own prior `final_text` (truncated to 6000 chars) as the *content to save*.
- Demands `write_file` as the FIRST tool call: *"Do this on your FIRST tool call. Do not explore more files first."*

The non-prose-mode reprompt path (some edits but under threshold) preserves the original behavior.

**Validation**: shipped without a 3-rep validation of the directive reprompt — opportunity cost of waiting for ~15 min of bench runs vs moving to the queued MLX_USE_ANE probe was deemed not worth it. The fix is structurally well-targeted (only fires in the case the original reprompt was ineffective) and the fallback preserves prior behavior. If the directive reprompt regresses, the rollback is `LUXE_REPROMPT_ON_DOC` is opt-in so users opt-in to this code path explicitly. Validation deferred to a future bench cycle.

**Bench state at v1.3.1**:
- lpe-typing: deterministic PASS (post-fixture-surgery)
- nothing-doc-config: ~33% FAIL rate without reprompt; with bug-fixed reprompt, fire rate now matches prose-mode rate. Whether the directive reprompt rescues those FAILs is the open question.
- Expected score: 9.67/10 baseline (9 stable + ~0.67 from nothing-doc); 10/10 if directive reprompt works.

**Meta-lesson for future investigations**: the diff_stat bug had been silently corrupting our reprompt firing decisions for 24+ hours. The "3/3 PASS with reprompt" finding that justified the lever's ship was bug-inflated. Two takeaways:
1. **Test telemetry against ground truth before relying on it for decisions.** A simple sanity check — "run a fixture that creates a new file, verify diff_stat shows nonzero additions" — would have caught this. Add to the diagnostic-tool habit.
2. **Validate threshold-based decisions on edge cases.** The reprompt threshold check is a numerical comparison; if the input is silently zeroed, every comparison is wrong. Future thresholding logic should log the input value with a sanity-check assertion or warning when the value is implausibly low.

**Affected files**: `src/luxe/cli.py` (`_diff_against_base` adds `git add -N .` prefix; reprompt block branches on `additions == 0 AND len(prior_text) > 1000` for the directive prose-mode followup), `pyproject.toml` (1.3.0 → 1.3.1), `lessons.md` (this entry).

### [2026-05-03] v1.4.0 — SpecDD Lever 1: programmatic Definition of Done; first 10/10 bench

**What happened**: Shipped Lever 1 of the SpecDD phase
(`~/.claude/plans/fluffy-brewing-lemur.md`). All 10 bench fixtures now
have a `requirements:` schema; the agent's reprompt gate uses the spec
validator (per-requirement check) when a spec is provided. Full bench
validation: **10/10 PASS, 40/50 score, v1 release gate cleared** — the
first time the bench has gone clean.

**Sequence of v1.4-prep commits in this ship** (all on main, between
v1.3.2 and v1.4.0 tags):
1. `23827c1` — `src/luxe/spec.py` data model (`Requirement`, `Spec`,
   YAML round-trip), 27 unit tests.
2. `0d37844` — `src/luxe/spec_validator.py` predicate evaluator
   (regex_present, regex_absent, tests_pass, ast_query stub, manual
   stub), 18 unit tests. Reuses `git add -N` diff trick from v1.3.1.
3. `fcc9830` — Bench integration. `Fixture` dataclass gains
   `requirements`/`to_spec()`; `grade_fixture` runs spec validator as
   parallel observation (does not gate score yet); lpe-typing migrated
   as proof-of-concept. Local smoke validated PASS+FAIL agreement.
4. `a81007c` — Prompt-template helpers `format_spec_for_task_prompt`
   (input side, in spec.py) and `format_unsatisfied_for_reprompt`
   (output side, in spec_validator.py), 7 tests.
5. `98c6b89` — `cli.py` reprompt gate replacement. New `--spec-yaml`
   flag; spec validator gate replaces the v1.3 diff-size heuristic
   when a spec is provided AND `LUXE_REPROMPT_ON_DOC=1`. Bench harness
   threads the spec via temp YAML. v1.3 directive form preserved as
   fallback for ad-hoc usage without `--spec-yaml`.
6. `e01169d` — 4 mechanical fixture migrations (lpe-implement-strict,
   the-game-shuffle, neon-rain-reset, isomer-healthcheck). Direct
   port of expected_outcome to single R1.
7. `0f611d0` — 5 loose-grader fixture migrations (the-game-arch,
   neon-rain-modules, isomer-quickstart, nothing-manage-deps,
   nothing-doc-config). Tightened to per-sub-deliverable requirements;
   audit-recommended bench-rigor improvements landed at the spec layer.

**Validation results** (`acceptance/v1_4_prep_full_bench/`,
2026-05-03 18:13–18:53):
- 10/10 PASS, 40/50 score (4/5 each = offline cap)
- Every fixture: `expected_outcome_passed=true` AND `spec_all_satisfied=true`
- Zero unsatisfied requirements across the entire bench
- nothing-doc-config (the variance fixture) PASSed cleanly with 119 added-line
  matches against R1's ≥15 threshold; reprompt did not need to fire
- v1 release gate cleared by `mono__qwen3.6-35b-a3b-6bit`

**Recalibrated framing** (per audit memos written this session):
- The original SpecDD plan framed Lever 1 as "attacks compound-goal shadowing,
  the bench's primary ceiling" with a 1+-point bench-score lift expected.
- Post-v1.3 audit (`project_compound_goal_audit.md`) showed compound-goal
  shadowing wasn't actually exhibited on the bench — every passing run
  fully addressed all sub-deliverables. The "primary ceiling" framing
  was empirically thin.
- Lever 1 still ships for **architectural value**: programmatic
  Definition of Done + per-requirement grading + future-readiness for
  Levers 2/3.
- The bench-score outcome (8/10 → 10/10) is real but the causes are
  layered: lpe-typing fixture surgery (v1.3.0) closed the deterministic
  FAIL; nothing-doc variance happened to roll positive on this run
  (~33% historical FAIL rate is unchanged structurally).

**What did NOT ship in this version (deferred)**:
- v1.3 directive reprompt code retirement (step 7 from the v1.4 roadmap).
  Removing it would silently disable reprompt for ad-hoc `luxe maintain`
  usage without `--spec-yaml`. Spec path is preferred when available;
  directive form is preserved as fallback. Future ship once we have
  evidence of ad-hoc usage patterns.
- min_added_lines representation in spec model. Currently a fixture-level
  floor in legacy grader; not yet a per-requirement predicate kind. The
  4 mechanical-port fixtures still have legacy `min_added_lines` floors
  enforced parallel to spec validation.
- ast_query and manual predicate kinds stubbed (return unsatisfied with
  notice). Full integration with `src/luxe/symbols.py` deferred until
  a fixture actually authors an ast_query requirement.

**Affected files**: `src/luxe/spec.py` (new), `src/luxe/spec_validator.py`
(new), `tests/test_spec.py` (new, 31 tests after step 4 additions),
`tests/test_spec_validator.py` (new, 21 tests after step 4 additions),
`src/luxe/cli.py` (`--spec-yaml` flag + spec validator gate in reprompt
block; v1.3 directive code preserved as fallback), `benchmarks/maintain_suite/grade.py`
(`Fixture.requirements`, `Fixture.to_spec()`, `FixtureResult.spec_validation`,
`spec_all_satisfied`; spec validator wired into `grade_fixture` as parallel
observation), `benchmarks/maintain_suite/run.py` (`_luxe_maintain` writes
spec YAML and threads `--spec-yaml`), `benchmarks/maintain_suite/fixtures.yaml`
(all 10 fixtures gained `requirements:` blocks, 5 of which were tightened
beyond the legacy `expected_outcome` to close audit-identified gaps).

**391 tests pass** (up from 384 pre-Lever-1; 27 spec + 21 spec_validator + 1 net change in other test counts).

**Memory entries from this ship**: see also
`project_compound_goal_audit.md`, `project_loose_grader_audit.md`,
`feedback_instrument_loop_first.md`, `feedback_verify_fixture_grader.md`
in the project memory directory.

### [2026-05-03 PM] v1.4.0 — three-replicate validation: 9/10, 10/10, 10/10

**What happened**: Per the RESUME.md decision tree, ran three independent
full-bench replicates of v1.4.0 (`acceptance/v1_4_validation_rep_{1,2,3}/`)
with `LUXE_REPROMPT_ON_DOC=1` against `variants_v1_default.yaml`, pinned
work_dir, `--force` to wipe stored state per rep. Results:

| Rep | Stored result | Failed fixture | Notes |
|---|---|---|---|
| 1 | 9/10 (40/50 score) | `nothing-ever-happens-document-config` (1F) | Variance fixture rolled FAIL — expected ~33% historical rate |
| 2 | 10/10 | — | clean |
| 3 | 10/10 | — | clean |

**Headline**: 2/3 at 10/10 confirms the variance branch — bench is
**effectively 9.67/10**, not a structural 10/10. The original v1.4.0
ship at 10/10 (acceptance/v1_4_prep_full_bench) was real but
variance-fortunate; the structural ceiling is at one fixture's prose-mode
roll on `nothing-doc-config`. Implement and manage categories remain
deterministic; doc-category variance dominates.

**Sidecar regrade discovered a pre-existing tooling bug** (`scripts/regrade_local.py:90`):
the regrader uses `git checkout origin/<branch_name>` against the local
clone, but for fixtures where the agent's branch wasn't pushed to origin
(notably `nothing-ever-happens-manage-deps-audit` across all 3 reps), the
ref doesn't exist and the regrader silently falls back to `base_sha` →
0 additions → spurious FAIL. This is exactly the stale-`origin/<branch>`
trap warned about in `feedback_offline_cache_refs.md` and the Critical
Gotchas in RESUME.md, except inside the regrader itself. The bench-time
grader's numbers are authoritative; the sidecar regrade for manage tasks
is unreliable until this is fixed. Hand-grading the actual local cache
showed real `SECURITY-AUDIT.md` writes of 81-107 lines on branches -3/-4/-5,
matching the bench-time `diff_additions=76-90` numbers.

**Decision per the resume tree**: end at option B. v1.4.0 is shipped and
tagged; the 9.67/10 effective bench is the honest framing.

**Lessons reinforced**:
1. **Variance is structural, not eliminated by Lever 1.** The original
   audit (`project_compound_goal_audit.md`) was right: SpecDD's
   compound-goal premise didn't account for prose-mode tool-affordance
   variance. Lever 1's value is architectural (programmatic DoD); it
   doesn't lift the doc-category variance ceiling.
2. **Trust bench-time results over sidecar regrade for manage tasks.**
   The sidecar is a tool for cheap iteration on grading logic, not a
   ground-truth re-evaluation. When stored and regrade disagree on a
   manage fixture, hand-grade the cache.

**Affected files**: `lessons.md` (this entry), memory directory
(`project_v1_4_validation.md`, `project_regrade_local_origin_bug.md`).

### [2026-05-04] v1.4.1 Mode B/A fix combination — 10/10 PASS on nothing-doc-config × 10 reps

**What happened**: Three fixes from the late 2026-05-03 session
(citation-linter bare-filename fallback + Mode B mid-loop write-pressure
injection + sidecar regrade lint re-run) validated on
`nothing-ever-happens-document-config` × 10 replicates with
`LUXE_WRITE_PRESSURE=1` + `LUXE_REPROMPT_ON_DOC=1`. Result: **10/10 PASS,
score 4/5 each**, 0 unresolved citations across all reps.

**Per-rep breakdown** (`acceptance/doc_config_modeB_rep_{1..10}/`):

| Rep | Score | Citations | Additions | Mode B fired | Reprompt fired | Notes |
|-----|-------|-----------|-----------|--------------|----------------|-------|
| 1 | 4 | 0 | 173 | — | — | clean engagement |
| 2 | 4 | 0 | 135 | — | — | clean |
| 3 | 4 | 0 | 203 | — | — | clean |
| 4 | 4 | 0 | 168 | — | — | clean |
| 5 | 4 | 0 | 147 | — | ✓ | reprompt rescue (main pass: 0 add, 28k prose chars) |
| 6 | 4 | 0 | 135 | — | — | clean |
| 7 | 4 | 0 | 207 | — | — | clean |
| 8 | 4 | 0 | 173 | — | — | clean |
| 9 | 4 | 0 | 138 | ✓ (step 6) | — | Mode B rescue (gate fired at step 6: 18 tool calls + 5024 tokens + 0 writes; main pass continued and produced 138 additions) |
| 10 | 4 | 0 | 30 | — | — | clean (smaller diff but still PASS) |

**Three rescue regimes observed**:
1. **Clean engagement** (8/10): model writes early, neither Mode B nor reprompt fires.
2. **Mode B rescue** (1/10, rep 9): gate fires mid-main-pass when tool count + tokens + step thresholds cross with 0 writes. The synthetic user message lands at the right moment; model writes immediately after. No reprompt needed.
3. **Reprompt rescue** (1/10, rep 5): Mode B doesn't fire because the main pass exits before step 5's *cumulative* completion tokens cross the 4000 threshold (the model goes from "still reading" at step 4 to "all-prose" at step 5; step 5 generates ~5000 prose tokens AFTER the gate evaluation at step 5 entry). The post-loop reprompt — gated on `spec_all_satisfied=false` — fires and the model writes on the second pass.

**Mode A signal**: 0/10 reps had unresolved citations. Linter bare-
filename fallback eliminated the false-flag failure mode that produced
`acceptance/doc_config_diag_rep_1` 3F before the fix.

**Headline**: With v1.4.1's three fixes combined, the historical ~33%
FAIL rate on this fixture collapses to 0% across n=10. Mode B and
reprompt are complementary — when one doesn't catch the trap, the other
does.

**Threshold tuning consideration**: Mode B's `_WRITE_PRESSURE_MIN_TOKENS=4000`
*just barely* misses rep 5's pre-prose state (cumulative tokens hit ~3800
at step 5 entry, the prose itself crosses 4000). Lowering to 3000 would
catch rep 5 before reprompt does, saving ~370s of wall time on rescued
runs. Not pursued — n=1 observation isn't enough signal to tune
thresholds, and the reprompt rescue worked.

**Affected files**: `lessons.md` (this entry), `RESUME.md` (state update).
Memory: `project_doc_config_three_modes.md`, `project_external_benchmark_program.md`.

---

### [2026-05-04 PM] SWE-bench n=10 A/B — counterexample heuristic regressed quality; deliberation amplifiers are dangerous on already-correct trajectories

**What happened**: After the swebench prompt overlay shipped (smoke 2/3 PASS
mechanically), one trajectory (`astropy-12907`) showed the model localizing
the bug correctly (`_cstack` in `astropy/modeling/separable.py`) but never
calling `edit_file` — 5 reads + 25k chars of analysis + final report with
no edits. Diagnosed as hypothesis-stall (`(e)`): model traced the bug
report's simple snippet, concluded its tracing was correct, and never
constructed the failing nested-CompoundModel input that would have
falsified the conclusion. Shipped a `swebench_bugfix_counterexample`
PromptVariant that adds one clause: "if your trace yields the expected
result but the report shows otherwise, construct the failing variant."

A/B on a stratified n=10 (4 `<15 min fix` + 6 `15 min - 1 hour` across 10
distinct repos) flipped the working state backwards:
- Baseline: mechanical 10/10 (real-fix rate 4-5/10 by manual review)
- +Heuristic: mechanical 8/10 — `matplotlib-13989` (which the baseline
  had matched gold EXACTLY) regressed to empty patch, and
  `astropy-13453` (baseline had a partial fix) also regressed to empty.

**Root cause**: The heuristic was scoped as a global prompt modifier but
behaves like a conditional intervention. Helps: ambiguous /
underdetermined / multi-site fixes (rare in the n=10 set). Hurts:
straightforward pattern-alignment fixes (common). Adding a "construct a
counterexample" deliberation trigger to a model that was already on the
correct trajectory shifts it from pattern-completion → overthinking →
deviation. The 12907 trace was atypical — falsification is genuinely the
right move there, but extrapolating from one trace to a global prompt
clause amplified noise.

**Two more findings worth banking**:

1. **Trajectory fragility is the bigger story.** Two cases flipped from
   gold-match → broken under the heuristic. The model often has the
   correct answer early, and continued reasoning can erase it. This
   suggests minimality / early-stopping bias may outperform more-
   reasoning prompts as a future direction.

2. **The (b) reasoning bucket is not one thing.** In the n=10 manual
   review, (b) splits into:
   - **(b1)** missing transformation pattern (django regex char-class —
     fixable with examples)
   - **(b2)** multi-location consistency (requests-2931 fixes one of
     two sites, pytest-10051 misses adding a new method — planning gap)
   - **(b3)** true design gap (sphinx prefers obvious `dict.fromkeys`
     dedup over the gold's `sorted(set(...))` — model chose the
     simpler-looking option)

   Only b1 and b2 are realistically prompt-tractable.

**Fix / takeaway**:
1. Reverted the rule; baseline prompt is the working state. The
   `swebench_bugfix_counterexample` variant stays in tree as a negative
   control for SpecDD comparison — useful, even if not shipped.
2. Inspector v2 ships a 5-signal gold-proximity tier (file match,
   line-based hunk proximity, hunk coverage, hunk count, size, token
   overlap) so that "10/10 mechanical PASS" stops misleading. n=10
   real picture: 6 strong + 3 plausible + 1 wrong_location, vs.
   mechanical's 10/10. Rich tiers visible via
   `python -m benchmarks.swebench.smoke_inspect --gold-source ...`.
3. New durable rule (memory): interventions that amplify reasoning can
   harm trajectories that were already correct. A/B before shipping any
   "think more" clause; do not extrapolate from single-instance probes.

**Affected files**: `src/luxe/agents/prompts.py`, `tests/test_prompts.py`,
`benchmarks/swebench/subsets/probe_n10.json`,
`configs/single_64gb_swebench_counterexample.yaml`,
`benchmarks/swebench/smoke_inspect.py`,
`tests/test_swebench_smoke_inspect.py`.
Memory: new `feedback_deliberation_amplifiers.md`; updated
`project_swebench_smoke_2026_05_04.md`.

---

### [2026-05-04] SWE-bench n=75 pre-SpecDD anchor — 32% high-confidence; empty-patch is the dominant failure class

**What happened**: Stratified n=75 Verified subset run completed in 7h 34m
wall against `configs/single_64gb_swebench.yaml` (anti-reproducer overlay,
Qwen3.6-35B-A3B-6bit @ temp=0). Headline numbers, with three different
honesty levels:

- **Mechanical PASS**: 45/75 (60%) — non-empty, non-test-path, non-new-file
- **Strong (gold-match)**: 12/75 (16%) — inspector v2 gold-proximity tier
- **Strong + plausible**: 30/75 (40%) — inspector v2 best-case
- **Manual high-confidence (Step 2 review)**: **24/75 = 32%** — the durable anchor

The 32% number landed squarely in RESUME.md's pre-defined "30-45% →
SpecDD Lever 2" decision branch. Lever 2 is now in flight at v1.5.0.

**Root cause / what surprised**:

1. **The n=10 A/B (`probe_n10.json`) was 50pp optimistic.** That run hit
   9/10 strong-or-plausible. The n=75 stratified mix dropped to 40%.
   The n=10 wasn't dishonest — it was just easy + small + cherry-picked
   across distinct repos. Don't extrapolate small probes to a real anchor.

2. **Empty-patch is the dominant failure class at scale (26/75 = 35%).**
   The n=10 had zero empty-patches; this only emerges past ~30 instances.
   Anti-reproducer prompt's locate→read→edit→verify protocol fails to
   even produce a candidate diff on a third of stratified tasks. The
   class clusters by repo: sphinx, pylint, mwaskom, late-requests
   (5414/6028) are heavily over-represented. It's not uniformly random.
   This is the **single biggest signal** for SpecDD: the model isn't
   over-editing or under-reviewing — it's failing to commit at all.

3. **Anti-reproducer prompt rule is leaky** — 4/75 created `test_fix.py`
   despite the prompt forbidding it (django-10097, xarray-3305,
   pytest-5262, sympy-13877). Prose-level rules are guidance, not
   enforcement. **Tool-side `Forbids` (Lever 2's design) is the right
   shape for this category** — the prompt cannot be made strictly
   reliable; the tool layer can.

4. **wrong_target (12) >> wrong_location (3)**. Cross-file localization
   is harder than within-file localization. Multi-file gold patches
   dominate the wrong_target class (sympy-13091 has 21 gold files;
   django-11532 has 5). When gold spans many files, the model picks
   one and ignores the rest. SpecDD Lever 2's `.sdd` chain that scopes
   each iteration to a specific subtree is a partial mitigation;
   Lever 3's per-file `.sdd` contracts would be a stronger one.

5. **Inspector v2 understates "strong" by ~6 cases** (~40% of plausibles
   were actually clean PASSes after manual review). The token-overlap
   (jaccard) signal is too noisy when model and gold use slightly
   different identifiers — `to_native_string` vs `builtin_str` removal
   in requests-2317, `dict.fromkeys` vs `sorted(set(...))` in
   sphinx-10466, etc. Line-based hunk proximity also brittle when
   intermediate edits drift line numbers (sklearn-10908 was marked
   wrong_location but is a clean gold-match in the same method).
   Manual Step 2 is non-optional; mechanical inspector ≠ ground truth.

6. **One distinct failure pattern worth a name: "fixed adjacent symptom".**
   `sphinx-10449` had a real `NameError: annotation` bug in the original
   code; the model fixed that and stopped, never reaching the actual
   reported issue (suppress `:rtype: None` for class autodocs). This is
   a new class — call it (f) **adjacent-bug stop**: model finds A real
   bug nearby and considers the goal satisfied. Not (d) already-passing
   (which is "no real bug exists"), and not (b1/b2/b3) reasoning class.
   Worth tracking on Lever 2/3 reruns to see if it's a one-off or a
   pattern.

**Fix / takeaway**:

1. **32% is the durable pre-SpecDD anchor.** Use this number, not
   "12 strong" or "30 strong-or-plausible", when comparing post-Lever-2
   runs. Strong-only is too tight (excludes semantically-equivalent
   different-mechanism fixes); strong-or-plausible is too loose
   (includes wrong-direction "plausibles"). Manual Step 2 with a
   per-instance taxonomy verdict is the only honest number.

2. **Empty-patch class is the SpecDD test.** Lever 2's tool-side `Forbids`
   doesn't directly help here, but the per-file `.sdd` chain in worker
   prompts plus the spec validator's reprompt-on-unmet-requirement gate
   should help: when the model returns "I couldn't find the bug",
   the validator should emit a structured "R1 still unsatisfied — do
   not stop" instead of letting the loop terminate. Track empty-patch
   delta as the headline signal on the post-Lever-2 rerun. If empty-patch
   stays at ~25/75, Lever 2's bench-moving claim falls apart.

3. **Anti-reproducer rule moves to the tool layer at Lever 2.** Already
   queued in the build order: `src/luxe/tools/fs.py` `write_file` and
   `edit_file` refuse if target matches an ancestor `.sdd`'s `Forbids:`
   glob. Internal `.sdd` for the swebench fixture mode will list
   `Forbids: test_*.py` at the root.

4. **Don't extrapolate small probes to anchors.** The 9/10 n=10 result
   was real but unrepresentative. Future bench-program planning should
   require at least 50-instance stratified samples before claiming a
   number is "the" anchor.

5. **Add (f) adjacent-bug stop to the failure-mode taxonomy.** Update
   `project_swebench_smoke_2026_05_04.md`'s taxonomy when Lever 2 reruns
   produce a second instance of this pattern.

**Affected files**: no source changes for the bench run itself. Memory
entry: new `project_swebench_n75_baseline.md`. Output:
`acceptance/swebench/pre_specdd_v141_n75/rep_1/predictions.json` (the
durable artefact for FAIL_TO_PASS Docker harness scoring later) and
`step2_gold_vs_model.txt` (per-instance gold-vs-model dump for the
plausibles + wrong_locations, used for the manual review).

---

### [2026-05-05] SpecDD Lever 2 architecture + two subtle gotchas

**What happened**: Shipped SpecDD Lever 2 (v1.5.0) end-to-end in one
session: parser (`src/luxe/sdd.py`) → resolver (`src/luxe/spec_resolver.py`)
→ tool-side Forbids enforcement (`src/luxe/tools/fs.py`) → prompt-side
chain block (`src/luxe/agents/single.py`) → citation linter
spec_violation/spec_orphan signals (`src/luxe/citations.py`) → synthetic
`.sdd` injection for SWE-bench fixtures (`benchmarks/swebench/adapter.py`)
→ four dogfood `.sdd` files for the luxe codebase itself + root
`CLAUDE.md`. Full suite: 521 → 607 passed (+86 tests).

Two subtle issues surfaced during integration that are worth banking:

**1. `except ValueError` silently catches `SddParseError`.**

`SddParseError(ValueError)` subclasses `ValueError` so a tool can
distinguish "out-of-repo path" (`ValueError` raised by
`Path.relative_to`) from "malformed contract". The tool layer's
`_check_spec_forbids` had:

```python
try:
    chain = resolve_chain(...)
except ValueError:
    return None  # outside repo_root, _safe will reject
except SddParseError as e:
    return f"Cannot evaluate Forbids: malformed .sdd — {e}"
```

The `ValueError` clause caught the malformed-`.sdd` case first, so
malformed contracts silently allowed all writes — exactly the
opposite of the intended behaviour. Fixed by reordering the
catches; the constraint is now documented with a `NOTE` comment in
the function.

The general rule: **when catching multiple exception types where one
subclasses another, list the most-derived first.** Test each path
explicitly — a passing "no-error" test does not exercise this
ordering.

**2. Synthetic `.sdd` in a fixture clone reads as "uncommitted".**

The SWE-bench adapter drops a synthetic `<repo_basename>.sdd` at
fixture-prep time so tool-side Forbids fires for the anti-reproducer
rule that the prose prompt cannot reliably hold. The first smoke run
crashed in 1 second with rc=2:

> `luxe refuses to start with uncommitted changes — commit, stash, or
> pass --allow-dirty to proceed (the PR diff will include them).`

The synthetic contract is by design uncommitted (it's removed before
`extract_diff` so it never enters predictions.json). Fix: pass
`--allow-dirty` from `invoke_luxe_maintain`. Smoke probe
(astropy-12907 with injection) then succeeded with the gold-shape
patch and zero `.sdd` contamination.

Generalisation: **fixture-prep injection that adds untracked files
must be paired with `--allow-dirty` in any agent invocation that
checks tree cleanliness.** Other future cases that might trigger
this: temp-files for environment overrides, model-context sidecars,
synthesizer.md overrides for resume scenarios.

**Architecture note worth banking**:

The plan's "Lever 2 chain at worker iteration time" was scoped against
a worker tier that doesn't exist post-mono pivot (v1.0). The actual
shape that emerged: **chain block injection at the single task-prompt
construction** in `single.py` (full-repo scope, since mono mode has
no per-file targeting). `find_all_sdd` walks once, `format_sdd_block`
renders Forbids/Owns only (Must / Done when stay aspirational and live
in spec_validator's reprompt path). The plan's "resume reloads chain"
task is N/A — luxe's resume path is `luxe pr <run_id>` which runs
the post-synthesizer PR cycle, not the agent loop. Chain reloads
fresh on every `run_single` call by construction. Documented inline.

**Fix / takeaway**:
1. New durable rule (memory): when catching exception hierarchies in
   the same try/except, derived classes first or it silently routes
   wrong.
2. New durable rule (memory): fixture-prep injections that drop
   uncommitted files into the cloned tree need `--allow-dirty` in
   the downstream agent invocation. Track this as part of any future
   .sdd-class fixture-prep work.
3. Lever 2 ships at v1.5.0 with seven concrete deliverables; the
   hypothesis ("anti-reproducer rule moves to the tool layer →
   empty_patch class shrinks via better engagement, new_file_in_diff
   class disappears") is testable on the next n=75 rerun. The
   prediction is empty_patch ↓, new_file_in_diff → 0; if either
   doesn't materialise, the hypothesis is wrong.

**Affected files**: `src/luxe/sdd.py`, `src/luxe/spec_resolver.py`,
`src/luxe/tools/fs.py`, `src/luxe/agents/single.py`,
`src/luxe/citations.py`, `src/luxe/cli.py`,
`benchmarks/swebench/adapter.py`, `benchmarks/swebench/run.py`,
`benchmarks/swebench/compare_runs.py`, `tests/test_sdd.py`,
`tests/test_spec_resolver.py`, `tests/test_tools_spec_forbids.py`,
`tests/test_single.py`, `tests/test_citations_diff_aware.py`,
`tests/test_swebench_adapter.py`, plus the four dogfood `.sdd` files
and root `CLAUDE.md`.

---

### [2026-05-05] SpecDD Lever 2 — post-ship SWE-bench n=75 result

**What happened**: Same n=75 stratified subset, same model, same
config + Lever 2's prompt-side `.sdd` block + tool-side Forbids +
synthetic `<repo>.sdd` injection at fixture-prep. 7h41m wall (vs
baseline 7h2m, +9% from added prompt tokens).

| Metric | Pre-Lever-2 | Post-Lever-2 | Delta |
|---|---|---|---|
| strong (gold-match) | 12 | 13 | +1 |
| strong + plausible | 30 | 32 | +2 |
| empty_patch | 26 | 30 | **+4** |
| new_file_in_diff | 4 | **0** | **-4** |
| any non-empty patch | 45 | 45 | 0 |

**Two simultaneous effects**:

1. **`new_file_in_diff` 4 → 0 — target class CLEARED.** Of the four
   baseline instances that created reproducer files: django-10097
   → **strong** gold-match, xarray-3305 → plausible, pytest-5262 →
   strong, sympy-13877 → empty_patch. Three out of four escaped to
   a real fix once the synthetic `.sdd` Forbids fired tool-side.
   The Lever 2 hypothesis ("anti-reproducer rule moves to the tool
   layer → no_file_in_diff disappears") is empirically confirmed
   at n=75 scale.

2. **`empty_patch` 26 → 30 — prose-mode regression.** The prompt-side
   `.sdd` block adds context tokens (~200-400 depending on chain
   depth). On borderline instances, this shifts the response
   distribution toward deliberation mode: the model writes the
   correct fix in synthesizer.md prose but never invokes
   `write_file`. Confirmed at n=10 via xarray-2905 trace inspection
   (21 read tool calls, 0 write calls, correct fix in prose).

   Specific n=75 regressions worth tracking:
   - pylint-4970: **strong → wrong_location** — model picked an
     adjacent line to the gold's edit. Localization noise, possibly
     unrelated to Lever 2.
   - sphinx-10435: **strong → empty_patch** — clear engagement loss.
   - sphinx-10449: **plausible → empty_patch** — the "fixed adjacent
     symptom" case I named yesterday. Now doesn't fix even the
     adjacent NameError.
   - 4 wrong_target → empty_patch — instances that previously
     attempted an off-target fix now give up entirely.

**Net direction**: target class hit (new_file_in_diff → 0), modest
quality lift on the durable anchor (strong + plausible: 30 → 32, +2),
but offset by prose-mode growth (empty_patch +4). Net change in
non-empty patch presence: 0.

**Root cause of the prose-mode regression**: extra tokens in the
task prompt nudge marginal instances toward "let me think more"
behavior. The fix already exists at v1.4.1: `LUXE_WRITE_PRESSURE=1`
mid-loop intervention rescued `nothing-doc-config` from 33% FAIL →
0% over 10 reps. The flag is currently opt-in; not enabled in
`configs/single_64gb_swebench.yaml`.

**Fix / takeaway**:

1. **Lever 2 ships at v1.5.0 with the empirical caveat above.** The
   target-class win is real (new_file_in_diff → 0); the modest
   strong+plausible lift is within n=75 noise but directionally
   positive; the empty_patch regression is a known prior-shipped
   class with a known fix (LUXE_WRITE_PRESSURE).

2. **Recommended next step** (NOT this session): enable
   `LUXE_WRITE_PRESSURE=1` in the swebench config and rerun n=75.
   Predicted result: empty_patch ↓ toward baseline (~26) while
   keeping new_file_in_diff at 0. If that holds, ship v1.5.0
   (Lever 2) + v1.5.1 (write-pressure default flip) as a paired
   tag with a clean attribution table.

3. **General durable rule**: **prompt-side context additions cost
   tokens and may push borderline instances into prose-mode**. When
   adding any prompt block (.sdd contracts, examples, plan
   templates), measure both the target-class win AND the
   prose-mode delta on the same fixture set. Bundle write-pressure
   defaults if a regression appears.

4. **Add a per-instance rerun probe** for the strong → empty
   regressions (sphinx-10435, sympy-13091) to distinguish "Lever 2
   broke it" from "n=75 variance" — temp=0 is mostly deterministic
   for SWE-bench but extra tokens in the prompt change the input,
   so identical-trajectory determinism doesn't apply across pre/post.

**Affected files**: results in
`acceptance/swebench/post_specdd_v15_n75/rep_1/predictions.json`;
delta report via `python -m benchmarks.swebench.compare_runs`. No
source changes from the rerun itself.
