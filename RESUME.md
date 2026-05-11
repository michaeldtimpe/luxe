# luxe — session resume document

## Current state — 2026-05-11 (post-m5max_moe substrate hardening; v1 maintain_suite at 30/30 modulo variance)

**Working tree**: clean. **648 tests collected, 643 passing** (5 new regression tests landed; 5 pre-existing `test_bfcl_adapter.py` failures from missing optional `bfcl_eval` dep persist — unrelated, predates the session). **7 commits pushed to `origin/main`** (5cc3c87 → 1d848ae).

**M5 Max MoE bake-off complete** (`acceptance/m5max_moe/`, 2026-05-10). The full run started at 17/30 (81/150, GLM 0/10) and landed at **30/30 (120/150, all 3 variants pass v1 gate) modulo a single transient `embedded null byte` ValueError at the commit step** (lpe-rope-calc-implement-strict-flag on GLM, scored 4/5 on the recheck). The final official bench shows 29/30; the variance recheck confirms the true rollup is 30/30. See `lessons.md` 2026-05-10 m5max_moe entry for the full postmortem.

**Six fix vectors landed durably:**

1. **`tools/base.py` `dispatch_tool` strips whitespace** in the tool name. GLM-4.5-Air-4bit emits `"read_file\n"` / `"bash\n\n"` etc.; without the strip, every dispatch missed and the model bailed (0/10 baseline → 7/10 from this fix alone).
2. **`agents/loop.py` normalizes `tc.name` at the loop boundary** too. The dispatcher fix wasn't enough — `_WRITE_TOOLS`, `_DEDUP_EXEMPT_TOOLS`, schema validation, and dedup keying all read the raw name. With whitespace, `writes_seen` never incremented for GLM, so WRITE_PRESSURE fired *after* diffs landed and `_POST_WRITE_IDLE_MAX` never armed.
3. **`agents/loop.py` `_WRITE_PRESSURE_MAX_TOOLS_BEFORE_FIRE = 15`** OR-branch on the existing completion-tokens gate. The 4000-token threshold was calibrated on qwen3.6-35B's prose-heavy failure; qwen3-coder-next averages 1855 completion tokens per fixture — the gate was unreachable. 10 of 11 firings in the verifying re-bench hit the tool-ceiling branch.
4. **`agents/loop.py` `_POST_WRITE_IDLE_MAX = 3`** — once any write succeeds, 3 consecutive 0-byte non-write calls trigger a clean exit (not `aborted`). Catches the post-success verification drift the dup-detector eventually catches but marks as bailout. Fired in 13/30 runs.
5. **`benchmarks/maintain_suite/run.py`** sets `LUXE_WRITE_PRESSURE=1` via `env.setdefault` so the read-loop interrupt is the bench default (ablations can still override).
6. **SpecDD Lever 2 extended to maintain_suite** — `Fixture.forbids_create: list[str]` + `_inject_forbids_create_sdd` writes `<repo>.sdd` at the cloned-repo root + appends to `.git/info/exclude` so the synthetic contract doesn't pollute fixture diffs. Three opted-in fixtures (lpe-rope-calc-implement-strict-flag, the-game-implement-shuffle-shortcut, neon-rain-implement-reset-shortcut) get cross-product coverage of test-name shapes (prefix/suffix × separator × root/subpath). Verified end-to-end: `.sdd` lands, exclude registers, fixture diffs stay clean.

**Per-machine env state** (not version-controlled, documented inline in RESUME.md §oMLX configuration and §maintain_suite bench-host prereqs):
- `~/.omlx/settings.json` `sampling.max_context_window`: 32k → 48k (qwen3-coder-next was hitting 33k+ per turn on `nothing-ever-happens-document-config`).
- `brew install node` (npm 11.12) — fixture `neon-rain-implement-reset-shortcut` shells out to `npm test`.

**The variance class is open for v1.7.** GLM at temp=0 still shows ~10% per-fixture variance across replicates (orphan scaffold creation, transient `embedded null byte` from the commit step). Existing scoring gates (vacuous_test, orphan_file) catch these; `Forbids creating` cuts the rate further via the recovery-gradient error wording. Lever 3 positive constraints ("you must edit X") are the long-term answer per the v1.7 backlog below — not gating any v1 bench.

**No release tag**: the m5max_moe work is substrate hardening on top of the already-tagged v1.6.0. No new version bump is implied.

---

## ⚡ Resume here — v1.7 priorities (unchanged)

The four remaining v1.6-era loose ends below still apply. The m5max_moe substrate work landed durably and clears the path for v1.7 work; the "open question" from the m5max_moe lessons.md entry — *do the threshold-asymmetry findings generalise to SWE-bench?* — is now the natural first probe before the early-bail intervention design lands.

### v1.7 priorities (in order of expected impact)

