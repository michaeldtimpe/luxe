# luxe — session resume document

Snapshot for picking up cross-session. Everything here is true as of the
v1.0 simplification commit (2026-04-30).

---

## 30-second orientation

**luxe** is an MLX-only repo maintainer for Apple Silicon (oMLX backend on
`localhost:8000`). Takes a goal + repo, opens a PR.

As of v1.0, **luxe is mono-only.** Single model, single agent loop, single
`luxe maintain` command. No swarm, no micro, no phased. The deletion
landed after the 8-bit completion bake-off confirmed `mono__qwen3.6-35b-a3b-6bit`
beat every multi-agent topology we'd tested across 6 bake-offs. See
`lessons.md` ([2026-04-30] entries) for the postmortem.

**Current champion**: `Qwen3.6-35B-A3B-6bit` (set as default monolith in
`configs/single_64gb.yaml`). First model to produce production-quality
implementations of `the-game` (real React keydown handler) and `neon-rain`
(Shift+R via existing event bus, npm test passes naturally).

**In flight**: re-baseline against the 10-fixture v1.0 acceptance suite
with the new tighter grader. Need ≥8/10 PASSes for the v1 release gate.

---

## The bench-as-truth pattern

Every model claim in this project goes through:

1. Run `python -m benchmarks.maintain_suite.run --variants <yaml>` against
   the 10 v1.0 fixtures.
2. Read the printed comparison table — `pass/fail/wall/tokens/bailouts`
   per cell.
