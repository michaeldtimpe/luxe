# luxe — session resume document

## Current state — 2026-05-04

**v1.4.1 fixes shipped + validated**: 18 commits since v1.4.0 (`1e545e8 → 10b352b`). Working tree clean. 461 tests passing.

**`nothing-doc-config × 10` validation: 10/10 PASS** with `LUXE_WRITE_PRESSURE=1` + `LUXE_REPROMPT_ON_DOC=1`. Three rescue regimes:
- 8/10 clean engagement (no rescue)
- 1/10 Mode B mid-loop write-pressure (rep 9 — gate fired at step 6 with 18 tools + 5024 tokens + 0 writes; model wrote 138 lines after injection)
- 1/10 reprompt rescue (rep 5 — Mode B's threshold barely missed at step 5 entry; main pass produced 28k prose chars + 0 writes; reprompt landed and model wrote 147 lines)
- Linter fix held: 0 unresolved citations across all 10 reps
- Historical ~33% FAIL rate on this fixture → 0%

**External agent benchmark adapters scaffolded** (per `~/.claude/plans/fancy-honking-lerdorf.md`):
- BFCL v3 — `benchmarks/bfcl/`: schemas + adapter + grade + run.py + aggregate.py + tests
- SWE-bench Verified — `benchmarks/swebench/`: fixtures + stratify + adapter + run.py (preds-only) + harness.py + compare.py (paired McNemar) + tests
- Frozen subset: `benchmarks/swebench/subsets/v1_baseline_n75.json` (75 instances, 12 repos, per-repo cap 8)

**BFCL pre-SpecDD baseline complete** (`acceptance/bfcl/pre_specdd_v141/rep_1/`, 2026-05-04):
- `irrelevance`: 220/240 = **91.67%** (avg 20.9s)
- `multiple`: 166/200 = **83.00%** (avg 18.5s)
- `simple_python`: 330/400 = **82.50%** (avg 16.6s)
- `parallel`: 132/200 = **66.00%** (avg 20.1s)
- `parallel_multiple`: 98/200 = **49.00%** (avg 24.3s)
- **TOTAL: 946/1240 = 76.29%** in ~3.5h wall
- **Parallel cliff**: parallel_multiple sits 33pp below single-call avg. Multi-call planning is the model's weakness and the most likely place SpecDD agent-mode will show value. Single-call categories are too saturated for big SpecDD gains.
- 43/70 `simple_python` failures = `no_tool_call_emitted` — same prose-mode tendency that drove the SWE-bench reproducer-script regression in the smoke run.

**SWE-bench preds-only smoke** (`acceptance/swebench/smoke_2026_05_04/`):
- 3 astropy instances; 2/3 produced non-empty patches; ~3 min wall/instance
- Pipeline validated end-to-end (clone → agent → diff → predictions.json)
- **Critical finding**: model creates reproducer scripts (`repo_root/test_sep.py`) instead of editing source files. SWE-bench-specific prompting needed before a real n=75 baseline run.

Champion unchanged: `Qwen3.6-35B-A3B-6bit` at temperature=0.0 on oMLX.

---

## ⚡ Resume here — pending decisions

### A. Tag v1.4.1 + decide on Mode B promotion (~5 min)
Three fixes are validated and worth tagging:
- Citation-linter bare-filename fallback (Mode A)
- Mode B mid-loop write-pressure (opt-in via `LUXE_WRITE_PRESSURE=1`)
- Sidecar regrade lint re-run

```bash
git tag -a v1.4.1 -m "v1.4.1: linter bare-filename fix + Mode B opt-in + regrade lint re-run"
```

Decision: promote `LUXE_WRITE_PRESSURE=1` from opt-in to default? 10/10 validation supports it but a bench-wide ×3 reps with the flag would be the rigorous gate before promotion. ~75 min wall.

### B. SWE-bench smoke re-run + n=75 baseline (next)
**Shipped (commit `60acdc7`)**: `swebench_bugfix` PromptVariant + `swebench_strict_only` TaskOverlay + `configs/single_64gb_swebench.yaml` (now the swebench runner default) + mechanical `smoke_inspect.py`. Prompt forbids new files, treats reproducers as search context, requires one tool call per response (parallel-cliff defense), prescribes a linear locate→read→edit→verify protocol, includes a continued-exploration permission clause to avoid shallow edits. Inspector applied to the historical smoke confirms 0/3 PASS — the regression we're fixing.

Next steps:
1. Re-run the same 3-instance astropy smoke with the new config (~10 min).
2. `python -m benchmarks.swebench.smoke_inspect --predictions acceptance/swebench/smoke_2026_05_04_v2/predictions.json` — expect 3/3 PASS before escalating.
3. If smoke clean, run n=75 against `subsets/v1_baseline_n75.json` (~5h wall preds-only; Docker harness scoring still gated by RESUME option D).

### C. BFCL — second rep + agent-mode probe on parallel categories (~5h)
Single-call rep is saturated; not worth re-running. The actionable next signal is:
1. Second rep across all 5 raw-mode categories for variance bounds on the 76.29% number (~3.5h).
2. Agent-mode probe on `parallel` + `parallel_multiple` to test whether the agent loop lifts the cliff (~1.5h for 400 problems if agent-mode wall ≈ raw-mode wall).

### D. Docker confirmation for SWE-bench harness scoring
`pip install swebench` is done; harness wrapper at `benchmarks/swebench/harness.py` is wired but defers Docker calls. Confirm Docker Desktop is up + ~10GB free + acceptable RAM headroom (model ~30GB + container layer 6-8GB on a 64GB box is tight but viable). Then:

```bash
.venv/bin/python -c "
from pathlib import Path
from benchmarks.swebench.harness import run_harness, write_harness_summary
res = run_harness(Path('acceptance/swebench/smoke_2026_05_04/predictions.json'),
                  output_dir=Path('acceptance/swebench/smoke_2026_05_04/'),
                  run_id='luxe_smoke')
write_harness_summary(res, Path('acceptance/swebench/smoke_2026_05_04/harness_summary.json'))
"
```

### E. Lever 2 prep — `~/.claude/plans/fluffy-brewing-lemur.md`
~2-3 sessions per the SpecDD plan. Decoupled from benchmark work; can parallelize.

### F. Smaller cleanup items
- Retire v1.3 directive reprompt code in `cli.py` (RESUME old option C, ~15 min)
- `min_added_lines` as per-requirement predicate kind in `src/luxe/spec.py`
- `ast_query` and `manual` predicate full integrations (currently stubbed in `spec_validator.py`)
- Tune Mode B thresholds based on broader bench data (currently 10 tools / 4000 tokens / step 5)

---

## Memory entries (read first)

Bench-substrate / failure-mode work:
- `project_doc_config_three_modes.md` — A/B/C decomposition of doc-config variance
- `project_v1_4_1_mode_b_validation.md` — 10/10 PASS validation
- `project_v1_4_validation.md` — original v1.4.0 3-rep result (9.67/10 effective)
- `project_compound_goal_audit.md` — SpecDD premise empirically thin
- `project_loose_grader_audit.md` — 5/10 graders looser than goal text (closed at v1.4 spec layer)

External benchmark program:
- `project_external_benchmark_program.md` — SWE-bench n=75 + BFCL v3 plan
- `project_bfcl_pre_specdd_baseline.md` — 85.94% combined, 16-21s/problem
- `project_swebench_smoke_2026_05_04.md` — pipeline validated, prompt gap exposed

Diagnostic / process:
- `feedback_instrument_loop_first.md` — `LUXE_LOG_TOOL_CALLS=1` before adding prompt mass
- `feedback_verify_fixture_grader.md` — read base file before debugging model behavior
- `feedback_replicate_borderline_fixtures.md` — 3× replicate before claiming regression
- `feedback_offline_cache_refs.md` — don't read `origin/<branch>` in offline cache
- `feedback_offer_long_running_commands.md` — bench >5 min: hand off, don't auto-run
- `feedback_validate_first.md` — cheap probe before multi-hour runs

Closed non-starters:
- `project_mlx_use_ane_probe.md` — feature doesn't exist in MLX
- `project_omlx_logprobs_unsupported.md` — oMLX silently strips `logprobs:true`
- `project_qwen3_migration.md` — fully reverted

Latent / open:
- `project_regrade_local_origin_bug.md` — fixed in v1.4.1 (`scripts/regrade_local.py` re-runs linter against synthesizer.md)
- `project_gh_auth_flake.md` — open but mitigated by `--retry-errors`
- `project_lmstudio_loop.md` — open
- `project_omlx_metal_crashes.md` — latent

---

## 30-second orientation

**luxe** is an MLX-only repo maintainer for Apple Silicon (oMLX backend on `localhost:8000`). Takes a goal + repo, opens a PR. Mono-only since v1.0 — single model, single agent loop, single `luxe maintain` command. Champion: `Qwen3.6-35B-A3B-6bit` in `configs/single_64gb.yaml`.

**What's shipped through v1.4.1**:
- v1.0 — mono-only; 10 fixtures; strict gates
- v1.1 — pinned work_dir default + manage_strict overlay → 9/10
- v1.2 — per-tool subphase pass: cve_lookup gated to manage; bash chain-hardening; read_file binary detection
- v1.3 — read_file dedup exemption + lpe-typing fixture surgery + reprompt-on-doc + `_diff_against_base` fix
- v1.4 — SpecDD Lever 1: programmatic Definition of Done; per-requirement spec validator; reprompt gate uses spec
- v1.4.1 — citation-linter bare-filename fallback (Mode A) + Mode B mid-loop write-pressure (opt-in) + sidecar regrade lint re-run

**What's queued**:
- v1.5.0 — SpecDD Lever 2 (`.sdd` parser, spec_resolver, tool-side Forbids, resume path, prompt injection)
- v1.6.0 — SpecDD Lever 3 (fixture-side `.sdd` contracts, methodology A/B)
- External benchmark baseline run (BFCL multiple/parallel + SWE-bench n=75 with curated prompt)

**Iteration model**: bench changes go through `scripts/regrade_local.py` for fast iteration on grader/linter logic without re-running luxe. Full bench re-runs reserved for end-of-phase confirmation.

---

## The bench-as-truth pattern

Every model claim goes through:

1. Run `python -m benchmarks.maintain_suite.run --variants <yaml>`.
2. Read the printed comparison table — `pass/fail/wall/tokens/bailouts` per cell.
3. **Inspect every PASS PR by hand** via the actual local-branch ref in the offline cache: `git -C ~/.luxe/fixture-cache/<repo> diff <base_sha>..<branch_name>`. **Do NOT use `origin/<branch>`** — the cache's stale GitHub-tracking refs point to old runs and silently mislead. Branch name is in `~/.luxe/runs/<run_id>/pr_state.json`.
4. Sidecar regrade with `scripts/regrade_local.py --output <dir>` for fast, faithful re-grading without re-running luxe (seconds vs 60-120 min). As of v1.4.1, re-runs the citation linter against the original synthesizer.md.

Real PASS count is always ≤ printed count. Every historical bake-off has had at least one false-positive PASS.

---

## Files of consequence

| Path | Purpose |
|---|---|
| `src/luxe/agents/single.py` | mono runner — agentic loop end-to-end |
| `src/luxe/agents/loop.py` | shared loop; **Mode B write-pressure injection (v1.4.1)** |
| `src/luxe/agents/prompts.py` | prompt registry + TaskOverlay; doc/manage strict variants |
| `src/luxe/citations.py` | diff-aware citation linter; **bare-filename fallback (v1.4.1)** |
| `src/luxe/spec.py` | SpecDD Lever 1 data model (`Requirement`, `Spec`, YAML round-trip) |
| `src/luxe/spec_validator.py` | SpecDD Lever 1 predicate evaluator + reprompt-text helper |
| `src/luxe/tools/base.py` | `dispatch_tool` (tool exceptions captured as retry-able errors) |
| `src/luxe/tools/fs.py` | write-time honesty guards |
| `src/luxe/backend.py` | `chat()` accepts `repeat_penalty`; `unload_model()`, `loaded_models()` |
| `src/luxe/cli.py` | `luxe maintain` (mono only); `--spec-yaml` for SpecDD reprompt gate |
| `src/luxe/config.py` | `RoleConfig` w/ system/task prompt + overlay ids + repeat_penalty |
| `benchmarks/maintain_suite/run.py` | bench harness; `Variant` carries prompt + overlay overrides |
| `benchmarks/maintain_suite/grade.py` | grading + strict gates + multi-variant `v1_release_gate` |
| `benchmarks/maintain_suite/fixtures.yaml` | the 10 v1 fixtures (each w/ `requirements:` block) |
| `benchmarks/swebench/` | SWE-bench Verified adapter (preds-only + Docker harness wrapper + compare) |
| `benchmarks/bfcl/` | BFCL v3 adapter (raw + agent modes, schema converter, grader) |
| `configs/single_64gb.yaml` | the only config — currently `Qwen3.6-35B-A3B-6bit` |
| `scripts/regrade_local.py` | sidecar regrade w/ citation re-run (v1.4.1) |
| `scripts/register_omlx_models.py` | symlink HF cache → `~/.omlx/models/` |
| `lessons.md` | running postmortem; latest entry covers v1.4.1 Mode B/A combo validation |
| `~/.claude/plans/fancy-honking-lerdorf.md` | external benchmark plan (SWE-bench n=75 + BFCL v3) |
| `~/.claude/plans/fluffy-brewing-lemur.md` | SpecDD plan (Levers 1/2/3) |

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

API key for HTTP requests: `export OMLX_API_KEY=omlx-sdb25582k3mq8pf9` (in user's shell init; the bench harness reads it).

**Restart oMLX** any time `settings.json`, `model_settings.json`, or new symlinks land: `brew services restart omlx`.

---

## Trace instrumentation

`LUXE_LOG_TOOL_CALLS=1` emits per-tool-call and per-step events to the run's `events.jsonl`. Permanent debugging knob (off by default, zero overhead when off):

```bash
LUXE_LOG_TOOL_CALLS=1 python -m benchmarks.maintain_suite.run --id <fixture> --force
RUN=$(jq -r .luxe_run_id acceptance/<output>/.../state.json)
jq -c 'select(.kind=="tool_call" or .kind=="tool_step_done")' ~/.luxe/runs/$RUN/events.jsonl
```

Mode B fix events (when `LUXE_WRITE_PRESSURE=1`):
```bash
jq -c 'select(.kind=="write_pressure_fired")' ~/.luxe/runs/$RUN/events.jsonl
```

---

## Critical gotchas

- **`oMLX` `idle_timeout: null` keeps models resident forever.** Set to `1800`.
- **`luxe maintain` post-run unload fires by default.** Bench mode uses `--keep-loaded` (already passed by `_luxe_maintain` in `run.py`).
- **At temp=0 the variance collapses to deterministic vectors** (probe_a == probe_b across all 10 fixtures on 2026-05-01 PM). Master plan's "±1 deltas are noise" rule applied at temp=0.2; at temp=0 a 1-fixture delta IS the signal.
- **Offline mode caps every fixture at 4/5** — `gh pr create` always fails (no GitHub remote), so `pr_opened` (1pt of 5) never fires offline. Every PASS reads as 4/5; gate math (≥8 fixtures with score ≥4) still works correctly.
- **`origin/<branch>` in offline-cache repos is a stale-ref trap** — post-2026-05-01 runs push to local branches (`refs/heads/...`) which do NOT update remote-tracking refs. Use `git diff base..<branch>` (local ref) or sidecar regrade.
- **Dense >30B mxfp8 doesn't fit on 64GB Mac under load** — granite-4.1-30b-mxfp8 spiked 22GB+ wired and pushed system into swap. MoE models (Qwen3.6-35B-A3B at ~3B active) run comfortably; dense models don't.
- **`stuck_after_done` doesn't always mean failure** — Qwen3.6-35B-A3B often ships a real diff then trips the stuck-loop detector on cleanup. Distinguishes from `stuck_no_output` (never engaged).
- **`run.py` resume model treats `status: error` as `skip_done` by default** — if a sweep dies before any model invocation, re-launching without `--retry-errors` silently skips every fixture and prints a zeroed Summary. Either pass `--retry-errors` or `rm -rf` the output dir.

---

## Recent commit trail (most recent first)

```
10b352b  docs: RESUME.md — autonomous slot summary (Mode B + BFCL + SWE-bench smoke)
86c3b4e  benchmarks/bfcl/aggregate: post-hoc summary builder from per-problem JSONs
096fdee  docs: RESUME.md — Mode B 10/10 PASS validation result
dc4c5df  lessons.md: v1.4.1 Mode B/A combo validation — 10/10 PASS on nothing-doc-config × 10
19d2202  benchmarks/swebench/compare: add CLI entry — `python -m benchmarks.swebench.compare`
c64f7ad  benchmarks/swebench/compare: paired McNemar + Wilson CI for pre/post analysis
41e47c1  tests/test_bfcl_adapter: load smoke + dispatch shape against real bfcl_eval data
e0da66e  benchmarks/swebench/harness: Docker harness wrapper (defers Docker call)
8de1e46  docs: RESUME.md updated for late 2026-05-03 work
399ed66  benchmarks/swebench: adapter + preds-only runner (no Docker harness yet)
37cd1c8  .gitignore: exclude benchmarks/<bench>/subsets/raw/ (re-downloadable HF dumps)
656e83a  benchmarks/swebench: freeze v1_baseline_n75 subset from real Verified data
2f58019  benchmarks/bfcl: adapter + grader + runner (raw mode validated, agent mode plumbed)
bb92b09  docs: lessons.md + RESUME.md updates for v1.4.1 fixes + Mode B/C decomposition
71b4c7e  benchmarks/bfcl: PRELIMINARY scaffolding (BFCL v3 adapter)
42d2d51  benchmarks/swebench: PRELIMINARY scaffolding (SWE-bench Verified adapter)
1d5b006  v1.4.1: citation-linter bare-filename fallback + Mode B write-pressure + regrade lint re-run
1e545e8  docs: RESUME.md — five next-session options + cleanup of v1.3-era resume content
707bab8  v1.4.0: SpecDD Lever 1 — programmatic Definition of Done; first 10/10 bench
0f611d0  v1.4-prep: SpecDD Lever 1 — 5 loose-grader fixture migrations (step 6 complete)
```

---

## When in doubt

`git log --oneline -20` tells the trajectory. `lessons.md` has postmortems of every failure pattern. The user prefers terse, action-oriented responses — don't summarize what they can read; tell them the next step.

The user is comfortable with auto mode but draws hard lines on destructive shared-system actions (oMLX config, sudo, force-push, deletes outside their workspace). When in doubt, write the change but ask before applying. Do NOT push to remote unless explicitly asked.
