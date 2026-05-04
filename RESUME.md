# luxe — session resume document

Current state: **v1.4.1 in progress 2026-05-03 PM/late** — three fixes + adapter scaffolding for external benchmarks. Mode B validation in flight (bhnsu1i79).

**2026-05-04 early-AM update**: Mode B validation completed — **10/10 PASS** on `nothing-doc-config × 10` reps with `LUXE_WRITE_PRESSURE=1` + `LUXE_REPROMPT_ON_DOC=1`. Three rescue regimes observed: clean engagement (8/10), Mode B mid-loop write-pressure (1/10, rep 9), reprompt rescue (1/10, rep 5). Linter fix held: 0 unresolved citations across all 10 reps. Historical ~33% FAIL rate for this fixture collapsed to 0%.

BFCL raw mode on `simple_python` (400 problems) launching now via bdeuxmalt — first external agent benchmark data point.

**Late 2026-05-03 / early 2026-05-04 work** (commits 1d5b006 → dc4c5df):
- `1d5b006` v1.4.1 fixes: citation-linter bare-filename fallback + Mode B write-pressure + regrade lint re-run
- `42d2d51`, `656e83a`, `399ed66` SWE-bench scaffolding (data model, stratify, frozen n=75 subset, preds-only runner). No Docker harness yet (decision point #1).
- `71b4c7e`, `2f58019` BFCL v3 adapter (schemas, grade, run.py). Raw-mode validated via load smoke; live execution pending bench completion.
- `bb92b09`, `37cd1c8` lessons.md / RESUME.md / .gitignore tidy.

**Failure-mode decomposition for `nothing-ever-happens-document-config`** (was: "33% prose-mode" — actually three distinct bugs):
- **Mode A**: synthesizer prose truncates `bot/strategy/foo.py:42` to `foo.py:42`. Linter false-flags missing_file. → **fixed** by bare-filename fallback in `src/luxe/citations.py` (validated 3F→4P on diag rep 1).
- **Mode B**: prose-mode hallucination — agent declares "comprehensive picture" early, hallucinates content from priors, never writes. → **fix landed** (`LUXE_WRITE_PRESSURE=1` opt-in, mid-loop write-pressure injection in `src/luxe/agents/loop.py`); validation in flight.
- **Mode C**: out-of-range line-number hallucination in synthesizer prose (88 instances observed in postfix rep 5). Genuine citation lint failures; not a linter bug. Mode B fix may partially mitigate; structural fix deferred.

**Original 2026-05-03 PM validation outcome** (`acceptance/v1_4_validation_rep_{1,2,3}/`):
- Rep 1: 9/10 (`nothing-ever-happens-document-config` FAIL — variance fixture)
- Rep 2: 10/10
- Rep 3: 10/10
- Per RESUME's decision tree: 2/3 at 10/10 → "variance is real; bench effectively 9.67/10; document honestly; close session at B."

**External benchmarks plan**: `~/.claude/plans/fancy-honking-lerdorf.md` — SWE-bench Verified n=75 stratified subset + BFCL v3 Python-relevant subset, pre/post SpecDD Lever 2/3. Adapter code scaffolded; live runs pending Mode B validation completion.

**Frozen subsets**:
- `benchmarks/swebench/subsets/v1_baseline_n75.json` — 75 instances across 12 repos, per-repo cap 8, deterministic from seed 20260503.

**Today's ship sequence**:
- **v1.3.0** (`cd27e35`) — read_file dedup exemption + lpe-typing fixture surgery. Bench 8/10 → 9/10.
- **v1.3.1** (`162d1da`) — `_diff_against_base` undercount bug fix + directive reprompt for prose-mode.
- **v1.3.2** (`e22773e`) — `assert_gh_auth` retry-with-backoff + 3 perf-probe non-starters formally closed (ANE, F2.2 logprobs, spec-prefill revisit).
- **v1.4-prep** chain (`23827c1` → `0f611d0`) — 7 incremental commits laying down SpecDD Lever 1: data model, predicate evaluator, bench integration, prompt-template helpers, cli.py reprompt gate, fixture migrations.
- **v1.4.0** (`707bab8`, tag `v1.4.0`) — Lever 1 ship. 10/10 PASS validated end-to-end with `LUXE_REPROMPT_ON_DOC=1`. All 10 fixtures: `expected_outcome_passed=true` AND `spec_all_satisfied=true`. v1 release gate cleared.

Champion unchanged: `Qwen3.6-35B-A3B-6bit` at temperature=0.0.

**Honest framing of the 10/10 result** (per `lessons.md` v1.4.0 entry):
- 10/10 layers v1.3.0 fixture surgery + lucky variance roll on `nothing-doc-config`. Variance is structurally unchanged (~33% historical FAIL).
- SpecDD Lever 1's premise (compound-goal shadowing is the bench's primary ceiling) was empirically thin per `project_compound_goal_audit.md`. Lever 1 ships for **architectural value** (programmatic Definition of Done; tightened per-requirement grading), not bench-score lift.
- Tightened doc/manage specs (R2/R3 across 4 fixtures) didn't false-fail this run — model addresses every sub-deliverable in passing runs.