1. **Early-bail intervention** — addresses ≥10 of the 18 v3 paired-mechanism `empty_patch` cases (the `agent_bailed` class). Interception strategy: detect the bail signature in the loop (consecutive low-output steps + no write-tool calls) and inject a directive turn rather than letting the loop trip its stuck detector. Prerequisite: `LUXE_LOG_TOOL_CALLS=1` traces of the 18 v3 empties to confirm class composition. With m5max_moe's `_POST_WRITE_IDLE_MAX` and tuned WRITE_PRESSURE thresholds now in place, the bail-class composition may already shift before any v1.7 work lands — worth re-checking traces before designing.
2. **b2 multi-site retrieval** — extend the spec-validator predicate kinds so SpecDD Lever 1 can demand citations from N sites within a single fixture. Closes the loose-grader gap surfaced in `project_loose_grader_audit.md`.
3. **In-loop test execution feedback** — pipe `pytest` results from the previous step back into the model's next prompt. Likely gates the second strong-tier rebound (Phase B nearest-anchoring tightening, slated to fire here).
4. **Mode B threshold tuning** — broader bench data is incoming from v3 + Phase B; revisit the 10 tools / 4000 tokens / step 5 thresholds against the v3 traces. The m5max_moe tune (tool-ceiling OR-branch) already addressed the most acute miscalibration on tool-call-heavy models; more granular per-model defaults are next.
5. **Lever 3** — held until empty_patch class is fully addressed; Lever 3 needs clean separation of constraint vs reasoning failures, and the empty_patch class confounds that boundary today.

### v1.6-era loose ends (status as of 2026-05-11)

1. **BFCL v3 post-SpecDD raw-mode** — was kicked off 2026-05-10 17:30 local. Check `acceptance/bfcl/post_specdd_v16/rep_1/`. If totals deviate >2pp from 76.29%, an infra regression leaked. If they don't, file the result and move on.
2. **(Open question)** BFCL agent-mode post-SpecDD run — would actually exercise SpecDD, but no pre-SpecDD agent-mode anchor exists. Worth doing as a one-shot v1.6 datapoint to inform v1.7 BFCL strategy.
3. **(Optional follow-up)** Re-aggregate the v3 harness summary into a tracked `harness_summary.json` once the rebuilt `harness.py:collect_results` fix is exercised on a fresh run. Current summary was written via the fixed collector against the existing `logs/run_evaluation/luxe_v16_n75/` dir.
4. **sphinx-doc__sphinx-10466 strong→unresolved** is the lone strong tier instance the harness rejected. Worth a glance for v1.7 prep but not a v1.6 blocker.

---

## Earlier state — 2026-05-10 (v1.6.0 SHIPPED)

**Working tree**: clean post-tag. **643 tests passing**. **v1.6.0 tagged** with the v3 ship-floor + Docker harness numbers. BFCL v3 post-SpecDD raw-mode comparison run kicked off (~3.5h wall, in-progress as of tag time).

**Ship-floor result (Phase D Step 3, all gates green)**:

| Signal | Floor | v3 actual |
|---|---|---|
| new_file_in_diff | =0 | **0** ✅ (jq cross-check confirms zero `new file mode`) |
| strong | ≥14 | **16** ✅ |
| strong + plausible | ≥30 | **36** ✅ |
| empty_patch | ≤18 | **18** ✅ |
| wrong_target | ≤20 (soft) | **17** ✅ (no Phase B anchoring spike) |

**Docker harness (Phase D Step 4, n=75)**: **36/75 = 48.0% resolved** in 34m43s, 0 errors. Tier × resolved: strong 15/16 (94%), plausible 10/20 (50%), wrong_target 8/17 (47%), wrong_location 3/4 (75%), empty_patch 0/18 (0%). The strong inspector tier is a near-perfect predictor of harness-resolution; 11 wrong_target/wrong_location resolves are alternative-solution credit (model fixed a different file/locus than gold, tests pass anyway).

**v3 vs pre-Lever-2 baseline (long-arc claim)**: strong 12→16 (+33%); empty_patch 26→18 (−10.7pp); new_file_in_diff 4→0 (full class elimination); any non-empty 45→57 (+27%). Paired-mechanism win sustained AND class eliminated.

**v3 vs v2 (creation-only delta)**: new_file 2→0 (the architectural target). xarray-3305 + sphinx-10466 both empty/wrong_loc → strong (variance, not collateral, confirmed). sympy-12481 invent→plausible (gold file modified). matplotlib-24870 new_file→empty (1/2 v2-escape "constraint pressure → occasional abandonment", within budget).

**Architectural shift recap — operation-aware policy**: v1.5 encoded *"these filenames are suspicious"* (path-aware). v1.6 encodes *"creating verifier scaffolding is disallowed"* (operation-aware). `.sdd` gains a new section `Forbids creating` that fires only when a write would create a new file at the target path. The policy boundary now matches the behavioral distinction the system was missing: **repository participation** (legitimate edits to existing files) vs **benchmark gaming** (invented validation scaffolds).

| Section | Fires on edit? | Fires on create? |
|---|---|---|
| `Forbids` (existing) | ✅ | ✅ |
| `Forbids creating` (v1.6 new) | ❌ | ✅ |

