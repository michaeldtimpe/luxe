# luxe — session resume document

Snapshot for picking up cross-session. Reflects state at HEAD `9b38d93`
(2026-05-01) — Phase 1 prompt-shaping sweep complete, Branch B
implementation committed but **not yet run**. The next launchable
action is the Branch B sweep against the new variant file.

---

## 30-second orientation

**luxe** is an MLX-only repo maintainer for Apple Silicon (oMLX backend
on `localhost:8000`). Takes a goal + repo, opens a PR. **Mono-only as
of v1.0** — single model, single agent loop, single `luxe maintain`
command. Champion: `Qwen3.6-35B-A3B-6bit` in `configs/single_64gb.yaml`.

**What's in flight:** the v1.0 release path. The 10-fixture acceptance
suite needs **≥8/10 PASSes** to ship. Current best is 7/10 (baseline,
2026-05-01 sweep). Phase 1 prompt-shaping showed structural prompts
(CoT, SoT, HADS) lift `implement` to a 4/4 ceiling but regress
`document` and `manage` — no cell beat baseline overall. Branch B
(per-task-type overlays, applies CoT only to implement+bugfix) is
implemented and queued; expected to compose baseline's doc/manage
performance with the implement ceiling for an 8/10 projection.

**Resume command** is at the bottom — launches the Branch B sweep.

---

## The bench-as-truth pattern

Every model claim goes through:

1. Run `python -m benchmarks.maintain_suite.run --variants <yaml>`.
2. Read the printed comparison table — `pass/fail/wall/tokens/bailouts`
   per cell. (As of `c6a83c6` the per-fixture output also shows
   `[HH:MM:SS] N/total ETA ~Xmin` headers.)
3. **Inspect every PASS PR by hand** via `gh pr diff`. Historical false-
   positive rate is 30-50% even with the tighter grader.
4. Strict regrade with `scripts/regrade_phase2.py` for an automated
   sanity check on diff shape.

Real PASS count is always ≤ printed count. Every historical bake-off
has had at least one false-positive PASS.

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

Full results: `acceptance/<phase>/comparison.json` per phase dir.

---

## Phase 1 outcome (the table that matters)

`acceptance/prompt_shaping/comparison.json`:

| Cell | impl (4) | doc (5) | manage (1) | Total |
|---|---|---|---|---|
| **champ-baseline** | 3 | 3 | 1 | **7** |
| champ-baseline-rp__rp105 | 3 | 1 | 0 | 4 |
| champ-cot | **4** | 1 | 1 | 6 |
| champ-sot | **4** | 2 | 0 | 6 |
| champ-hads | **4** | 1 | 0 | 5 |
| champ-combined-rp | **4** | 1 | 0 | 5 |

Hidden inside: every prompt-shaped variant cleared the implement
ceiling (4/4) — including `lpe-rope-calc-implement-strict-flag`, which
baseline misses. The composition hypothesis (Branch B) is: apply the
implement-friendly framing only on implement+bugfix, keep baseline on
docs/manage. Projected to **8/10** if baseline's 3+1 doc/manage holds.

---

## Strict gates currently in place

Tool-side (`src/luxe/tools/fs.py`, write-time): `_check_placeholder_text`,
`_check_role_path`, `_check_mass_deletion`.

Tool dispatch (`src/luxe/tools/base.py`): tool exceptions are now
captured as retry-able `ToolCall.error` strings instead of escaping
`run_agent`. Catches the absolute-path crash that previously killed
`neon-rain-document-modules` at wall=0s/tokens=0.

Post-PR strict gates (`benchmarks/maintain_suite/grade.py`):
`destructive_diff`, `role_name_leak`, `placeholder_diff`, `vacuous_test`,
`orphan_file` (catches the granite-3b `.ts`-next-to-`.js` exploit).

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

Per-fixture (as of `c6a83c6`):
```
━━━ run 53/60  [19:43:35]  ETA ~24min  [variant]  fixture  [task_type]  goal...
      grading: ...
  → invoking `luxe maintain` (variant)
  PASS  [19:46:12 +2:37]  score=5/5  wall=157s  tokens=82583  ...
```