**Memory entries from this session** (read these first when resuming):
- `project_v1_3_dedup_fix.md` — v1.3.0 ship + investigation
- `project_compound_goal_audit.md` — 5/5 fixtures show full sub-deliverable completion
- `project_loose_grader_audit.md` — 5/10 graders looser than goal text (closed at spec layer in v1.4.0)
- `feedback_instrument_loop_first.md`, `feedback_verify_fixture_grader.md` — diagnostic principles
- `project_mlx_use_ane_probe.md`, `project_omlx_logprobs_unsupported.md` — closed non-starters

---

## ⚡ Resume here — five next-session options

Recommended order: **A then B**. The 10/10 result is the most consequential claim from the last session; spending an hour proving it holds across replicates is the highest-leverage move before committing to deeper work.

### A. Replicate v1.4.0 bench 2-3× (~50-75 min, mostly hands-off)

Confirms 10/10 isn't variance-fortunate. `nothing-doc-config` has ~33% historical FAIL rate; one PASS isn't proof of a real ceiling lift. Three independent runs:

```bash
cd /Users/michaeltimpe/Downloads/luxe && for i in 1 2 3; do
  echo "=== rep $i (v1.4.0 full bench validation) ==="
  rm -rf "acceptance/v1_4_validation_rep_$i"
  LUXE_REPROMPT_ON_DOC=1 .venv/bin/python -m benchmarks.maintain_suite.run \
      --variants benchmarks/maintain_suite/variants_v1_default.yaml \
      --output "acceptance/v1_4_validation_rep_$i" \
      --per-fixture-timeout 1800 \
      --work-dir ~/.luxe/bench-workspace \
      --all \
      --force 2>&1 | tail -10
  echo ""
done
```

Decision tree:
- **3/3 at 10/10** → strong claim; lessons.md update; ship v1.4.0 with confidence; end session at B.
- **2/3 at 10/10** → variance is real; one fixture FAIL across reps. Document honestly; bench is effectively 9.67/10. Close session at B.
- **<2/3 at 10/10** → either spec gates are missing failures the legacy grader caught, or substrate has shifted. Investigate before continuing to D/E.

### B. Pause / end session

Natural stopping point after replicates settle. v1.4.0 is shipped and tagged. Good place to step away if no other lever calls.

### C. Step 7 — retire v1.3 directive reprompt code (~15 min)

Cleanup. Removes the `additions == 0 AND len(prior_text) > 1000` branch in `cli.py` reprompt block. Trade-off: ad-hoc `luxe maintain` users (no `--spec-yaml`) silently lose reprompt entirely. Consider keeping deferred until evidence of ad-hoc usage patterns.

If pursuing: edit `src/luxe/cli.py` reprompt block to assert `loaded_spec is not None` when `LUXE_REPROMPT_ON_DOC=1`; raise a clear error if a user opts in to reprompt without providing a spec.

### D. Variance-fixing structural work (~half-day to a session)

Targets the only known residual weakness: `nothing-doc-config`'s ~33% prose-mode FAIL rate. Two candidate approaches:

1. **`prepend_to_file` tool affordance** — addresses Hypothesis B from the original lpe-typing review (top-of-file `edit_file` friction). Even though lpe-typing turned out to be fixture-surgery territory, a `prepend_to_file` tool would be useful for future doc fixtures and might shift `nothing-doc-config`'s prose-loop behavior.

2. **Synthetic write_file injection on prose-mode detection** — harness-side scaffolding: when the agent loop produces N reads with 0 writes, inject a synthetic tool result message indicating the model should consider `write_file`. Empirically targets the failure mode (0/1 prompt-based rescue rate observed; structural intervention may work where prompts didn't).

Either is an architectural addition. Pick one based on whether tool-affordance-extension or harness-scaffolding feels right for the codebase's direction.

### E. Lever 2 prep (~2-3 sessions per the SpecDD plan)

Ships at v1.5.0:
- `src/luxe/sdd.py` — `.sdd` file parser (markdown + section extraction)
- `src/luxe/spec_resolver.py` — directory walk + chain assembly + glob compilation
- Tool-side `Forbids` enforcement in `src/luxe/tools/fs.py`
- `src/luxe/cli.py` resume path — load `.sdd` chain on resume
- Worker prompt construction injects scoped `.sdd` chain

The plan calls for dogfooding: author internal `.sdd` files for `src/luxe/luxe.sdd`, `src/luxe/agents/agents.sdd`, etc. before enabling target-repo `.sdd` consumption.

Premise: ship Lever 2 only if Lever 1's architectural value delivered. Reassess after replicates land — if 10/10 is robust, Lever 2 is a defensible next investment; if variance dominates, structural work in D may take priority.

### F. (smaller items also queued)

- `min_added_lines` as a per-requirement predicate kind in `src/luxe/spec.py` (small; would close the legacy-floor gap noted in v1.4.0 lessons entry).
- `ast_query` and `manual` predicate kind full integrations (currently stubbed in `spec_validator.py`).
- Lever 3 (`v1.6.0`) — fixture-side `.sdd` contracts + methodology A/B with `scripts/audit_requirements.py`.

### Trace instrumentation (still on)

`LUXE_LOG_TOOL_CALLS=1` emits per-tool-call and per-step events to the
run's `events.jsonl`. Permanent debugging knob (off by default, zero
overhead when off). Use to diagnose any future agent-loop abort:

```bash
LUXE_LOG_TOOL_CALLS=1 python -m benchmarks.maintain_suite.run --id <fixture> --force
RUN=$(jq -r .luxe_run_id acceptance/<output>/.../state.json)
jq -c 'select(.kind=="tool_call" or .kind=="tool_step_done")' ~/.luxe/runs/$RUN/events.jsonl
```

The `key_hash` field uses the same `_call_key()` the detector compares —
duplicate pairs are unambiguous from the trace.

### Open hazards (not addressed in v1.4.0)

- **Latent ToolCache invalidation** — `ToolCache.get_or_run` has no
  invalidation. Currently moot in bench code path (no cache wired in
  for `run_single`) but a hazard if/when the cache gets used. Add
  `invalidate_for_write(path)` if/when relevant.
- **`min_added_lines` not in spec model** — fixtures with mechanical
  ports retain a legacy substantive-edit floor; spec validator can't
  represent it as a per-requirement predicate. Address in a future
  spec.py extension.

---

## 30-second orientation

**luxe** is an MLX-only repo maintainer for Apple Silicon (oMLX backend
on `localhost:8000`). Takes a goal + repo, opens a PR. **Mono-only**
since v1.0 — single model, single agent loop, single `luxe maintain`
command. Champion: `Qwen3.6-35B-A3B-6bit` in `configs/single_64gb.yaml`.

**What's shipped:** v1.2.0 (tag `df646b0`, per-tool subphase pass:
cve_lookup gated to `manage`, bash chain-hardening, read_file binary
detection, latent _REPO_ROOT bug closed). v1.1.0 features (manage_strict
overlay, pinned-work_dir default) all preserved.

**What's queued for v1.3 / v2.0:**
- Reprompt-on-doc lever — uncommitted in `cli.py` behind
  `LUXE_REPROMPT_ON_DOC=1`. Ship/revert decision pending morning
  replicate (see "Resume here" at top).
- ~~lpe-typing under-engagement~~ — three negative lever attempts.
  Recommend accepting as ceiling unless model is upgraded.
- MCP-mediated codebase slicing — independent value prop (reduces
  ingest size on large repos).