`creating = not Path.is_file()` is computed in `_write_file` at the moment of the write. `_edit_file` always passes `creating=False` (existence enforced two lines later). Disk state naturally handles the multi-step trajectory case (create in step 1, edit in step 2) without synthetic planner state. Distinct error messages: `"forbidden ... do not write outside allowed paths"` for unconditional Forbids (reads as *wrong location*) vs `"forbidden-on-create ... Edit an existing file instead of creating a new one"` for create-only matches (reads as *wrong operation*; primes reroute, not bailout).

**Phase A static audit** (full SWE-bench Verified n=500, 2026-05-06): **CLEAN** — zero gold patches create a `test_*.py` file. The broad `**/test_*.py` create-ban ships as a stable adapter-wide policy, not subset-specific tuning.

**Phase C smoke** (n=14, `acceptance/swebench/v16_smoke_n14/rep_1/`, 2026-05-06):
- **new_file_in_diff = 0** across all 14 ✅ (HARD floor met)
- **sympy-12481 reroute (the architectural test case)**: was inventing `test_fix_check.py` in v2 → v1.6 produced a **strong gold-match** by editing `sympy/combinatorics/permutations.py` directly. The qualitative transition *invent scaffold → modify existing artifact* was empirically demonstrated.
- Both v2 strong-tier "regressions" (xarray-3305, sphinx-10466) rebounded to strong → confirms variance hypothesis (not glob collateral).
- v2-strong preservation 4/5 (matplotlib-13989 dropped to empty — within ±1 variance budget).
- matplotlib-24870 (other v2 escape) went empty rather than rerouting. 1/2 architectural test cases reroute cleanly; the other shows the user-predicted "constraint pressure → occasional abandonment". Mixed but net positive.

**SWEBENCH_SDD_BODY split**: only `repo_root/**` (synthetic prompt-context path) stays in `Forbids`. ALL scaffolding-name patterns moved to `Forbids creating`, including the v2-escape additions: `test_*.py`, `**/test_*.py`, `test_fix_*.py`, `**/test_fix_*.py`. Internal `.sdd` dogfood (`src/luxe/luxe.sdd` etc.) unchanged — `Forbids creating` is bench-specific in v1.6.

**See `~/.claude/plans/cozy-wiggling-conway.md`** for the full v1.6 plan, the audit gates, the ship-floor table, and the Phase B nearest-existing-test anchoring watch.

### v1.6 ship-cycle Phase D reference commands (kept for re-run)

### Step 1 — n=75 v3 rerun with creation-only forbids — DONE 2026-05-09

Reference command (kept for re-run):

```bash
brew services restart omlx && sleep 5 && \
cd ~/Downloads/luxe && \
LUXE_LOG_TOOL_CALLS=1 OMLX_API_KEY=omlx-sdb25582k3mq8pf9 nohup \
  .venv/bin/python -m benchmarks.swebench.run \
    --subset benchmarks/swebench/subsets/v1_baseline_n75.json \
    --output acceptance/swebench/post_specdd_v16_creation_only_n75/rep_1/ \
    > /tmp/n75_v16.log 2>&1 &
```

Adapter binds `LUXE_WRITE_PRESSURE=1` and disables `commit.gpgsign` automatically; no shell env munging needed beyond `OMLX_API_KEY`. Restart oMLX before any rerun to clear pinned models.

### Step 2 — Compare v3 vs prior runs

```bash
# v3 vs pre-Lever-2 baseline (the long-arc claim)
.venv/bin/python -m benchmarks.swebench.compare_runs \
    --pre  acceptance/swebench/pre_specdd_v141_n75/rep_1/predictions.json \
    --post acceptance/swebench/post_specdd_v16_creation_only_n75/rep_1/predictions.json \
    --gold-source benchmarks/swebench/subsets/raw/verified.jsonl

# v3 vs v2 (isolates the creation-only semantic shift)
.venv/bin/python -m benchmarks.swebench.compare_runs \
    --pre  acceptance/swebench/post_specdd_v15_pressure_v2_n75/rep_1/predictions.json \
    --post acceptance/swebench/post_specdd_v16_creation_only_n75/rep_1/predictions.json \
    --gold-source benchmarks/swebench/subsets/raw/verified.jsonl

# Inspector — verdict tally + new_file_in_diff escape audit
.venv/bin/python -m benchmarks.swebench.smoke_inspect \
    --predictions acceptance/swebench/post_specdd_v16_creation_only_n75/rep_1/predictions.json \
    --gold-source benchmarks/swebench/subsets/raw/verified.jsonl \
    | grep -E "^  (strong|plausible|empty_patch|new_file_in_diff|wrong_location|wrong_target)" \
    | awk '{print $1}' | sort | uniq -c
```

### Step 3 — Ship-floor check (HARD; all must hold)

The headline is not `new_file_in_diff = 0` in isolation — that alone could be achieved by suppressing all writes (which would push empty_patch up). The success signal is the *combination*: scaffolding creation blocked AND model didn't bail under the additional pressure AND model rerouted to *correct* edits, not *any* edits.

