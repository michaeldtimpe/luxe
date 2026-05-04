# luxe — session resume document

## Current state — 2026-05-04 PM

**Working tree**: clean. **485 tests passing** (was 461 at session start; +24 added across BFCL resume, SWE-bench prompt overlay tests, gold-proximity inspector tests).

**SWE-bench n=75 baseline launched**, expected to be running when this resume is read. Output: `acceptance/swebench/pre_specdd_v141_n75/rep_1/`. Uses the default `configs/single_64gb_swebench.yaml` (baseline anti-reproducer prompt). The runner has resume capability — same command picks up where it left off after a crash.

**BFCL pre-SpecDD baseline complete** (`acceptance/bfcl/pre_specdd_v141/rep_1/`, 2026-05-04):
- `irrelevance`: 220/240 = **91.67%**, `multiple`: 166/200 = **83.00%**, `simple_python`: 330/400 = **82.50%**, `parallel`: 132/200 = **66.00%**, `parallel_multiple`: 98/200 = **49.00%**
- **TOTAL: 946/1240 = 76.29%** in ~3.5h wall
- **Parallel cliff**: parallel_multiple sits 33pp below single-call avg — multi-call planning is the model's weakness; single-call is too saturated for big SpecDD gains.

**SWE-bench prompt + inspector work this session** (post-v1.4.1):
- Anti-reproducer overlay shipped: `swebench_bugfix` PromptVariant + `swebench_strict_only` TaskOverlay + `configs/single_64gb_swebench.yaml`. Prompt forbids new files, treats reproducer snippets as search context, requires one tool call per response (parallel-cliff defense), prescribes linear locate→read→edit→verify protocol, includes continued-exploration permission clause.
- **n=10 A/B** (`probe_n10.json` — 4 easy + 6 medium across 10 distinct repos): baseline 6 strong + 3 plausible + 1 wrong_location; **counterexample-heuristic variant regressed two gold-matches to empty** (matplotlib-13989, astropy-13453). Heuristic functions as conditional intervention, not global modifier — preserved in tree as negative control (`single_64gb_swebench_counterexample.yaml`) but NOT promoted.
- **Inspector v2** with gold-proximity tier (`benchmarks/swebench/smoke_inspect.py --gold-source`): five signals (file match, line-based hunk proximity, hunk coverage, hunk count, size, token overlap) producing tiered verdict (strong / plausible / wrong_shape / wrong_location / wrong_target). Mechanical PASS/FAIL alone overstates quality (was reading 10/10 against a 4-6/10 real-fix reality on n=10).
- **Refined failure-mode taxonomy**: (a) commitment / (b1) transformation pattern / (b2) multi-site consistency / (b3) true design gap / (c) localization / (d) already passing / (e) hypothesis-stall. Per the n=10 review, b2 is the most common non-d class; only b1 is realistically prompt-tractable.

Champion unchanged: `Qwen3.6-35B-A3B-6bit` at temperature=0.0 on oMLX.

---

## ⚡ Resume here — n=75 result analysis

The expected workflow when you start the next session:

### Step 1 — gold-proximity tier on the n=75 output

```bash
cd /Users/michaeltimpe/Downloads/luxe && \
.venv/bin/python -m benchmarks.swebench.smoke_inspect \
    --predictions acceptance/swebench/pre_specdd_v141_n75/rep_1/predictions.json \
    --gold-source benchmarks/swebench/subsets/raw/verified.jsonl
```

Output format: per-instance line `<tier>  <instance_id>  file=Y/N loca=Y/N full=Y/N hunk=Y/N size=Y/N toke=Y/N  cov=X.XX  jac=X.XX`. Final summary line gives counts per tier + mechanical PASS rate + strong-or-plausible rate.

### Step 2 — manual review of plausibles + wrong_locations

