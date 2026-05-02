# luxe — session resume document

Snapshot for picking up cross-session. Reflects state at HEAD `9b38d93`
+ uncommitted Phase-0 grader fixes (2026-05-01 evening). The bench
infrastructure went offline-only (fixtures cloned from a local cache,
no network needed) and the v1 acceptance grader had two latent bugs
fixed (Bugs 1+2; see `lessons.md`). Branch B was queued to launch but
deferred: at temp=0 the implement category hits 4/4 baseline, which
*obsoletes* the `implement_via_cot` overlay. Real strategic question
shifted from "lift implement" to "lift doc/manage" — which the
existing overlay set doesn't address.

---

## 30-second orientation

**luxe** is an MLX-only repo maintainer for Apple Silicon (oMLX backend
on `localhost:8000`). Takes a goal + repo, opens a PR. **Mono-only as
of v1.0** — single model, single agent loop, single `luxe maintain`
command. Champion: `Qwen3.6-35B-A3B-6bit` in `configs/single_64gb.yaml`.

**What's in flight:** v1.0 release path. The 10-fixture acceptance
suite needs **≥8/10 PASSes** to ship. After Phase 0 grader fixes,
fixture surgery on `lpe-typing` and `neon-rain-modules`, and Branch C
calibration on `nothing-config`, sidecar regrade against probe_b
shows **6/10** with the current grader + current fixtures. A fresh
full run is the E2E confirmation step — expected 6-8/10 (the surgered
neon-rain-modules will pass on a fresh "Update" run since the model
won't be forced into a destructive rewrite; lpe-typing depends on
whether the model writes both halves of the task — typing AND a
docstring — this time).

**The remaining gap to ship**: 2-3 passes (6/10 → 8/10). Levers
remaining: (a) overlay for doc/manage if a fresh run still tops out at
6-7/10, (b) more fixture surgery on `isomer-document-quickstart`
(prior runs were destructive — model rewrote 148 lines of README), or
(c) `pr_opened`-as-1pt question (every offline run currently caps at
4/5 because `gh pr create` fails without a GitHub remote).

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
| Overnight MoE | 04-29 | qwen3.6-35B-A3B {4,6}bit, gemma-4-26B-A4B {4,8}bit | qwen3.6-35B-A3B-6bit |
| 8-bit completion | 04-30 | qwen3.6-35B-A3B-8bit, qwen-coder-32B-8bit, qwen3-coder-30B-A3B-8bit | none beat 6-bit |
| Granite-4.1 (3b only) | 04-30 | granite-4.1-3b-bf16 | 1/10 real (model too small for the loop) |
| v1 baseline (post P0 fixes) | 04-30 | qwen3.6-35B-A3B-6bit | 5/10 (then 7/10 on 05-01 re-run; sampling variance is real at N=10) |
| **Phase 1 prompt-shaping** | **05-01** | **6 cells: baseline + baseline-rp + cot + sot + hads + combined-rp** | **baseline 7/10; structural variants uniformly 4/4 implement but regressed doc/manage; no Gate A winner** |
| **temp=0 baseline (Phase 0 grader fixed)** | **05-01 PM** | **qwen3.6-35B-A3B-6bit @ temp=0** | **5/10 stable across 2 runs; per-task: impl 4/4, doc 1/5, manage 0/1** |

Full results: `acceptance/<phase>/comparison.json` per phase dir.

> **Caveat on pre-Phase-0 results**: every row above 04-30 was graded
> against a grader that didn't fire its strict gates (Bug 2 — see
> `lessons.md`). The "real PASS leader" column is approximately right
> for top-of-table comparisons but the absolute pass counts almost
> certainly include 1-2 false positives per cell that destructive_diff
> would have caught. Re-grade with `scripts/regrade_local.py` if the
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

In priority order:

1. **Decide on Branch C calibration for `nothing-config`** — confirmed
   gate-side failure (model produced 136-line CONFIG.md, regex too
   narrow). Per `~/.claude/plans/v1-ship-and-prompt-sweep.md` Branch C
   gate, requires a `lessons.md` entry per fixture with (a) semantic
   acceptability, (b) failure category, (c) targeted-vs-general
   justification before any `fixtures.yaml` edits. (a)/(b) are already
   in the bag; (c) needs to specify the alternate accepting pattern
   that matches markdown-table UPPER_SNAKE listings without admitting
   vacuous prose.
2. **Phase 2 fixture surgery** — `lpe-rope-calc-document-typing`'s
   base_sha already has type hints (the task is misaligned with the
   file state); `neon-rain-document-modules`'s base_sha already has
   `ARCHITECTURE.md` (the task says "Create" but the file exists,
   forcing the model into a destructive rewrite that triggers the
   gate). Both want either a new base_sha or a fixture replacement.
3. **F2.3 specprefill_enabled probe** — flip the flag on Qwen3.6-35B-A3B-6bit
   in `~/.omlx/model_settings.json`, restart oMLX, run a 5-fixture
   sub-sweep. Keep on if median wall drops ≥15% AND no quality
   regression. Decode-speed lift would make every future bake-off
   cheaper. Master plan §F2.3.
4. **Decide if `pr_opened` (1pt of 5) should remain a gate** when
   running offline. With local-cache origin (no GitHub remote), every
   run caps at 4/5 because `gh pr create` fails. Either (a) accept the
   4-point ceiling for offline runs and adjust the 8/10 threshold, or
   (b) make `pr_opened` no-op when origin isn't a GitHub host.
5. **F2.1 IFS-lite** — refactor `expected_outcome` into weighted
   sub-instructions; report Instruction Following Score per cell.
   Master plan §F2.1.
6. **F2.2 logprob capture** — 15-min probe to test if oMLX honors
   `logprobs: true`. Master plan §F2.2.
7. **Drop Gemma-4-26B-A4B** entries from `~/.omlx/model_settings.json`
   if still there — ship with empty `chat_template` and fail with HTTP
   400 in the chat-driven loop.
8. **Re-grade prior bake-offs with the fixed grader** if their
   numbers are load-bearing for any decision. `scripts/regrade_local.py
   --output acceptance/<phase>` walks any prior run dir.

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
9b38d93  scripts: register_omlx_models — require --models-file (post-v1.0 cleanup)
b81b628  bench: Branch B task-type overlays + multi-variant v1_release_gate fix
506b190  docs: align iogpu.wired_limit_mb in RESUME.md with actual value (36864 MB)
c6a83c6  bench: add timestamps + run progress + rolling ETA to run.py output
b341ea9  bench: prompt-shaping v2 — fix CoT XML collision and HADS deliberation loop
cb1d12c  bench: prompt-shaping bake-off scaffolding (registry, RoleConfig, variant matrix)
c2b8484  bench: capture tool exceptions + drop dead escalate refs + sharpen regex_present errors
76d3e7a  bench: add orphan_file grader gate — close last "tests pass for the wrong reason" hole
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