| Signal | Floor | v2 actual | v3 target |
|---|---|---|---|
| new_file_in_diff | =0 | 2 | =0 (HARD) |
| strong | ≥14 | 16 | ≥14 |
| strong + plausible | ≥30 | 35 | ≥30 |
| empty_patch | ≤18 | 17 | ≤18 (within +1 of v2) |
| wrong_target | ≤ v2 + 4 | 16 | ≤20 (soft watch — Phase B "nearest-existing-test anchoring") |

Acceptance gate:
1. Inspector reports zero `new_file_in_diff` entries.
2. jq cross-check on v3 predictions.json: list any `model_patch` containing `new file mode` lines — should agree with inspector at zero.
3. strong ≥14 AND strong+plausible ≥30 AND empty_patch ≤18.
4. wrong_target composition delta vs v2 — if it spikes by +5 or more, Phase B nearest-anchoring watch fired (model satisfied pressure by editing *some* existing test rather than the *correct* one). Inspect 3 random wrong_target rows that came from previously-empty v2 instances; if model_files cluster on `tests/...`, anchoring is real and tag should hold for v1.7 planning-prompt tuning.
5. Spot-check 3 random `strong` rows by reading the patch — guards against "broad glob accidentally blocked legit edits".

**Stop conditions:**
- Any of (1)-(3) fails → do **NOT** tag. Investigate what shape escaped.
- (4) fires (wrong_target +5 or more) → hold tag. Phase B postmortem before deciding ship vs v1.7-tune.
- empty_patch climbs above 22 → the new error message + create-only semantics aren't providing the recovery gradient; v1.6 needs a re-read.

### Step 4 — Docker harness scoring (~30-45m)

Run the wrapper at `benchmarks/swebench/harness.py` against `acceptance/swebench/post_specdd_v16_creation_only_n75/rep_1/predictions.json`. Confirm Docker Desktop is up + ~10GB free + RAM headroom. Output to `acceptance/swebench/post_specdd_v16_creation_only_n75/harness/`. Numbers go into the v1.6.0 release commit body.

### Step 5 — Tag v1.6.0

Tag message records v3 absolute floors AND delta vs v2 (creation-only effect) AND delta vs pre-Lever-2 baseline (long-arc claim):

```bash
git tag -a v1.6.0 -m "$(cat <<'EOF'
v1.6.0: SpecDD Lever 2 — creation-only forbids (operation-aware policy)

`.sdd` gains a `Forbids creating` section that fires only when a
write would create a new file. Splits two qualitatively different
operations the v1.5 contract conflated:
  - editing a pre-existing file (legitimate repository participation)
  - inventing a new file (benchmark gaming)

`creating = not Path.is_file()` is operationally observable,
deterministic, and stateful across turns automatically — disk state
handles multi-step trajectories without synthetic planner state.

Distinct error wording for the create-only class
("forbidden-on-create ... Edit an existing file instead of creating
a new one") gives the planner a recovery gradient — wrong operation
rather than wrong location.

Phase A static audit (full SWE-bench Verified n=500): zero gold
patches create a test_*.py file → broad **/test_*.py create-ban
ships as a stable adapter-wide policy.

n=75 v3 (creation-only forbids):
  strong:                <v3>  (v2: 16 → v3: <delta>)
  strong + plausible:    <v3>  (v2: 35 → v3: <delta>)
  empty_patch:           <v3>  (v2: 17 → v3: <delta>; baseline 26)
  new_file_in_diff:      <v3>  (v2: 2 → v3: 0;  baseline 4)
  wrong_target:          <v3>  (v2: 16 → v3: <delta>)
  any non-empty patch:   <v3>
  FAIL_TO_PASS (Docker harness): <pre> → <post>

vs pre-Lever-2 baseline (acceptance/swebench/pre_specdd_v141_n75/rep_1/):
  empty_patch:           -<X>pp  (paired-mechanism win, sustained)
  new_file_in_diff:      0       (full class elimination, durable)
  strong:                +<X>    (gold-match increase, durable)

The architectural shift: v1.5 encoded "these filenames are suspicious"
(path-aware folklore). v1.6 encodes "creating verifier scaffolding is
disallowed" (operation-aware policy). The policy boundary stops
conflating two distinct operations on the same target.
EOF
)"
```

---

## Earlier state — 2026-05-06 morning (v1.5.0-rc-2; v2 result captured, ceiling discovered)

**v2 n=75 rerun result** (`acceptance/swebench/post_specdd_v15_pressure_v2_n75/rep_1/`):

| Metric | Pre-Lever-2 baseline | Post-Lever-2 (no pressure) | v1 paired | **v2 paired** | Ship floor |
|---|---|---|---|---|---|
| strong (gold-match) | 12 | 13 | 16 | **16** | ≥12 |
| strong + plausible | 30 | 32 | 32 | **35** | ≥30 |
| empty_patch | 26 | 30 | 14 | **17** | ≤28 |
| **new_file_in_diff** | 4 | 0 | 8 | **2** | =0 |
| any non-empty patch | 49 | 45 | 61 | 56 | — |