Token-interval progress logging fires every 5000 completion tokens
(via `LUXE_TOKEN_LOG_INTERVAL`; 0 disables). Captured in
`acceptance/<output>/<variant>/<fixture>/stdout.log`.

---

## Resume command — Branch B sweep

```bash
cd /Users/michaeltimpe/Downloads/luxe

# Confirm oMLX state
.venv/bin/python -c "
from luxe.backend import Backend
b = Backend(); listed = set(b.list_models())
m = 'Qwen3.6-35B-A3B-6bit'
print(f'  {\"✓\" if m in listed else \"✗\"} {m}')
"
# If ✗: brew services restart omlx

python -m benchmarks.maintain_suite.run \
    --variants benchmarks/maintain_suite/variants_task_type_overlay.yaml \
    --output acceptance/task_type_overlay \
    --per-fixture-timeout 1800 \
    --all
```

2 cells × 10 fixtures = 20 runs. Expect **1-3h wall** based on Phase 1
averages.

**Promotion criterion** (locked, see `~/.claude/plans/task-type-overlays.md`):
- `champ-implement-via-cot` real-PASS ≥ 8/10
- AND beats `champ-baseline-control` by ≥1 fixture pass
- The bench will print `v1 release : YES (cleared by: <cell>)` when
  the per-cell gate fires.

---

## After-the-bench checklist

1. Read the comparison table — note pass count and which cell cleared.
2. **Hand-grade every PASS PR** with `gh pr diff --repo <owner/repo>
   <num>`. Pay particular attention to the new doc/manage passes — the
   structural prompts being suppressed there is the entire Branch B
   hypothesis.
3. If `champ-implement-via-cot` ≥ 8/10 AND beats control by ≥1: **ship
   v1.0**:
   - Add `task_overlay_id: implement_via_cot` to `configs/single_64gb.yaml`'s
     `roles.monolith` block.
   - Run a 1-cell × 10-fixture confirmation against the production config.
   - If still 8/10, bump `pyproject.toml` `version` from `1.0.0.dev0` to
     `1.0.0`. Tag `v1.0.0`. Push.
   - Append the v1-ship entry to `lessons.md`.
4. If overlay ties baseline at 7/10: re-run once for sample size before
   deciding (Phase 1 baseline already swung 5→7 between identical runs).
5. If overlay fails to clear 8/10 even on the second run: fall through
   to Branch C in the master plan. **`lessons.md` entry required first**
   per the plan, before any fixture.yaml or configs/ edits.

---

## Open work / next steps

In priority order:

1. **Branch B sweep** — the resume command above. Single launchable
   action; everything else is downstream.
2. **F2.3 specprefill_enabled probe** — flip the flag on Qwen3.6-35B-A3B-6bit
   in `~/.omlx/model_settings.json`, restart oMLX, run a 5-fixture
   sub-sweep against `acceptance/v1_default_post_fix` numbers. Keep on
   if median wall drops ≥15% AND no quality regression. Decode-speed
   lift would make every future bake-off cheaper. See master plan §F2.3.
3. **F2.1 IFS-lite** — refactor `expected_outcome` into weighted
   sub-instructions; report Instruction Following Score per cell. Would
   distinguish "variant X engaged but missed sub-step" from "variant X
   did nothing" on partial-credit fixtures. Master plan §F2.1.
4. **F2.2 logprob capture** — 15-min probe to test if oMLX honors
   `logprobs: true`. If yes, build the capture + analysis path. Master
   plan §F2.2.
5. **Drop Gemma-4-26B-A4B** entries from `~/.omlx/model_settings.json`
   if still there — ship with empty `chat_template` and fail with HTTP
   400 in the chat-driven loop.
6. **Hand-grade Phase 1 PASS PRs** if you want to confirm the no-winner
   verdict beyond the per-cell totals. 33 PASSes across 6 cells; the
   ones worth checking are the new lpe-strict-flag passes (4 across
   the structural variants — confirm real work, not regex-gaming).

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
- **Sampling variance at N=10 is large** — Phase 1 baseline went 5→7
  between identical runs. ±1 fixture deltas between cells are noise.
  The promotion criterion gates on absolute floor (8/10) for this reason.
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