3. **Inspect every PASS PR by hand** via `gh pr diff` — the regex grader
   has been gamed in 4+ distinct ways (placeholder text, role-name leaks,
   destructive deletions, vacuous tests, regex-on-pre-existing content,
   single-line edits matching multi-call-site requirements, vacuous "no
   findings" audits).
4. Strict regrade with `scripts/regrade_phase2.py` (works on any output
   dir) for an automated sanity check on diff shape.

The bench's "real PASS" count is always ≤ the printed count. Every
historical bake-off has had at least one false-positive PASS. The new
`min_matches` / `min_added_lines` grader fields close the most-common
gaming patterns but are not a substitute for hand inspection.

---

## Files of consequence (post-simplification)

| Path | Purpose |
|---|---|
| `src/luxe/agents/single.py` | the runner — drives the agentic loop end-to-end |
| `src/luxe/agents/loop.py` | shared agentic loop with token-interval logging |
| `src/luxe/citations.py` | diff-aware citation linter + ValidatorEnvelope dataclasses |
| `src/luxe/tools/fs.py` | write-time honesty guards (placeholder/role-name/mass-deletion) |
| `src/luxe/backend.py` | `Backend.unload_model()`, `unload_all_loaded()`, `loaded_models()` |
| `src/luxe/cli.py` | `luxe maintain` (mono only), `luxe pr/runs/unload/check/serve` |
| `benchmarks/maintain_suite/run.py` | bench harness; variant matrix is now `(model_label, model_id)` pairs |
| `benchmarks/maintain_suite/grade.py` | grading + strict gates + `_check_regex_present` w/ `min_matches`/`min_added_lines` |
| `benchmarks/maintain_suite/fixtures.yaml` | the 10 v1 fixtures (5 implement / 4 document / 1 manage hybrid) |
| `configs/single_64gb.yaml` | the only config — currently `Qwen3.6-35B-A3B-6bit` |
| `scripts/register_omlx_models.py` | symlink HF cache → `~/.omlx/models/` |
| `scripts/regrade_phase2.py` | apply strict gates to existing results |
| `lessons.md` | running postmortem — entries dated; mono-only pivot is at the bottom |

What's gone: `src/luxe/pipeline/`, `src/luxe/benchmark/`, `src/luxe/metrics/`,
`src/luxe/agents/{microloop,phased,architect,synthesizer,validator,worker}.py`,
`src/luxe/{escalation,mode_select}.py`, `configs/{swarm,qwen,deepseek,mode}.yaml`,
plus `tests/test_{mode_select,pipeline_model,escalation,validator_contract,
benchmark,architect,resume_drift}.py`. Also: `--mode`, `luxe run`,
`luxe benchmark*`, `luxe compare`, `luxe resume` are no longer commands.

---

## oMLX configuration (must-restart-to-apply)

`~/.omlx/settings.json`:
```json
"max_model_memory": "48GB",
"idle_timeout": { "idle_timeout_seconds": 1800 }
```

System-level Metal wired ceiling — kept aligned with `max_model_memory`
above so the GPU wired pool exactly fits the largest model + KV cache
without leaving idle wired memory on the table:
```bash
sudo sysctl iogpu.wired_limit_mb=36864
echo "iogpu.wired_limit_mb=36864" | sudo tee -a /etc/sysctl.conf
```

`~/.omlx/model_settings.json` has explicit per-model entries for every
champion-tier model. Add new entries when registering new models —
`register_omlx_models.py` handles symlinks but not the settings.

**Restart oMLX** any time `settings.json`, `model_settings.json`, or new
symlinks land: `brew services restart omlx`.

---

## Bake-off history (chronological summary)

| Phase | Date | Variants | Real PASS leader |
|---|---|---|---|
| Phase 1 — mono shootout | 04-28 | qwen-coder 7B/14B/32B mono | 14B (2/5) by tiebreaker |
| Phase 2 — head-to-head | 04-28 | mono/swarm/micro × {14B, 1.5B} | mono 14B + swarm 14B tied at 2/5 real |
| Phase 3 — phased mode | 04-29 | phased__qwen-coder-14b | 0/5 real (architect rubber-stamped fabrication) |
| Mono precision | 04-29 | 14B-8bit, 32B-6bit, 30B-A3B-6bit, abliterated-14B-6bit | 30B-A3B-6bit (2-3/5 real) but bimodal |
| Overnight MoE | 04-29 | qwen3.6-35B-A3B {4,6}bit, gemma-4-26B-A4B {4,8}bit | **qwen3.6-35B-A3B-6bit (2/5 real, incl. neon-rain)** |
| 8-bit completion | 04-30 | qwen3.6-35B-A3B-8bit, qwen-coder-32B-8bit, qwen3-coder-30B-A3B-8bit | none beat 6-bit; 32B-8bit dropped from roster (2 timeouts) |

After Phase 3 + 8-bit completion, the conclusion was unambiguous: mono
wins at every model scale we've tested, and the 6-bit champion stays.

Full results: `acceptance/<phase>/comparison.json` per phase dir.

---

## Strict gates currently in place

Tool-side (`src/luxe/tools/fs.py` — block at write time):
- `_check_placeholder_text` — refuses `<paste...here>`, `// Your X code here`,
  `// TODO: implement Y`, multi-word variants
- `_check_role_path` — fuzzy match on tokenized path components against
  `worker_*`, `drafter`, `verifier`, `architect`, etc. (kept post-deletion
  to defend against historical evasions creeping back)
- `_check_mass_deletion` — refuses overwriting ≥50-line file with ≤5-line
  stub when deletion ratio ≥10×

Post-PR strict gates (`benchmarks/maintain_suite/grade.py`):
- `destructive_diff` (deletions ≥30 + ratio ≥5×)
- `role_name_leak` (same fuzzy match as tool guard)
- `placeholder_diff` (regex match in added lines)
- `vacuous_test` (new test file passes against `base_sha` worktree)

Grader-level controls per fixture (NEW 04-30):
- `expected_outcome.min_matches` — pattern must hit in ≥N distinct added
  lines. Defeats single-edit gaming on multi-call-site tasks.
- `expected_outcome.min_added_lines` — diff must add ≥N total lines.
  Defeats rename-only / one-line "edits" that match the regex.

Diff-aware grading: `_check_regex_present` reads `git diff <base> HEAD`
and scans only `+` lines. Closes the loophole where a model touched a
file that already contained the pattern.

Bailout categorization: `_classify_bailout` in `run.py` produces
`stuck_after_done` / `stuck_no_output` / `refusal` / `prose_only` /
`no_engagement` / `schema_confusion` / `context_overflow` / `no_diff_writes`
/ `aborted` / `""`. Comparison table prints counts per variant.

---

## Token-interval logging

`src/luxe/agents/loop.py:run_agent` prints a progress line every 5000
completion tokens (configurable via `LUXE_TOKEN_LOG_INTERVAL` env var; 0
disables). Captured in `acceptance/<output>/<variant>/<fixture>/stdout.log`.
Lets us see "engaged with steady tool calls" vs "burst-prosing without tools".

---

## Resume command — re-baseline against the 10-fixture suite

```bash
cd /Users/michaeltimpe/Downloads/luxe

# Confirm oMLX state
.venv/bin/python -c "
from luxe.backend import Backend
b = Backend(); listed = set(b.list_models())
m = 'Qwen3.6-35B-A3B-6bit'
print(f'  {\"✓\" if m in listed else \"✗\"} {m}')
"

# If ✗, restart: brew services restart omlx

python -m benchmarks.maintain_suite.run \
    --variants benchmarks/maintain_suite/variants_v1_default.yaml \
    --output acceptance/v1_default \
    --per-fixture-timeout 1800 \
    --all
```

1 variant × 10 fixtures = 10 runs, ~30-90 min wall (5 of the fixtures
are new; the model has not seen them before, so prompt-engineering
weakness will surface here).

---

## After-the-bench checklist

When the run finishes:

1. Read the printed comparison table — note PASS count and bailout types.
2. **Inspect each PASS PR by hand** with `gh pr diff --repo <owner/repo>
   <num>` — historical false-positive rate is ~30-50%, even with the
   tighter grader.
3. Run strict regrade: `python scripts/regrade_phase2.py --output
   acceptance/v1_default`.
4. Check if real-PASS count clears the v1 release gate (≥8/10).
5. If short of 8/10, identify the failing task types and either:
   - Tune the system prompt for `document` / `manage` tasks (champion's
     known weak categories)
   - Calibrate the new fixtures (`min_matches`/`min_added_lines` may need
     adjustment if they're too aggressive)
6. Document findings: append a new entry to `lessons.md` if a new failure
   pattern emerges.
7. Bump pyproject.toml to 1.0.0 once gate clears.

---

## Open work / next steps

In rough priority order:

1. **Run the 10-fixture re-baseline** (the resume command above). The
   5 new fixtures have not been tested; expect calibration on grader
   thresholds in the first run or two.
2. **Iterate on `document` / `manage` prompts** if those categories are
   where the gate falls short. The champion is strong on JS implements
   and weak on multi-section docs / audit writeups.
3. **Bump pyproject.toml from 1.0.0.dev0 to 1.0.0** when the gate clears.
4. **Drop Gemma-4-26B-A4B** entries from `model_settings.json` if they're
   still there — both 4-bit and 8-bit uploads from mlx-community don't
   have `tokenizer.chat_template`.
5. **Past v1**: expand fixtures to ≥15 with explicit task-type balance,
   and consider per-task-type prompt overlays.

---

## Critical gotchas (things that wasted time)

- **`oMLX` `idle_timeout: null` keeps models resident forever.** Set to
  `1800`.
- **`luxe maintain` post-run unload fires by default.** In bench mode use
  `--keep-loaded` (already passed by `_luxe_maintain` in `run.py`).
- **Path-style HF symlinks need oMLX restart.** Adding a model to
  `~/.omlx/models/` doesn't make it appear in `/v1/models` until restart.
- **`mlx-community` chat templates are inconsistent.** StarCoder2-3B,
  CodeGemma-2B, Gemma-4-26B-A4B all ship with empty `chat_template` and
  fail with HTTP 400 in the chat-driven loop.
- **Bench `wall=0s tokens=0` is a known telemetry bug** — single-mode
  events aren't always summed correctly into per-stage counts. Read
  `diagnostics.json` per fixture for ground truth.
- **`stuck_loop` doesn't always mean failure** — qwen3.6-35b-a3b often
  ships a real diff then trips the stuck-loop detector on cleanup. The
  `stuck_after_done` vs `stuck_no_output` split resolves the column.

---

## When in doubt

`git log --oneline -20` tells the recent trajectory. `lessons.md` has
postmortems of every failure pattern and architectural decision. The
user prefers terse, action-oriented responses — don't summarize what
they can read; tell them the next step.

The user is comfortable with auto mode but draws hard lines on
destructive shared-system actions (oMLX config, sudo, force-push, deletes
outside their workspace). When in doubt, write the change but ask before
applying.