**Headline (v2 vs baseline)**: empty_patch 26 → 17 (−35%); strong 12 → 16 (+33%); any-non-empty 45 → 56 (+24%). The paired-mechanism (`.sdd` constraint + WRITE_PRESSURE actuation) sustained its win.

**Headline (v2 vs v1)**: new_file_in_diff cratered 8 → 2 (−75%) under broad-glob tightening. 6 of 8 v1 escapes routed to legitimate buckets (1 strong, 3 plausible, 2 wrong_target).

**The blocker**: 2 escapes remained — `test_bool_contour.py` (matplotlib-24870) and `test_fix_check.py` (sympy-12481). Both shapes are indistinguishable from legitimate test files by name alone. No broad glob can safely cover them as edit-or-create bans. The v1.5 broad-glob approach hit an architectural ceiling that more patterns cannot resolve. Hence v1.6.

**Falsification check passed (2026-05-06)**: gold patches for the two strong-tier "regressions" (xarray-3305, sphinx-10466) and the xarray cluster (xarray-6938) do NOT match any v1.5 broad glob. Those regressions are temp=0 variance, not glob collateral. Smoke (Phase C) later confirmed: both rebounded to strong under v1.6.

---

## Earlier state — 2026-05-04 night (pre-SpecDD anchors)

**SWE-bench n=75 pre-SpecDD anchor — DONE** (`acceptance/swebench/pre_specdd_v141_n75/rep_1/`):
- 7h 34m wall (15:47 → 23:21 on 2026-05-04). 49/75 non-empty patches; mechanical 45/75 (60%).
- Strong (gold-match): 12/75 = 16%. Strong + plausible: 30/75 = 40%. Manual high-confidence (post Step-2 review): 24/75 = 32%.
- Empty-patch (26/75 = 35%) is the dominant failure mode at n=75 scale; n=10 had zero. Anti-reproducer prompt's locate→read→edit→verify protocol fails to even produce a candidate diff on a third of stratified instances.
- 4/75 created `test_fix.py` despite anti-reproducer rule — prompt is **leaky**; tool-side enforcement is the right shape.

**BFCL pre-SpecDD baseline complete** (`acceptance/bfcl/pre_specdd_v141/rep_1/`, 2026-05-04):
- TOTAL: 946/1240 = **76.29%** in ~3.5h wall
- Parallel cliff: parallel_multiple sits 33pp below single-call avg.

---

## Explicit non-goals this session

- **Lever 3** — held until empty_patch class is fully addressed. Lever 3 needs clean separation of constraint vs reasoning failures; the empty_patch class confounds that boundary until early-bail intervention lands.
- **Phase B trace inspection on matplotlib-24870** — non-blocking diagnostic. Doesn't gate v1.6 tag; informs whether bailout-after-forbid is a real interaction or just hard-instance variance. Slated for v1.7 prep.
- **Tagging v1.6.0 with current data** — would lock in unverified ship floor. Wait for v3.

---

## Background tasks (queued, non-blocking)

These do not block v1.6.0 tag; revisit after the overnight v3 lands.

- Retire v1.3 directive reprompt code in `cli.py` (~15 min) — superseded by SpecDD Lever 1 spec validator
- `min_added_lines` as per-requirement predicate kind in `src/luxe/spec.py`
- `ast_query` and `manual` predicate full integrations (currently stubbed)
- Tune Mode B thresholds based on broader bench data (currently 10 tools / 4000 tokens / step 5) — extra signal incoming from v3 + Phase B
- Bring `benchmarks/swebench/run.py` ETA format into BFCL standard (group + global counts) — cosmetic
- Per-fixture `.sdd` contracts on the maintain_suite (Lever 3 prep) — depends on `trace:` field audit
- **Minimality-bias A/B** (orthogonal experiment proposed pre-Lever-2): adds `swebench_bugfix_minimal` PromptVariant. Re-evaluate after v1.6 ships — may not be needed if `empty_patch` is already in target range.

---

## Memory entries (read first)

External benchmark program — current focus:
- `project_v16_creation_only.md` — **PRIMARY** v1.6 creation-only forbids ship state + n=14 smoke result + n=75 v3 plan
- `project_v15_specdd_lever2_shipped.md` — v1.5 Lever 2 ship state + paired-mechanism reframe
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
- `feedback_exception_hierarchy_catch_order.md` — when except clauses cover an inheritance hierarchy, derived class first
- `feedback_fixture_prep_dirty_tree.md` — synthetic-`.sdd`-class fixture prep needs `--allow-dirty` in the agent invocation
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
- `project_regrade_local_origin_bug.md` — fixed in v1.4.1
- `project_gh_auth_flake.md` — open but mitigated by `--retry-errors`
- `project_lmstudio_loop.md` — open
- `project_omlx_metal_crashes.md` — latent