- MLX_USE_ANE=1 probe — decode-speed lever, no quality risk. Cheap.
- ~~Phased Mode v2~~ **deprioritized** — A2 re-measurement on pinned
  work_dir substrate showed 85.4% prefix-cache hit rate (HIGH per
  the plan's threshold). The cache is already warm; subtask scoping
  for cache warmth is a solution to a non-problem. See
  `project_prefix_cache_baseline.md`.

**Iteration model:** all grader iteration uses the sidecar regrade
tool (`scripts/regrade_local.py`) against existing acceptance dirs.
Full bench re-runs reserved for end-of-phase confirmation only.

---

## The bench-as-truth pattern

Every model claim goes through:

1. Run `python -m benchmarks.maintain_suite.run --variants <yaml>`.
2. Read the printed comparison table — `pass/fail/wall/tokens/bailouts`
   per cell. (As of `c6a83c6` the per-fixture output also shows
   `[HH:MM:SS] N/total ETA ~Xmin` headers.)
3. **Inspect every PASS PR by hand** via the actual local-branch ref
   in the offline cache: `git -C ~/.luxe/fixture-cache/<repo> diff
   <base_sha>..<branch_name>`. **Do NOT use `origin/<branch>` —** the
   cache's stale GitHub-tracking refs point to old runs and silently
   mislead. Branch name is in `~/.luxe/runs/<run_id>/pr_state.json`.
4. Sidecar regrade with `scripts/regrade_local.py --output <dir>`
   for fast, faithful re-grading without re-running luxe (seconds vs
   60-120 min). Reads cache LOCAL branches via `git clone --local`,
   which sidesteps the stale-ref trap.

Real PASS count is always ≤ printed count. Every historical bake-off
has had at least one false-positive PASS — and as of Phase 0 the
strict gates (destructive_diff, role_name_leak, placeholder_diff)
actually fire at grading time, which they previously didn't.

---

## Files of consequence

| Path | Purpose |
|---|---|
| `src/luxe/agents/single.py` | mono runner — agentic loop end-to-end |
| `src/luxe/agents/loop.py` | shared loop with token-interval logging + repeat_penalty plumbing |
| `src/luxe/agents/prompts.py` | **prompt registry + TaskOverlay (Branch B)** |
| `src/luxe/citations.py` | diff-aware citation linter + ValidatorEnvelope dataclasses |
| `src/luxe/tools/base.py` | `dispatch_tool` (P0.1: tool exceptions captured as retry-able errors) |
| `src/luxe/tools/fs.py` | write-time honesty guards |
| `src/luxe/backend.py` | `chat()` accepts `repeat_penalty`; `unload_model()`, `loaded_models()` |
| `src/luxe/cli.py` | `luxe maintain` (mono only), `luxe pr/runs/unload/check/serve` |
| `src/luxe/config.py` | `RoleConfig` w/ `system_prompt_id`, `task_prompt_id`, **`task_overlay_id`**, `repeat_penalty` |
| `benchmarks/maintain_suite/run.py` | bench harness; `Variant` carries prompt + overlay overrides |
| `benchmarks/maintain_suite/grade.py` | grading + strict gates + multi-variant `v1_release_gate` (per-cell, not aggregate) |
| `benchmarks/maintain_suite/fixtures.yaml` | the 10 v1 fixtures |
| `benchmarks/maintain_suite/variants_v1_default.yaml` | single-cell production variant (baseline + champion) |
| `benchmarks/maintain_suite/variants_prompt_shaping.yaml` | **Phase 1 6-cell sweep** (executed 2026-04-30; no Gate A winner) |
| `benchmarks/maintain_suite/variants_task_type_overlay.yaml` | **Branch B 2-cell sweep — next to run** |
| `configs/single_64gb.yaml` | the only config — currently `Qwen3.6-35B-A3B-6bit` |
| `scripts/register_omlx_models.py` | symlink HF cache → `~/.omlx/models/`; takes `--models-file <json>` |
| `scripts/regrade_phase2.py` | apply strict gates to existing results |
| `lessons.md` | running postmortem; latest entries explain Phase 1 + multi-variant gate bug |
| `~/.claude/plans/v1-ship-and-prompt-sweep.md` | **master plan** for v1.0 release path |
| `~/.claude/plans/jiggly-baking-kahan.md` | Phase 1 inner-loop plan (executed) |
| `~/.claude/plans/task-type-overlays.md` | Branch B inner-loop plan (next) |

---

## oMLX configuration

`~/.omlx/settings.json`:
```json
"max_model_memory": "36GB",
"idle_timeout": { "idle_timeout_seconds": 1800 }
```

System-level Metal wired ceiling — kept aligned with `max_model_memory`:
```bash
sudo sysctl iogpu.wired_limit_mb=36864
echo "iogpu.wired_limit_mb=36864" | sudo tee -a /etc/sysctl.conf
```

`~/.omlx/model_settings.json` has explicit per-model entries for every
champion-tier model. Add new entries when registering new models.

**Restart oMLX** any time `settings.json`, `model_settings.json`, or new
symlinks land: `brew services restart omlx`.

---

## Bake-off history

| Phase | Date | Variants | Real PASS leader |
|---|---|---|---|
| Phase 1 — mono shootout | 04-28 | qwen-coder 7B/14B/32B mono | 14B (2/5) by tiebreaker |
| Phase 2 — head-to-head | 04-28 | mono/swarm/micro × {14B, 1.5B} | mono 14B + swarm 14B tied at 2/5 real |
| Phase 3 — phased mode | 04-29 | phased__qwen-coder-14b | 0/5 real |
| Mono precision | 04-29 | 14B-8bit, 32B-6bit, 30B-A3B-6bit, abliterated-14B-6bit | 30B-A3B-6bit (2-3/5) but bimodal |
| Overnight MoE | 04-29 | qwen3.6-35B-A3B {4,6}bit, gemma-4-26B-A4B {4,8}bit | qwen3.6-35B-A3B-6bit (regraded 0/20 — every printed PASS was false) |
| 8-bit completion | 04-30 | qwen3.6-35B-A3B-8bit, qwen-coder-32B-8bit, qwen3-coder-30B-A3B-8bit | none beat 6-bit |
| Granite-4.1 (3b only) | 04-30 | granite-4.1-3b-bf16 | 1/10 real (model too small for the loop) |
| v1 baseline (post P0 fixes) | 04-30 | qwen3.6-35B-A3B-6bit | 5/10 → regraded 3/10 (40% false-positive). 05-01 re-run was 6/10 → regraded 4/10. Sampling variance is real at temp=0.2 |
| **Phase 1 prompt-shaping** | **05-01** | **6 cells: baseline + baseline-rp + cot + sot + hads + combined-rp** | **printed 33/60 → regraded 8/60 (76% inflation). Phase 1's "structural prompts hit 4/4 implement" finding was almost entirely a false-positive artifact of the broken grader.** |
| **temp=0 baseline (Phase 0 grader fixed)** | **05-01 PM** | **qwen3.6-35B-A3B-6bit @ temp=0** | **5/10 stable across 2 runs; per-task: impl 4/4, doc 1/5, manage 0/1** |
| **v1.0.0 ship confirmation** | **05-01/05-02** | **qwen3.6-35B-A3B-6bit @ temp=0 (production config) + Phase-0/surgery/Branch-C fixes** | **8/10 (impl 4/4, doc 4/5, manage 0/1). First honest pass count.** |
| **Phase v1.1 variance experiment** | **05-02** | **2-cell A/B: champ-no-overlay + champ-manage-strict, pinned work_dir** | **no-overlay 8/10 (matches v1.0 ship deterministically); overlay 9/10 (deps-audit closes; no regressions). Confirmed work_dir randomness was the dominant temp=0 variance source.** |
| **v1.1.0 ship** | **05-02** | **qwen3.6-35B-A3B-6bit @ temp=0 + manage_strict_only overlay + pinned work_dir** | **9/10 (impl 4/4, doc 4/5, manage 1/1). Only remaining FAIL is lpe-typing under-engagement.** |

Full results: `acceptance/<phase>/comparison.json` per phase dir.

> **Caveat on pre-Phase-0 results**: every row above 04-30 was graded
> against a grader that didn't fire its strict gates (Bug 2 — see
> `lessons.md`). A1 re-grade pass on 2026-05-02 confirmed the
> deflation is severe — `prompt_shaping` went 33/60 → 8/60 (76%
> false), `overnight_moe` went 5/20 → 0/20 (every "PASS" was
> gaming-shaped). Treat any pre-Phase-0 number as approximately 0.25-
> 0.7× of what's printed; re-grade with `scripts/regrade_local.py` if
> the
> historical numbers are load-bearing for a decision.