The inspector is much more honest after v2 but still has known limitations:
- **Same-hunk-but-different-lines partial fixes** still score "strong" when model adds 1 of N gold's added lines within a single hunk (astropy-13453 pattern at n=10).
- **wrong_class-but-right-file** can score "plausible" with low coverage (sklearn-10297 at n=10 hit RidgeClassifierCV vs gold's RidgeCV — both in `ridge.py`).

Run this script to dump per-instance gold-vs-model diffs for the plausibles + wrong_locations:

```bash
.venv/bin/python <<'EOF'
import json
from pathlib import Path
preds = json.loads(Path('acceptance/swebench/pre_specdd_v141_n75/rep_1/predictions.json').read_text())
gold_jsonl = Path('benchmarks/swebench/subsets/raw/verified.jsonl').read_text().splitlines()
gold = {}
for line in gold_jsonl:
    if line.strip():
        r = json.loads(line)
        gold[r['instance_id']] = r['patch']
# Filter: change to whatever subset you want to inspect
target_iids = ['<instance_id_1>', '<instance_id_2>']  # paste from inspector output
for r in preds:
    if r['instance_id'] not in target_iids: continue
    print(f"=== {r['instance_id']} ===")
    print("GOLD:")
    print(gold[r['instance_id']][:1500])
    print("MODEL:")
    print(r['model_patch'][:1500])
    print('-' * 80)
EOF
```

### Step 3 — classify every failure into the taxonomy

Reference: `project_swebench_smoke_2026_05_04.md` has the full taxonomy with n=10 examples per class. Tally the n=75 distribution.

### Step 4 — compute the durable pre-SpecDD anchor number

Three numbers worth reporting in the lessons.md entry:
1. **Mechanical PASS rate** (non-empty patches that don't trip new_file/test_path/no_substantive)
2. **Strong-or-plausible rate** (gold-proximity tier)
3. **Manual high-confidence rate** (after spot-check; what would actually pass FAIL_TO_PASS)

The third number is the real pre-SpecDD anchor. Expected from n=10 extrapolation: **30-50%**.

### Step 5 — decision branch

| Outcome | Next move |
|---|---|
| 30-45% high-confidence | Solid anchor. Move to **SpecDD Lever 2 work** (~2-3 sessions, `~/.claude/plans/fluffy-brewing-lemur.md`). |
| <20% high-confidence | Localization or reasoning ceiling dominates. Inspect distribution of wrong_target / wrong_location instances. Maybe **minimality-bias A/B** (Step C from prior planning) before SpecDD. |
| >50% high-confidence | Either model is more capable than expected, or inspector is still optimistic. Spot-check 10 random "strong" claims; if real, celebrate. |

### Resume command — if the n=75 run was interrupted

Same command. Resume is automatic (per-instance summaries include `model_patch`):

```bash
LUXE_LOG_TOOL_CALLS=1 OMLX_API_KEY=omlx-sdb25582k3mq8pf9 \
.venv/bin/python -m benchmarks.swebench.run \
    --subset benchmarks/swebench/subsets/v1_baseline_n75.json \
    --output acceptance/swebench/pre_specdd_v141_n75/rep_1/
```

---

## Background tasks (non-blocking on n=75)

### A. Tag v1.4.1 (~5 min)
Three fixes shipped + validated; ready to tag:
```bash
git tag -a v1.4.1 -m "v1.4.1: linter bare-filename fix + Mode B opt-in + regrade lint re-run"
```
Decision pending: promote `LUXE_WRITE_PRESSURE=1` from opt-in to default? 10/10 validation supports it; bench-wide ×3 reps with the flag would be the rigorous gate before promotion (~75 min wall).

### B. Minimality-bias A/B (proposed but not yet shipped)
Reviewer-suggested orthogonal experiment to the counterexample heuristic. Tests whether the bottleneck is **over-editing**, not **under-reasoning**. Would add `swebench_bugfix_minimal` PromptVariant with clause: *"Make the smallest change that fixes the issue. Avoid adding new structures unless the bug fundamentally requires them. Once you have a coherent patch, stop — do not iterate."* Run on the same `probe_n10.json` set; 3-way compare (baseline / counterexample / minimal). ~70 min wall.

Hypotheses (per reviewer):
- Preserves baseline's matplotlib + sympy gold-matches (counterexample broke them)
- May improve sphinx/xarray-type precision
- Won't help pytest (true structural — needs new method)

### C. Docker confirmation for SWE-bench harness scoring
`pip install swebench` is done; harness wrapper at `benchmarks/swebench/harness.py` is wired but defers Docker calls. Confirm Docker Desktop is up + ~10GB free + acceptable RAM headroom (model ~30GB + container layer 6-8GB on a 64GB box is tight but viable). Then run harness scoring on the n=75 predictions for FAIL_TO_PASS / PASS_TO_PASS — that's the definitive number.

### D. Smaller cleanup items (queued)
- Retire v1.3 directive reprompt code in `cli.py` (~15 min)
- `min_added_lines` as per-requirement predicate kind in `src/luxe/spec.py`
- `ast_query` and `manual` predicate full integrations (currently stubbed)
- Tune Mode B thresholds based on broader bench data (currently 10 tools / 4000 tokens / step 5)
- Bring `benchmarks/swebench/run.py` ETA format into BFCL standard (group + global counts) — cosmetic; the runner is functional

---

## Memory entries (read first)

External benchmark program — current focus:
- `project_swebench_smoke_2026_05_04.md` — **n=10 A/B + refined a/b1/b2/b3/c/d/e taxonomy** (most recent, primary reference for n=75 analysis)
- `project_bfcl_pre_specdd_baseline.md` — 76.29% combined, parallel cliff diagnosed
- `project_external_benchmark_program.md` — overall SWE-bench n=75 + BFCL v3 plan

Bench-substrate / failure-mode work:
- `project_doc_config_three_modes.md` — A/B/C decomposition of doc-config variance
- `project_v1_4_1_mode_b_validation.md` — 10/10 PASS validation
- `project_v1_4_validation.md` — original v1.4.0 3-rep result (9.67/10 effective)
- `project_compound_goal_audit.md` — SpecDD premise empirically thin
- `project_loose_grader_audit.md` — 5/10 graders looser than goal text (closed at v1.4 spec layer)

Diagnostic / process:
- `feedback_deliberation_amplifiers.md` — **NEW** don't extrapolate "think more" prompt clauses from single-instance probes; A/B before shipping
- `feedback_benchmark_progress.md` — **NEW** all bench runners need group + global elapsed/remaining/ETA
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

**Post-v1.4.1 (this session, swebench-side; not part of luxe's release versioning)**:
- BFCL Python subset complete: 76.29% (parallel cliff diagnosed)
- swebench prompt overlay: anti-reproducer + linear protocol
- n=10 A/B established: counterexample heuristic anti-correlated with already-correct trajectories; reverted
- inspector v2 with gold-proximity tier; resume capability for n=75-class runs
- n=75 baseline launched against `subsets/v1_baseline_n75.json`

**What's queued**:
- v1.5.0 — SpecDD Lever 2 (`.sdd` parser, spec_resolver, tool-side Forbids, resume path, prompt injection)
- v1.6.0 — SpecDD Lever 3 (fixture-side `.sdd` contracts, methodology A/B)
- Docker harness scoring on n=75 predictions for definitive FAIL_TO_PASS numbers

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
| `benchmarks/swebench/smoke_inspect.py` | **inspector v2** — mechanical + gold-proximity tier (`--gold-source`); 5 signals, line-based hunk proximity, hunk coverage |
| `benchmarks/swebench/run.py` | preds-only runner; **idempotent resume** (per-instance summaries carry `model_patch`); same command picks up after a crash |
| `benchmarks/swebench/subsets/v1_baseline_n75.json` | 75 stratified instances, 12 repos — the pre-SpecDD anchor target |
| `benchmarks/swebench/subsets/probe_n10.json` | n=10 A/B subset (4 easy + 6 medium across 10 distinct repos) |
| `benchmarks/swebench/subsets/probe_12907.json` | single-instance probe used for the original hypothesis-stall trace |
| `benchmarks/bfcl/` | BFCL v3 adapter (raw + agent modes, schema converter, grader); resume + ETA in `run.py` |
| `configs/single_64gb.yaml` | maintain_suite config — `Qwen3.6-35B-A3B-6bit`, `manage_strict_only` overlay |
| `configs/single_64gb_swebench.yaml` | **swebench config** — `swebench_strict_only` overlay (anti-reproducer prompt); the n=75 default |
| `configs/single_64gb_swebench_counterexample.yaml` | A/B variant with falsification clause; **negative control, not promoted** |
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
09e97a2  benchmarks/swebench/run: idempotent resume for n=75-class runs
238ecd0  lessons.md: SWE-bench n=10 A/B postmortem
a4af109  swebench/smoke_inspect: gold-proximity tier (line-based, with coverage)
f8b3490  swebench prompt: counterexample-heuristic A/B variant + n=10 subset
8aaac6d  swebench prompt: revert rule #5 + add 12907 single-instance probe subset
19424c9  swebench prompt: add rule #5 — no analysis-only final reports (REVERTED)
fb9f985  docs: RESUME.md — complete BFCL baseline + swebench overlay shipped
60acdc7  swebench: anti-reproducer prompt overlay + mechanical smoke inspector
d5003bb  benchmarks/bfcl/run: idempotent resume + group/global ETA progress
cebc2ba  docs: RESUME.md — restructure as lean current-state document (629 → 272 lines)
10b352b  docs: RESUME.md — autonomous slot summary (Mode B + BFCL + SWE-bench smoke)
86c3b4e  benchmarks/bfcl/aggregate: post-hoc summary builder from per-problem JSONs
dc4c5df  lessons.md: v1.4.1 Mode B/A combo validation — 10/10 PASS on nothing-doc-config × 10
1d5b006  v1.4.1: citation-linter bare-filename fallback + Mode B write-pressure + regrade lint re-run
707bab8  v1.4.0: SpecDD Lever 1 — programmatic Definition of Done; first 10/10 bench
```

---

## When in doubt

`git log --oneline -20` tells the trajectory. `lessons.md` has postmortems of every failure pattern. The user prefers terse, action-oriented responses — don't summarize what they can read; tell them the next step.

The user is comfortable with auto mode but draws hard lines on destructive shared-system actions (oMLX config, sudo, force-push, deletes outside their workspace). When in doubt, write the change but ask before applying. Do NOT push to remote unless explicitly asked.
