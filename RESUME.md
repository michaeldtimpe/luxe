# luxe — session resume document

## Current state — 2026-05-06 morning (v1.5.0-rc-2; v2 rerun pending)

**Working tree**: clean. **619 tests passing** (+8 vs prior 611 from new v1.5-pressure leakage parametrized cases). 3 commits ahead of `origin/main`:

```
e062bab swebench/adapter: extend Forbids for v1.5 paired-mechanism escape paths
dcfa772 swebench/adapter: disable commit.gpgsign for bench runs
6c21956 swebench: bind LUXE_WRITE_PRESSURE to inject_sdd (paired mechanism)
```

**SpecDD Lever 2 — code SHIPPED, tag DEFERRED** pending a clean paired-mechanism rerun. The first paired-mechanism n=75 (v1) succeeded on every floor *except* `new_file_in_diff = 0` (8 instances leaked). Forbids tightening (commit `e062bab`) covers all 8 observed escape paths. Plan calls for one more n=75 rerun (v2) to confirm the floor holds, then tag.

**Architectural reframe (unchanged)**: `.sdd` is a coupled control architecture.
- **`.sdd`** (negative constraint, three layers: prompt → tool → linter) restricts *where* the model can write.
- **`LUXE_WRITE_PRESSURE=1`** (positive actuation, mid-loop intervention from v1.4.1) ensures the model *does* write.
- They ship as a pair. v1 confirmed: empty_patch −12pp absolute, strong + plausible +2.

**SWE-bench n=75 results — paired-mechanism rerun v1 (2026-05-06 dawn)**

| Metric | Pre-Lever-2 baseline | Post-Lever-2 (no pressure) | **v1 paired (actual)** | Ship floor |
|---|---|---|---|---|
| strong (gold-match) | 12 | 13 | **16** ✅ +4 | ≥12 |
| strong + plausible | 30 | 32 | **32** ✅ | ≥30 |
| empty_patch | 26 | 30 | **14** ✅ −12 | ≤28 |
| **new_file_in_diff** | 4 | 0 | **8** ❌ regression | =0 |
| any non-empty patch | 49 | 45 | 61 | — |

**Headline:** `empty_patch 26 → 14` (paired mechanism beat the prediction range of 24–27). xarray-2905 (the n=10 paired-mechanism bellwether) produced a 14-line diff after previously writing the fix in prose without committing. Of two named-regression instances tracked in the original RESUME.md plan: sphinx-10435 still empty, sympy-13091 rescued (92-line diff).

**The new_file_in_diff regression** is closeable. v1 escape paths cluster into 3 patterns:
- `verify_fix.py` variants (4 instances; literal at root, `repo/verify_fix.py`, `xarray/tests/test_fix_verify.py`)
- `tmp_*.py` (1 instance, 2 paths: `tmp_test.py`, `tmp_install.py`)
- novel `test_*_<descriptor>.py` shapes (3 instances: `test_verify.py`, `test_refit_time.py`, `sympy/test_verify.py`, `lib/matplotlib/test_verify.py`)

Commit `e062bab` adds 12 globs covering all 8 + prophylactic adjacents. 8 new parametrized tests assert each escape path is now blocked; expanded legitimate-paths guard confirms the broad globs anchor on suffix not substring.

**Two prerequisite fixes discovered during v1**:
- `dcfa772` — disable `commit.gpgsign` env override for bench runs. Without this, every instance failed with `Load key: incorrect passphrase` (luxe maintain's pr.py git commit blocks on the user's interactive SSH-signing config). Documented in `feedback_offer_long_running_commands.md` adjacent.
- `6c21956` — bind `LUXE_WRITE_PRESSURE=1` to `inject_sdd=True` per the original plan's Step 2 ($described in next section).

**See `~/.claude/plans/humble-prancing-patterson.md`** for the full v2 plan + failure-mode analysis (categorized empty_patch, wrong_target, wrong_location patterns + prioritized v1.6 backlog).

---

## Earlier state — 2026-05-04 night

**Working tree**: clean as of last commit; Lever 2 work in progress.