---

## 30-second orientation

**luxe** is an MLX-only repo maintainer for Apple Silicon (oMLX backend on `localhost:8000`). Takes a goal + repo, opens a PR. Mono-only since v1.0 — single model, single agent loop, single `luxe maintain` command. Champion: `Qwen3.6-35B-A3B-6bit` in `configs/single_64gb.yaml`.

**What's shipped through v1.5.0-rc-2**:
- v1.0 — mono-only; 10 fixtures; strict gates
- v1.1 — pinned work_dir default + manage_strict overlay → 9/10
- v1.2 — per-tool subphase pass: cve_lookup gated to manage; bash chain-hardening; read_file binary detection
- v1.3 — read_file dedup exemption + lpe-typing fixture surgery + reprompt-on-doc + `_diff_against_base` fix
- v1.4 — SpecDD Lever 1: programmatic Definition of Done; per-requirement spec validator; reprompt gate uses spec
- v1.4.1 — citation-linter bare-filename fallback (Mode A) + Mode B mid-loop write-pressure (opt-in) + sidecar regrade lint re-run
- v1.5.0-rc-2 — SpecDD Lever 2 paired-mechanism (`.sdd` constraint + WRITE_PRESSURE actuation); 619 tests; v2 n=75 result hit ship floor on 3/4 metrics

**v1.6.0-rc-1 (this session, 2026-05-06 evening)**:
- New `.sdd` section `Forbids creating` — operation-aware policy (create-only vs always-fires)
- `creating: bool` threaded through `_check_spec_forbids` + `is_forbidden`; computed at write-time from `Path.is_file()`
- Distinct error message for create-only matches (recovery-gradient wording)
- SWEBENCH_SDD_BODY split: `repo_root/**` in Forbids; all scaffolding patterns in Forbids creating; v2 escapes (`test_*.py`, `test_fix_*.py`) added to the broad create-ban
- Phase A audit (n=500): broad `**/test_*.py` create-ban is safe — zero Verified gold patches create a `test_*.py`
- Phase C smoke (n=14): new_file=0 ✅; sympy-12481 reroute (invent → strong gold-match) validated architectural premise
- 643 tests passing (+24 vs v1.5-rc-2 baseline)
- **Pending**: v3 n=75 rerun confirming ship floor; harness scoring; tag