---

## Phase 1 outcome (pre-Phase-0-grader, retain for context)

`acceptance/prompt_shaping/comparison.json`:

| Cell | impl (4) | doc (5) | manage (1) | Total |
|---|---|---|---|---|
| **champ-baseline** | 3 | 3 | 1 | **7** |
| champ-baseline-rp__rp105 | 3 | 1 | 0 | 4 |
| champ-cot | **4** | 1 | 1 | 6 |
| champ-sot | **4** | 2 | 0 | 6 |
| champ-hads | **4** | 1 | 0 | 5 |
| champ-combined-rp | **4** | 1 | 0 | 5 |

> These numbers were graded **before** the Phase 0 grader fixes. Each
> cell almost certainly has 1-2 false-positive PASSes that
> `destructive_diff` / `placeholder_diff` would now catch. Re-grade
> with `scripts/regrade_local.py --output acceptance/prompt_shaping`
> to get current-grader numbers if the comparison is load-bearing.

Hidden inside: every prompt-shaped variant cleared the implement
ceiling (4/4). Original Branch B hypothesis was: apply the implement-
friendly framing only on implement+bugfix, keep baseline on
docs/manage; projected to 8/10 if baseline's 3+1 doc/manage held.

**That hypothesis is now obsolete.** The 2026-05-01 PM temp=0 baseline
showed implement is already at 4/4 at temp=0 without any structural
prompt — the implement gain Phase 1 attributed to CoT/SoT/HADS was
plausibly a baseline-variance artifact (Phase 1 ran at temp=0.2; the
sampling variance there explained baseline swinging 5→7 between
identical runs). The implement category being saturated at temp=0
means the `implement_via_cot` overlay has nothing to lift; promoting
it would not improve the gate. The real bottleneck is doc (1/5) and
manage (0/1), and Phase 1 showed structural prompts *regress* both.

---

## Strict gates currently in place

Tool-side (`src/luxe/tools/fs.py`, write-time): `_check_placeholder_text`,
`_check_role_path`, `_check_mass_deletion`.

Tool dispatch (`src/luxe/tools/base.py`): tool exceptions are now
captured as retry-able `ToolCall.error` strings instead of escaping
`run_agent`. Catches the absolute-path crash that previously killed
`neon-rain-document-modules` at wall=0s/tokens=0.