**SWE-bench n=75 pre-SpecDD anchor — DONE** (`acceptance/swebench/pre_specdd_v141_n75/rep_1/`):
- 7h 34m wall (15:47 → 23:21 on 2026-05-04). 49/75 non-empty patches; mechanical 45/75 (60%).
- **Strong (gold-match)**: 12/75 = **16%**. **Strong + plausible**: 30/75 = **40%**. **Manual high-confidence (post Step-2 review)**: **24/75 = 32%** — the durable pre-SpecDD anchor.
- Lands in RESUME.md's previously-defined 30-45% branch → **SpecDD Lever 2 is the next move**. Decision made.
- **Empty-patch (26/75 = 35%)** is the dominant failure mode at n=75 scale; n=10 had zero. Anti-reproducer prompt's locate→read→edit→verify protocol fails to even produce a candidate diff on a third of stratified instances.
- 4/75 created `test_fix.py` despite anti-reproducer rule — prompt is **leaky**; needs tool-side enforcement (Lever 2's tool-side Forbids is the right shape).
- See `project_swebench_n75_baseline.md` and lessons.md `[2026-05-04] SWE-bench n=75 pre-SpecDD anchor` for full breakdown.

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

## ⚡ Resume here — v1.5.0 v2 rerun + ship decision (overnight)

Steps 1-3 of the original release path are **done** (commits `6c21956`, `dcfa772`, `e062bab` on this branch). The remaining work is the v2 n=75 rerun confirming the Forbids tightening closes `new_file_in_diff` without regressing the other floors, then the tag.

Total remaining wall: ~5h n=75 + 30-45m harness scoring + tag.

Full plan + failure-mode analysis: `~/.claude/plans/humble-prancing-patterson.md`.

### Step 1 (was Step 4) — n=75 v2 rerun with tightened Forbids (~5h)

```bash
cd ~/Downloads/luxe
LUXE_LOG_TOOL_CALLS=1 OMLX_API_KEY=omlx-sdb25582k3mq8pf9 nohup \
  .venv/bin/python -m benchmarks.swebench.run \
    --subset benchmarks/swebench/subsets/v1_baseline_n75.json \
    --output acceptance/swebench/post_specdd_v15_pressure_v2_n75/rep_1/ \
    > /tmp/n75_pressure_v2.log 2>&1 &
```

NEW output dir (`...v2_n75`) so v1's data stays as the reference. The
adapter binds `LUXE_WRITE_PRESSURE=1` and disables `commit.gpgsign`
automatically (commits `6c21956`, `dcfa772`); no shell env munging
needed beyond `OMLX_API_KEY`.

**Restart oMLX before launching** (`brew services restart omlx`) to
clear any pinned models from earlier work — round 0 deluxe probes had
this issue and corrupted the first attempt.

### Step 2 — Compare v2 vs prior runs

```bash
# v2 vs pre-Lever-2 baseline (the long-arc claim)
.venv/bin/python -m benchmarks.swebench.compare_runs \
    --pre  acceptance/swebench/pre_specdd_v141_n75/rep_1/predictions.json \
    --post acceptance/swebench/post_specdd_v15_pressure_v2_n75/rep_1/predictions.json \
    --gold-source benchmarks/swebench/subsets/raw/verified.jsonl

# v2 vs v1 (isolates the Forbids-tightening effect)
.venv/bin/python -m benchmarks.swebench.compare_runs \
    --pre  acceptance/swebench/post_specdd_v15_pressure_n75/rep_1/predictions.json \
    --post acceptance/swebench/post_specdd_v15_pressure_v2_n75/rep_1/predictions.json \
    --gold-source benchmarks/swebench/subsets/raw/verified.jsonl

# Inspector — the hard new_file_in_diff blocker
.venv/bin/python -m benchmarks.swebench.smoke_inspect \
    --predictions acceptance/swebench/post_specdd_v15_pressure_v2_n75/rep_1/predictions.json \
    --gold-source benchmarks/swebench/subsets/raw/verified.jsonl \
    | grep -E "^  (strong|plausible|empty_patch|new_file_in_diff|wrong_location|wrong_target)" \
    | awk '{print $1}' | sort | uniq -c
```

### Step 3 — Ship-floor check (HARD BLOCKER, all must hold)

| Metric | Floor | v1 actual | v2 target |
|---|---|---|---|
| strong | ≥12 | 16 | ≥14 |
| strong + plausible | ≥30 | 32 | ≥30 |
| empty_patch | ≤28 | 14 | ≤16 (allow ±2) |
| **new_file_in_diff** | **=0** | **8** | **=0** |

Acceptance gate:

1. Inspector reports zero `new_file_in_diff` entries.
2. jq cross-check on v2 predictions.json: list any `model_patch` containing `new file mode` lines — should agree with inspector at zero.
3. strong ≥14 AND strong+plausible ≥30 AND empty_patch ≤16.
4. Spot-check 3 random `strong` rows by reading the patch — guards against the "broad glob accidentally blocked legit edits" failure mode where strong drops sharply because the model couldn't write what it needed.

**Stop conditions:**
- Any of (1)-(4) fails → do **NOT** tag.
- If `new_file_in_diff > 0` on v2: do NOT add another broad glob. Inspect the new escape names; if they reveal a fundamentally novel pattern, that's the trigger to escalate to creation-only forbids (v1.6 backlog item). Adding another whack-a-mole pattern risks a v3 round of the same dance.
- If strong drops sharply (e.g., 16 → ≤12): a broad glob blocked legitimate edits. Inspect with `_glob_matches` against the rejected paths; tighten the offending glob.

### Step 4 — Docker harness scoring (~30-45m)

Run the wrapper at `benchmarks/swebench/harness.py` against `acceptance/swebench/post_specdd_v15_pressure_v2_n75/rep_1/predictions.json`. Confirm Docker Desktop is up + ~10GB free + RAM headroom. Output to `acceptance/swebench/post_specdd_v15_pressure_v2_n75/harness/`. Numbers go into the v1.5.0 release commit body.

### Step 5 — Tag v1.5.0

Tag message records v2 absolute floors AND delta vs v1 (so the rerun's signal is preserved alongside the original v1 numbers):

```bash
git tag -a v1.5.0 -m "$(cat <<'EOF'
v1.5.0: SpecDD Lever 2 — coupled constraint + actuation system

.sdd contracts (negative constraint, three enforcement layers:
prompt + tool + linter) bound to LUXE_WRITE_PRESSURE in the
SWE-bench harness (positive actuation). Ships together because
constrained execution requires enforced actuation.

n=75 paired-mechanism rerun (v2 — Forbids tightened):
  strong:                <v2>  (v1: 16 → v2: <delta>)
  strong + plausible:    <v2>  (v1: 32 → v2: <delta>)
  empty_patch:           <v2>  (v1: 14 → v2: <delta>; baseline 26)
  new_file_in_diff:      <v2>  (v1: 8 → v2: 0;  baseline 4)
  any non-empty patch:   <v2>
  FAIL_TO_PASS (Docker harness): <pre> → <post>

vs pre-Lever-2 baseline (acceptance/swebench/pre_specdd_v141_n75/rep_1/):
  empty_patch:           -<X>pp  (paired mechanism's headline win)
  new_file_in_diff:      0       (full class elimination, sustained)

Internal dogfood: src/luxe/luxe.sdd (Forbids retired modes),
agents/agents.sdd, tools/tools.sdd, maintain_suite.sdd, root
CLAUDE.md.
EOF
)"
```

### Step 6 — Documentation

Move the v1.5.0 sections of RESUME.md to "Earlier state". New "Resume here" points at v1.6 priorities — early-bail intervention is #1 (addresses 10 of 14 empty_patch in v1 paired-mechanism data; see plan file's failure analysis).

`lessons.md` already has the v1 paired-mechanism entry (added in this session); add a closing entry with the v2 ship numbers + the Forbids tightening lesson.

---

## Explicit non-goals this session

- **Lever 3** — held until actuation behavior is stable. Lever 3 needs clean separation of constraint vs reasoning failures; the Mode B class confounds that boundary until WRITE_PRESSURE is bound in.
- **Per-instance variance probe across all regressions** — limited to the 2 the user named (sphinx-10435, sympy-13091). Lower-N regressions (pylint-4970, sphinx-10449) get re-evaluated post-rerun; if they remain regressions after WRITE_PRESSURE, escalate then.
- **Tagging v1.5.0 with current data** — would lock in a known regression with the fix on the shelf.

---

## Background tasks (queued, non-blocking)

These do not block v1.5.0 tag; revisit after the overnight rerun lands.

- Retire v1.3 directive reprompt code in `cli.py` (~15 min) — superseded by SpecDD Lever 1 spec validator
- `min_added_lines` as per-requirement predicate kind in `src/luxe/spec.py`
- `ast_query` and `manual` predicate full integrations (currently stubbed)
- Tune Mode B thresholds based on broader bench data (currently 10 tools / 4000 tokens / step 5) — extra signal incoming from the WRITE_PRESSURE n=75 rerun
- Bring `benchmarks/swebench/run.py` ETA format into BFCL standard (group + global counts) — cosmetic
- Per-fixture `.sdd` contracts on the maintain_suite (Lever 3 prep) — depends on `trace:` field audit
- **Minimality-bias A/B** (orthogonal experiment proposed pre-Lever-2): adds `swebench_bugfix_minimal` PromptVariant with *"Make the smallest change that fixes the issue. Once you have a coherent patch, stop — do not iterate."* Run on `probe_n10.json`; 3-way compare. ~70 min wall. **Re-evaluate after Lever 2 + pressure ships** — may not be needed if `empty_patch` already returns to baseline.

---

## Memory entries (read first)

External benchmark program — current focus:
- `project_v15_specdd_lever2_shipped.md` — **PRIMARY** Lever 2 ship state + n=75 result table + paired-mechanism reframe + WRITE_PRESSURE recommendation
- `project_swebench_n75_baseline.md` — pre-Lever-2 anchor: 32% high-confidence; empty-patch 26/75 dominant
- `project_swebench_smoke_2026_05_04.md` — n=10 A/B + a/b1/b2/b3/c/d/e taxonomy (n=10 was 50pp optimistic; superseded by n=75)
- `project_bfcl_pre_specdd_baseline.md` — 76.29% combined, parallel cliff diagnosed
- `project_external_benchmark_program.md` — overall SWE-bench n=75 + BFCL v3 plan

Bench-substrate / failure-mode work:
- `project_doc_config_three_modes.md` — A/B/C decomposition of doc-config variance
- `project_v1_4_1_mode_b_validation.md` — 10/10 PASS validation
- `project_v1_4_validation.md` — original v1.4.0 3-rep result (9.67/10 effective)
- `project_compound_goal_audit.md` — SpecDD premise empirically thin
- `project_loose_grader_audit.md` — 5/10 graders looser than goal text (closed at v1.4 spec layer)

Diagnostic / process:
- `feedback_exception_hierarchy_catch_order.md` — **NEW** when except clauses cover an inheritance hierarchy, derived class first
- `feedback_fixture_prep_dirty_tree.md` — **NEW** synthetic-`.sdd`-class fixture prep needs `--allow-dirty` in the agent invocation
- `feedback_deliberation_amplifiers.md` — don't extrapolate "think more" prompt clauses from single-instance probes; A/B before shipping
- `feedback_benchmark_progress.md` — all bench runners need group + global elapsed/remaining/ETA
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

**v1.5.0-rc-2 (this session, 2026-05-06)**:
- SpecDD Lever 2 code complete (carried from prior session)
- **`6c21956`** — bind `LUXE_WRITE_PRESSURE=1` to `inject_sdd=True` in swebench adapter (paired-mechanism wiring)
- **`dcfa772`** — disable `commit.gpgsign` env override in `invoke_luxe_maintain` (bench-only; required because user's SSH-signing config blocks unattended commits)
- **`e062bab`** — extend `SWEBENCH_SDD_BODY` Forbids with 12 new globs covering all 8 v1 paired-mechanism escape paths + prophylactic adjacents
- 619 tests passing (+12 new vs the 607 baseline that started this work: 4 paired-mechanism env tests in `6c21956`, 8 v1.5-pressure leakage parametrized cases in `e062bab`)
- v1 paired-mechanism n=75 result analyzed; 8 escape paths discovered + closed
- **Pending**: v2 n=75 rerun confirming `new_file_in_diff = 0` floor holds; harness scoring; tag

**What's queued**:
- **v1.5.0** (final tag, next overnight) — v2 rerun confirming Forbids tightening landed cleanly. See `~/.claude/plans/humble-prancing-patterson.md` for the full plan + failure-mode analysis.
- v1.6.0 — early-bail intervention #1 priority (addresses 10 of 14 v1 empty_patch). Then creation-only forbids (replaces v1.5's broad-glob workaround with proper semantics). Then retrieval-side multi-file edits. Then in-loop test-execution feedback. Lever 3 still on backlog but de-prioritized vs the empty_patch class.

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
| `src/luxe/agents/single.py` | mono runner — agentic loop end-to-end; **`_build_sdd_block` injects Repository contracts (v1.5)** |
| `src/luxe/agents/loop.py` | shared loop; **Mode B write-pressure injection (v1.4.1)** |
| `src/luxe/agents/prompts.py` | prompt registry + TaskOverlay; doc/manage strict variants |
| `src/luxe/citations.py` | diff-aware citation linter; bare-filename fallback (v1.4.1); **`spec_violation`/`spec_orphan` (v1.5)** |
| `src/luxe/sdd.py` | **`.sdd` parser (v1.5)** — six canonical sections, tolerant header normalization |
| `src/luxe/spec_resolver.py` | **chain assembly + glob matching (v1.5)** — `find_all_sdd`, `resolve_chain`, `format_sdd_block` |
| `src/luxe/spec.py` | SpecDD Lever 1 data model (`Requirement`, `Spec`, YAML round-trip) |
| `src/luxe/spec_validator.py` | SpecDD Lever 1 predicate evaluator + reprompt-text helper |
| `src/luxe/tools/base.py` | `dispatch_tool` (tool exceptions captured as retry-able errors) |
| `src/luxe/tools/fs.py` | write-time honesty guards; **`_check_spec_forbids` pre-write enforcement (v1.5)** |
| `src/luxe/luxe.sdd` | **root invariants (v1.5 dogfood)** — Forbids retired `src/swarm/**` etc. |
| `src/luxe/agents/agents.sdd` | **(v1.5 dogfood)** — prompt registry as single source of truth |
| `src/luxe/tools/tools.sdd` | **(v1.5 dogfood)** — honesty guards before Forbids; cve_lookup gating |
| `benchmarks/maintain_suite/maintain_suite.sdd` | **(v1.5 dogfood)** — bench rules |
| `CLAUDE.md` | **(v1.5)** — auto-loaded by Claude Code; points at the `.sdd` chain |
| `src/luxe/backend.py` | `chat()` accepts `repeat_penalty`; `unload_model()`, `loaded_models()` |
| `src/luxe/cli.py` | `luxe maintain` (mono only); `--spec-yaml` for SpecDD reprompt gate |
| `src/luxe/config.py` | `RoleConfig` w/ system/task prompt + overlay ids + repeat_penalty |
| `benchmarks/maintain_suite/run.py` | bench harness; `Variant` carries prompt + overlay overrides |
| `benchmarks/maintain_suite/grade.py` | grading + strict gates + multi-variant `v1_release_gate` |
| `benchmarks/maintain_suite/fixtures.yaml` | the 10 v1 fixtures (each w/ `requirements:` block) |
| `benchmarks/swebench/` | SWE-bench Verified adapter (preds-only + Docker harness wrapper + compare) |
| `benchmarks/swebench/smoke_inspect.py` | **inspector v2** — mechanical + gold-proximity tier (`--gold-source`); 5 signals, line-based hunk proximity, hunk coverage |
| `benchmarks/swebench/run.py` | preds-only runner; **idempotent resume** (per-instance summaries carry `model_patch`); same command picks up after a crash; **`--no-inject-sdd` + `--no-write-pressure` flags (v1.5) for ablation** |
| `benchmarks/swebench/adapter.py` | **synthetic `.sdd` injection (v1.5)** — `write_swebench_sdd` / `remove_swebench_sdd`; passes `--allow-dirty`; **paired-mechanism env wiring + commit.gpgsign override (v1.5.0-rc-2)**; SWEBENCH_SDD_BODY Forbids has two empirical layers (n=75 baseline + v1 paired-mechanism rerun escapes) |
| `benchmarks/swebench/compare_runs.py` | **(v1.5)** — pre/post predictions delta report (per-instance + class-level + summary) |
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
| `~/.claude/plans/humble-prancing-patterson.md` | **v1.5.0 ship plan + failure-mode analysis (this session)** — Forbids tightening + v2 rerun + v1.6 backlog ranking |

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

Run `git log --oneline -20` for fresh state. Highlights from this session:

```
a7de2ff  SpecDD Lever 2: n=75 result + Mode B follow-up recommendation
83a40ba  RESUME.md: SpecDD Lever 2 (v1.5.0) shipped + n=10 probe in flight
6d0b68d  Lever 2 follow-ups: compare_runs script + lessons entry + memory updates
cfcc5e7  swebench/adapter: pass --allow-dirty so synthetic .sdd doesn't block start
244ee45  SpecDD Lever 2: synthetic .sdd injection for SWE-bench fixtures
0a8ab2b  SpecDD Lever 2: citation linter spec_violation/spec_orphan signals
e50ab99  SpecDD Lever 2: dogfood internal .sdd files + CLAUDE.md
e1d01b0  SpecDD Lever 2: task prompt embeds .sdd Repository contracts block
a8862ea  SpecDD Lever 2: tool-side Forbids in fs.py write_file/edit_file
81ede53  SpecDD Lever 2: spec_resolver — chain assembly + glob matching
b764517  SpecDD Lever 2: .sdd parser + n=75 pre-SpecDD anchor docs
1d5b006  v1.4.1: citation-linter bare-filename fallback + Mode B write-pressure + regrade lint re-run
707bab8  v1.4.0: SpecDD Lever 1 — programmatic Definition of Done; first 10/10 bench
```

---

## When in doubt

`git log --oneline -20` tells the trajectory. `lessons.md` has postmortems of every failure pattern. The user prefers terse, action-oriented responses — don't summarize what they can read; tell them the next step.

The user is comfortable with auto mode but draws hard lines on destructive shared-system actions (oMLX config, sudo, force-push, deletes outside their workspace). When in doubt, write the change but ask before applying. Do NOT push to remote unless explicitly asked.