**What's queued**:
- **v1.6.0** (final tag, next overnight) — v3 rerun confirming creation-only floor. See `~/.claude/plans/cozy-wiggling-conway.md`.
- v1.7.0 — early-bail intervention #1 priority (addresses 10 of 14 v1 paired-mechanism empty_patch). Then b2 multi-site retrieval. Then in-loop test execution feedback. Then Phase B Mode B threshold tuning. Lever 3 on backlog but de-prioritized.

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
| `src/luxe/agents/single.py` | mono runner — agentic loop end-to-end; `_build_sdd_block` injects Repository contracts (v1.5) |
| `src/luxe/agents/loop.py` | shared loop; Mode B write-pressure injection (v1.4.1); tool-call ceiling OR-branch + `_POST_WRITE_IDLE_MAX` clean exit + `tc.name` loop-boundary normalization (2026-05-10) |
| `src/luxe/agents/prompts.py` | prompt registry + TaskOverlay; doc/manage strict variants |
| `src/luxe/citations.py` | diff-aware citation linter; bare-filename fallback (v1.4.1); `spec_violation`/`spec_orphan` (v1.5) |
| `src/luxe/sdd.py` | **`.sdd` parser** — seven canonical sections incl. **`forbids_create` (v1.6)**, tolerant header normalization (`Forbids creating` → `forbids_create`) |
| `src/luxe/spec_resolver.py` | chain assembly + glob matching — `find_all_sdd`, `resolve_chain`, `format_sdd_block`; **`is_forbidden(rel, *, creating)` kwarg-only required (v1.6)**; **`all_forbids_create` helper (v1.6)** |
| `src/luxe/spec.py` | SpecDD Lever 1 data model (`Requirement`, `Spec`, YAML round-trip) |
| `src/luxe/spec_validator.py` | SpecDD Lever 1 predicate evaluator + reprompt-text helper |
| `src/luxe/tools/base.py` | `dispatch_tool` (tool exceptions captured as retry-able errors); `name.strip()` at dispatch boundary tolerates whitespace from GLM-style emit shapes (2026-05-10) |
| `src/luxe/tools/fs.py` | write-time honesty guards; `_check_spec_forbids` pre-write enforcement; **`creating: bool` threaded (v1.6) — `_write_file` computes via `Path.is_file()`; `_edit_file` always `False`; create-only error wording for recovery gradient** |
| `src/luxe/luxe.sdd` | root invariants (v1.5 dogfood) — Forbids retired `src/swarm/**` etc. |
| `src/luxe/agents/agents.sdd` | (v1.5 dogfood) — prompt registry as single source of truth |
| `src/luxe/tools/tools.sdd` | (v1.5 dogfood) — honesty guards before Forbids; cve_lookup gating |
| `benchmarks/maintain_suite/maintain_suite.sdd` | (v1.5 dogfood) — bench rules |
| `CLAUDE.md` | (v1.5) — auto-loaded by Claude Code; points at the `.sdd` chain |
| `src/luxe/backend.py` | `chat()` accepts `repeat_penalty`; `unload_model()`, `loaded_models()` |
| `src/luxe/cli.py` | `luxe maintain` (mono only); `--spec-yaml` for SpecDD reprompt gate |
| `src/luxe/config.py` | `RoleConfig` w/ system/task prompt + overlay ids + repeat_penalty |
| `benchmarks/maintain_suite/run.py` | bench harness; `Variant` carries prompt + overlay overrides; `_inject_forbids_create_sdd` writes `<repo>.sdd` + appends to `.git/info/exclude` for per-fixture SpecDD Lever 2 (2026-05-10); `LUXE_WRITE_PRESSURE=1` env default |
| `benchmarks/maintain_suite/grade.py` | grading + strict gates + multi-variant `v1_release_gate`; `Fixture.forbids_create: list[str]` field (2026-05-10) |
| `benchmarks/maintain_suite/fixtures.yaml` | the 10 v1 fixtures (each w/ `requirements:` block) |
| `benchmarks/swebench/` | SWE-bench Verified adapter (preds-only + Docker harness wrapper + compare) |
| `benchmarks/swebench/smoke_inspect.py` | inspector v2 — mechanical + gold-proximity tier (`--gold-source`); 5 signals, line-based hunk proximity, hunk coverage |
| `benchmarks/swebench/run.py` | preds-only runner; idempotent resume; **`--no-inject-sdd` + `--no-write-pressure` flags (v1.5) for ablation** |
| `benchmarks/swebench/adapter.py` | synthetic `.sdd` injection (v1.5); paired-mechanism env wiring + commit.gpgsign override (v1.5.0-rc-2); **SWEBENCH_SDD_BODY split into Forbids + Forbids creating (v1.6); broad `**/test_*.py` create-ban added** |
| `benchmarks/swebench/compare_runs.py` | (v1.5) — pre/post predictions delta report (per-instance + class-level + summary) |
| `benchmarks/swebench/subsets/v1_baseline_n75.json` | 75 stratified instances, 12 repos — the pre-SpecDD anchor target |
| `benchmarks/swebench/subsets/v16_smoke_n14.json` | **(v1.6)** — Phase C smoke: 4 v2 regressions + 5 v2-strong preservation + 5 random; deterministic seed 20260506 |
| `benchmarks/swebench/subsets/probe_n10.json` | n=10 A/B subset (4 easy + 6 medium across 10 distinct repos) |
| `benchmarks/swebench/subsets/probe_12907.json` | single-instance probe used for the original hypothesis-stall trace |
| `benchmarks/bfcl/` | BFCL v3 adapter (raw + agent modes, schema converter, grader); resume + ETA in `run.py` |
| `configs/single_64gb.yaml` | maintain_suite config — `Qwen3.6-35B-A3B-6bit`, `manage_strict_only` overlay |
| `configs/single_64gb_swebench.yaml` | swebench config — `swebench_strict_only` overlay (anti-reproducer prompt); the n=75 default |
| `configs/single_64gb_swebench_counterexample.yaml` | A/B variant with falsification clause; **negative control, not promoted** |
| `scripts/regrade_local.py` | sidecar regrade w/ citation re-run (v1.4.1) |
| `scripts/register_omlx_models.py` | symlink HF cache → `~/.omlx/models/` |
| `lessons.md` | running postmortem; latest entry covers v1.6 creation-only architectural shift |
| `~/.claude/plans/fancy-honking-lerdorf.md` | external benchmark plan (SWE-bench n=75 + BFCL v3) |
| `~/.claude/plans/fluffy-brewing-lemur.md` | SpecDD plan (Levers 1/2/3) |
| `~/.claude/plans/humble-prancing-patterson.md` | v1.5.0 ship plan + failure-mode analysis |
| `~/.claude/plans/cozy-wiggling-conway.md` | **v1.6.0 ship plan (this session)** — creation-only forbids architecture + audit gates + Phase D ship floor |

---

## oMLX configuration

`~/.omlx/settings.json`:
```json
"max_model_memory": "36GB",
"idle_timeout": { "idle_timeout_seconds": 1800 },
"sampling": { "max_context_window": 49152 }
```

`max_context_window` was bumped from 32768 (default) to 49152 on 2026-05-10
during the m5max_moe bake-off — qwen3-coder-next-80B under realistic
retrieval load on `nothing-ever-happens-document-config` hits 33k+ per
turn and oMLX returns a hard 400 below the new ceiling. Qwen3 family
natively supports 128k+, so 48k is well within model architecture.
**This is per-machine state and not version-controlled** — any new bench
host needs the same bump.