Post-PR strict gates (`benchmarks/maintain_suite/grade.py`):
`destructive_diff`, `role_name_leak`, `placeholder_diff` — fire
**pre-outcome** on write tasks (Phase 0 fix; previously these were
defined but never invoked from `grade_fixture`, so silently no-op).
`vacuous_test`, `orphan_file` — fire post-outcome-pass on
implement-task `tests_pass` outcomes specifically. `destructive_diff`
threshold: deletions/additions ≥ 5.0 AND deletions ≥ 30.

Grader-level controls per fixture: `expected_outcome.min_matches` and
`expected_outcome.min_added_lines`. Diff-aware: `_check_regex_present`
scans only `+` lines.

Multi-variant `v1_release_gate`: as of `b81b628`, the gate is per-cell,
not aggregate — a multi-cell sweep with N total passes ≥8 across cells
no longer prints a false `v1 release: YES`. Gate clears iff some cell
has ≥8/10.

Bailout categorization: `_classify_bailout` produces `stuck_after_done` /
`stuck_no_output` / `refusal` / `prose_only` / `no_engagement` /
`schema_confusion` / `context_overflow` / `no_diff_writes` / `aborted`.

---

## Bench output

Per-fixture (as of token-split change):
```
━━━ run 53/60  [19:43:35]  ETA ~24min  [variant]  fixture  [task_type]  goal...
      grading: ...
  → invoking `luxe maintain` (variant)
  PASS  [19:46:12 +2:37]  score=5/5  wall=157s  tokens=68k/15k/83k  gen_tps~96  ...
```

`tokens=in/out/total` — prompt vs completion vs sum, sourced from the
oMLX `usage` block summed over chat turns. `gen_tps~` is the wall-
bounded decode estimate (`completion_tokens / wall_s`); the tilde
flags that it includes tool-execution and inter-turn overhead, so it
understates raw MLX decode speed. Real prefill/decode TPS requires the
streaming-backend refactor (stage 2 of the token-telemetry work) — see
`scripts/`/no plan written yet. Captured per cell in `comparison.json`
as `avg_prompt_tokens`, `avg_completion_tokens`, `gen_tps_wall`.

Token-interval progress logging fires every 5000 completion tokens
(via `LUXE_TOKEN_LOG_INTERVAL`; 0 disables). Captured in
`acceptance/<output>/<variant>/<fixture>/stdout.log`.

---

## Resume command — sidecar regrade against fixed grader

The previous "Branch B sweep" resume command is **deferred** (Branch B's
hypothesis is obsolete at temp=0; see "Phase 1 outcome" caveat above).
The next launchable action is a sidecar regrade pass to confirm the
post-Phase-0 grader's results across both probe runs:

```bash
cd /Users/michaeltimpe/Downloads/luxe

# oMLX is not required for sidecar regrade — pure repo state inspection.
python scripts/regrade_local.py --output acceptance/v1_temp0_probe_a
python scripts/regrade_local.py --output acceptance/v1_temp0_probe_b
```

Each regrade takes seconds, not minutes. Output: `result_regraded.json`
written next to each fixture's `result.json`, plus a summary table on
stdout. Expected: 5/10 PASS for both probes (one fixture flips vs the
stored 6/10 — `neon-rain-document-modules` via `destructive_diff`).

**Optional E2E confirmation (~60-90 min)**: a single fresh bench run
against the fixed grader — only worth doing if you want to verify the
sidecar's regrade matches a from-scratch run end-to-end:

```bash
python -m benchmarks.maintain_suite.run \
    --variants benchmarks/maintain_suite/variants_v1_temp0_probe.yaml \
    --output acceptance/v1_temp0_probe_post_grader_fix \
    --per-fixture-timeout 1800 \
    --all
```

---

## After-the-bench checklist (post-Phase-0)

1. Read the printed comparison table; note pass count, which cell
   cleared (if multi-variant), and which gates fired (`destructive_diff`,
   `placeholder_diff`, `role_name_leak`, `vacuous_test`, `orphan_file`).
2. **Hand-grade every PASS** by reading the actual local-branch state
   in the cache: `git -C ~/.luxe/fixture-cache/<repo> diff <base_sha>..<branch_name>`.
   Branch name from `~/.luxe/runs/<run_id>/pr_state.json`. **Do NOT use
   `origin/<branch>`** — stale GitHub-tracking refs in the cache point
   to old runs.
3. **Pareto reporting** (per `~/.claude/plans/task-type-overlays.md`
   Verification §4): record `implement` / `document` / `manage` pass
   counts separately, plus bailout-category distribution. Even when the
   8/10 ship gate fires, the per-task-type breakdown is the diagnostic
   surface.
4. If a multi-variant sweep cleared 8/10 in a single cell: ship per
   the Branch A path (master plan §Phase 2 Branch A).