System-level Metal wired ceiling — kept aligned with `max_model_memory`:
```bash
sudo sysctl iogpu.wired_limit_mb=36864
echo "iogpu.wired_limit_mb=36864" | sudo tee -a /etc/sysctl.conf
```

API key for HTTP requests: `export OMLX_API_KEY=omlx-sdb25582k3mq8pf9` (in user's shell init; the bench harness reads it).

**Restart oMLX** any time `settings.json`, `model_settings.json`, or new symlinks land: `brew services restart omlx`.

## maintain_suite bench-host prereqs

The 10-fixture suite includes fixtures that shell out to `npm test` as
their tests_pass predicate (`neon-rain-implement-reset-shortcut`).
Without `node` + `npm` on the bench host, those fixtures rc=127 and are
misscored as model failures. `brew install node` is the one-shot fix on
macOS. Documented here because the toolchain prereq isn't obvious from
the fixture YAML alone.

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
- **At temp=0 the variance collapses to deterministic vectors** (probe_a == probe_b across all 10 fixtures on 2026-05-01 PM). At temp=0 a 1-fixture delta IS the signal — except on SWE-bench where prompt-cache state and instance ordering can produce ±2-3 strong/empty drift between runs (the "variance budget" referenced in v1.6 ship floor).
- **Offline mode caps every fixture at 4/5** — `gh pr create` always fails (no GitHub remote), so `pr_opened` (1pt of 5) never fires offline. Every PASS reads as 4/5; gate math (≥8 fixtures with score ≥4) still works correctly.
- **`origin/<branch>` in offline-cache repos is a stale-ref trap** — post-2026-05-01 runs push to local branches (`refs/heads/...`) which do NOT update remote-tracking refs. Use `git diff base..<branch>` (local ref) or sidecar regrade.
- **Dense >30B mxfp8 doesn't fit on 64GB Mac under load** — granite-4.1-30b-mxfp8 spiked 22GB+ wired and pushed system into swap. MoE models (Qwen3.6-35B-A3B at ~3B active) run comfortably; dense models don't.
- **`stuck_after_done` doesn't always mean failure** — Qwen3.6-35B-A3B often ships a real diff then trips the stuck-loop detector on cleanup. Distinguishes from `stuck_no_output` (never engaged).
- **`run.py` resume model treats `status: error` as `skip_done` by default** — if a sweep dies before any model invocation, re-launching without `--retry-errors` silently skips every fixture and prints a zeroed Summary. Either pass `--retry-errors` or `rm -rf` the output dir.
- **`is_forbidden` is now kwarg-only required (v1.6)** — `chain.is_forbidden(rel, creating=...)`. Callers that pass positional-only will fail at runtime. Tests use `creating=False` for edit-time checks; bench paths compute `creating = not Path.is_file()`.

---

## Recent commit trail (most recent first)

Run `git log --oneline -20` for fresh state. Highlights from recent sessions:

```
1d848ae  maintain_suite: broaden JS forbids_create — catch hyphen-prefix variants (2026-05-10)
b00ffe1  maintain_suite: per-fixture Forbids creating + synth .sdd injection (2026-05-10)
f962ee6  agents/loop: normalize tool name at the loop boundary too (2026-05-10)
4590e68  maintain_suite: default LUXE_WRITE_PRESSURE=1 + m5max_moe runbook docs (2026-05-10)
6cf6b2a  agents/loop: WRITE_PRESSURE tool-ceiling branch + post-write idle exit (2026-05-10)
fceff7e  tools/base: tolerate whitespace in tool names from dispatch_tool (2026-05-10)
5cc3c87  maintain_suite: M5 Max bench-env prep + multi-variant repo hygiene (2026-05-10)
2240f22  docs: v1.6.0 SHIPPED — n=75 v3 + Docker harness 36/75 (48.0%)
4e9df21  swebench/harness: per-instance report aggregator for swebench >= 4.x
e49d7da  docs: RESUME.md — Phase D Step 1 done (n=75 v3 ran clean)
3174a79  docs: rewrite README for v1.6.0-rc-1 (mono-only, SpecDD Lever 2)
92ceb4c  docs: v1.6.0-rc-1 state + creation-only architectural shift entry
49c8acb  v1.6.0-rc-1: SpecDD Lever 2 — creation-only forbids (operation-aware policy)
04c8aac  docs: v1.5.0-rc-2 state + paired-mechanism v1 result + Forbids tightening
1d5b006  v1.4.1: citation-linter bare-filename fallback + Mode B write-pressure + regrade lint re-run
707bab8  v1.4.0: SpecDD Lever 1 — programmatic Definition of Done; first 10/10 bench
```

---

## When in doubt

`git log --oneline -20` tells the trajectory. `lessons.md` has postmortems of every failure pattern. The user prefers terse, action-oriented responses — don't summarize what they can read; tell them the next step.

The user is comfortable with auto mode but draws hard lines on destructive shared-system actions (oMLX config, sudo, force-push, deletes outside their workspace). When in doubt, write the change but ask before applying. Do NOT push to remote unless explicitly asked.