5. If 7/10 or below: don't proceed to Branch B with the existing
   `implement_via_cot` overlay — it has nothing to lift at temp=0
   (implement is already 4/4 baseline). Decide between (a) a new
   overlay targeting doc/manage, (b) Branch C calibration relax for
   the gate-side miss on `nothing-config`, or (c) accept the gate as
   unreachable on the current fixture set and shift conversation to
   fixture surgery / model selection.

---

## Open work / next steps

Closed in Phase v1.0 / v1.1 / v1.2 (commits trace from `eb2bdf0` through
`v1.2.0` tag `df646b0`):
- Branch C calibration on `nothing-config` (regex now accepts
  markdown-style UPPER_SNAKE listings).
- Fixture surgery on `lpe-typing`, `neon-rain-modules`,
  `isomer-quickstart`.
- `pr_opened` offline-mode 4/5 cap documented as expected.
- Gemma-4 dead settings entries removed.
- Historical bake-offs re-graded.
- A3 specprefill probe — reverted (~5% gain, didn't clear 15% gate).
- B1 `document_strict` overlay (abstract) — negative result;
  infrastructure registered but not promoted.
- B2 `manage_strict` overlay — POSITIVE result; promoted in
  production. Closes deps-audit.
- Variance investigation — confirmed random tempdir leak as
  dominant temp=0 variance source. Pinned-work_dir default wired
  into `run.py`; future bench runs are deterministic on the
  pinned substrate.
- v1.1.0 ship at 9/10.
- **Per-tool subphase pass (v1.2.0)**: `cve_lookup` (gated to manage
  task_type after surface-bloat regression discovery), bash chain
  hardening, read_file binary detection, latent `_REPO_ROOT` import-bind
  bug. 12 other tools audited solid.
- **Procedural document_strict overlay (2026-05-03)**: revised B1
  overlay with explicit decomposition + self-review. Reprobe negative —
  reverted. lpe-typing diff still +2/-1.
- **Variance discovery (2026-05-03)**: nothing-doc-config has ~33%
  FAIL rate at n=3 replicates. v1.1's "9/10" was variance-fortunate;
  effective ceiling was always 8-9/10. Implement category remains
  deterministic at temp=0; doc/manage are not.

Remaining for v2.0 (priority-ordered after the morning replicate):

1. **lpe-rope-calc-document-typing under-engagement** — THREE negative
   prompt/runtime lever attempts. Genuinely model-limited at this scale.
   Remaining viable levers: model upgrade, few-shot examples in the
   doc_strict prefix (untested but lower expected ROI given three
   misses), specialized re-prompt tool that does the missing edit
   deterministically (bench-gaming, not production). **Recommendation:
   accept as ceiling; pivot.**
2. **Reprompt-on-doc lever ship/revert decision** — depends on the
   morning replicate (see "Resume here" at top of this file). If 3/3
   PASS, lever ships at v1.3; if not, revert.
3. **Per-tool refinement subphases** — see
   `project_tool_subphases_and_cve_lookup.md`. v1.2 closed cve_lookup +
   bash + read_file + _REPO_ROOT. Template validated. Future tools
   added to the surface should follow the same shape AND get task_type
   gating to avoid the surface-bloat trap (cve_lookup precedent).
4. **MLX_USE_ANE=1 probe** — queued; see `project_mlx_use_ane_probe.md`.
   Decode-speed lever; no quality risk. Smaller win expected given
   85.4% cache hit rate, but cheap to test.
3. ~~**Phased Mode v2**~~ — **deprioritized 2026-05-02 PM**. A2
   re-measurement on the pinned-work_dir substrate showed 85.4%
   prefix-cache hit rate (HIGH; well above the 65% threshold).
   The architectural premise ("subtask scoping warms the cache")
   is invalidated by the measurement — the cache is already warm.
   See `project_prefix_cache_baseline.md` for the per-request data.
   Subtask scoping might still help for *context-window pressure*
   reasons (less to fit in 32k context) but the cache-warmth
   motivation is gone, and ingest reduction is better addressed by
   MCP slicing (item 2).
3. **F2.1 IFS-lite** — refactor `expected_outcome` into weighted
   sub-instructions; report Instruction Following Score per cell.
   Out of v1.1 scope; queue for v1.2 / v2.0.
4. **F2.2 logprob capture** — 15-min probe to test if oMLX honors
   `logprobs: true`. Out of v1.1 scope; queue for v1.2 / v2.0.
5. **gh auth flake mitigation** — `assert_gh_auth()` intermittently
   fails mid-bench (observed twice on 2026-05-02; see
   `project_gh_auth_flake.md`). Cheap defense: retry-with-backoff
   in `assert_gh_auth()`. Not blocking; mitigated by `--retry-errors`.

---

## Critical gotchas

- **`oMLX` `idle_timeout: null` keeps models resident forever.** Set to
  `1800`.
- **`luxe maintain` post-run unload fires by default.** Bench mode uses
  `--keep-loaded` (already passed by `_luxe_maintain` in `run.py`).
- **Path-style HF symlinks need oMLX restart.** Adding a model to
  `~/.omlx/models/` doesn't make it appear in `/v1/models` until restart.
- **`mlx-community` chat templates are inconsistent.** StarCoder2-3B,
  CodeGemma-2B, Gemma-4-26B-A4B all ship with empty `chat_template`.
- **Sampling variance at N=10 is large at temp=0.2** — Phase 1 baseline
  swung 5→7 between identical runs. **At temp=0 the variance collapses
  to deterministic vectors** (probe_a == probe_b across all 10 fixtures
  on 2026-05-01 PM). For ship decisions, prefer temp=0 over temp=0.2 —
  noise floor disappears. Master plan's "±1 deltas are noise" rule
  applied at temp=0.2; at temp=0 a 1-fixture delta IS the signal.
- **Offline mode caps every fixture at 4/5** — with the local-cache
  `origin`, `gh pr create` always fails (no GitHub remote), so
  `pr_opened` (1pt of 5) never fires offline. Every PASS reads as
  4/5, every FAIL reads as 1/5 (citations only) or 0/5 (no diff).
  This is consistent across all fixtures (uniform, no false signal),
  so the gate math (≥8 fixtures with score ≥4) still works correctly.
  Decision recorded 2026-05-02: *don't* add auto-detect logic to
  drop the gate offline — it'd be a new failure surface for cosmetic
  gain. The 4/5 cap is the offline-mode signature; treat it as
  expected rather than as a bug.
- **`origin/<branch>` in offline-cache repos is a stale-ref trap** —
  `~/.luxe/fixture-cache/<repo>/refs/remotes/origin/...` was populated
  when the cache was first cloned from GitHub, then never updated.
  Post-2026-05-01 runs push to local branches (`refs/heads/...`) which
  do NOT update the remote-tracking refs. Reading
  `git diff base..origin/<branch>` reads ancient state and silently
  misleads. Use `git diff base..<branch>` (local ref) or sidecar
  regrade. Documented in the 2026-05-01 lessons.md entry.
- **Dense >30B mxfp8 doesn't fit on this hardware tier under load** —
  granite-4.1-30b-mxfp8 spiked 22GB+ wired during forward pass and
  pushed the system into swap. MoE models (Qwen3.6-35B-A3B at ~3B
  active) run comfortably at the same static weight size; dense models
  don't. mxfp8 dequant kernels in MLX aren't yet optimized.
- **`stuck_after_done` doesn't always mean failure** — Qwen3.6-35B-A3B
  often ships a real diff then trips the stuck-loop detector on cleanup.
  Distinguishes from `stuck_no_output` (never engaged).
- **Bench `wall=0s tokens=0` was a flake mode caused by unhandled tool
  exceptions** — fixed in P0.1 (`c2b8484`). If you see it again, an
  uncaught exception escaped `dispatch_tool`.
- **`run.py` resume model treats `status: error` as `skip_done` by
  default** — if a sweep dies before any model invocation (e.g. DNS
  failure on git clone, the 2026-05-01 Branch B incident), re-launching
  without `--retry-errors` silently skips every fixture and prints a
  zeroed Summary. Either pass `--retry-errors` or `rm -rf` the output
  dir before re-launching. No preflight network check exists; consider
  adding one before the next overnight slot.

---

## Recent commit trail (most recent first)

```
df646b0  v1.2.0: gate cve_lookup to manage task_type — close v1.2 implement regression
7ccba1f  feat: tool subphases — bash chain-hardening, read_file binary detection, fs.get_repo_root() fix
84e3ea8  feat: cve_lookup — surface OSV.dev aliases; closes second-order hallucination
c1e2a81  feat: cve_lookup tool — defeats audit hallucination via OSV.dev
198a4a6  docs: post-v1.1 A2 re-measurement — prefix-cache hit rate is HIGH (85.4%)
00c3dc7  v1.1.0: pinned work_dir default + manage_strict overlay → 9/10
ec88cd2  bench: pin work_dir default + A/B variant YAML for overlay experiment
89cc3e6  config: promote manage_strict_only overlay into production single_64gb
eb9960c  feat: manage_strict overlay — closes deps-audit stuck-loop
4cfdac9  v1.0: mono-only — delete swarm/micro/phased + tighten grader + 10 fixtures
```

---

## When in doubt

`git log --oneline -20` tells the trajectory. `lessons.md` has
postmortems of every failure pattern. The user prefers terse,
action-oriented responses — don't summarize what they can read; tell
them the next step.

The user is comfortable with auto mode but draws hard lines on
destructive shared-system actions (oMLX config, sudo, force-push,
deletes outside their workspace). When in doubt, write the change but
ask before applying.
