# luxe — session resume document

Snapshot for picking up cross-session. Everything here is true as of the last
commit (`03b8989` — mono+bench: promote Qwen3.6-35B-A3B-6bit + split stuck_loop bailout).

---

## 30-second orientation

**luxe** is an MLX-only repo maintainer for Apple Silicon (oMLX backend on
`localhost:8000`). Takes a goal + repo, opens a PR. Four execution modes:
`single`, `swarm`, `micro`, `phased`. Bench harness in
`benchmarks/maintain_suite/run.py` drives `luxe maintain` per fixture.

**Current champion**: `Qwen3.6-35B-A3B-6bit` (set as default monolith in
`configs/single_64gb.yaml`). Beat every prior model/architecture combo in
the overnight MoE bake-off — first model to produce production-quality
implementations of `the-game` (real React keydown handler) and `neon-rain`
(Shift+R via existing event bus, npm test passes naturally).

**In flight**: 3-cell 8-bit completion bake-off — last bake-off planned for
this hardware tier. See "Resume command" below.

---

## The bench-as-truth pattern

Every model claim in this project goes through:

1. Run `python -m benchmarks.maintain_suite.run --variants <yaml>` against
   the 5 v1.0 fixtures.
2. Read the printed Mode×Model comparison table — `pass/fail/wall/tokens/
   bailouts` per cell.
3. **Inspect every PASS PR by hand** via `gh pr diff` — bench grading has
   been gamed in 4 distinct ways (placeholder text, role-name leaks,
   destructive deletions, vacuous tests, regex matching pre-existing
   content, orphan files that pass tests because they don't break
   anything).
4. Strict regrade with `scripts/regrade_phase2.py` (works on any output dir,
   not just phase2) for an automated sanity check on diff shape.

The bench's "real PASS" count is always ≤ the printed count. Every
historical bake-off has had at least one false-positive PASS.

---

## Files of consequence

| Path | Purpose |
|---|---|
| `src/luxe/agents/microloop.py` | micro mode runner — draft/verify per micro-step with blackboard |
| `src/luxe/agents/phased.py` | phased mode runner — chief_architect + worker_code with bounded retry |
| `src/luxe/tools/fs.py` | write-time honesty guards (placeholder/role-name/mass-deletion) |
| `src/luxe/backend.py` | `Backend.unload_model()`, `unload_all_loaded()`, `loaded_models()` |
| `src/luxe/cli.py` | `luxe maintain --mode {auto,single,swarm,micro,phased}` + `luxe unload` + `--keep-loaded` |
| `benchmarks/maintain_suite/run.py` | bench harness; multi-variant; `_classify_bailout` here |
| `benchmarks/maintain_suite/grade.py` | grading + strict gates (destructive_diff, role_name_leak, placeholder_diff, vacuous_test) + diff-aware `_check_regex_present` |
| `configs/single_64gb.yaml` | default mono config — currently `Qwen3.6-35B-A3B-6bit` |
| `configs/swarm_64gb.yaml` | default swarm/micro/phased config |
| `scripts/bench_small_models.py` | small-model bake-off w/ warmup probe |
| `scripts/register_omlx_models.py` | symlink HF cache → `~/.omlx/models/` |
| `scripts/regrade_phase2.py` | apply strict gates to existing results |
| `scripts/generate_phase2_variants.py` | derive Phase 2 variants from Phase 1 winner |
| `benchmarks/maintain_suite/variants_*.yaml` | per-bake-off variant matrices |
| `lessons.md` | 4 documented lessons from Phases 1-3 |

---

## oMLX configuration (must-restart-to-apply)

`~/.omlx/settings.json`:
```json
"max_model_memory": "48GB",                     // bumped from 36GB for the 32B/35B 8-bit
"idle_timeout": { "idle_timeout_seconds": 1800 } // 30 min — too tight at 5 min caused mid-run evictions in phased
```

System-level Metal wired ceiling (set via sysctl):
```bash
sudo sysctl iogpu.wired_limit_mb=49152
echo "iogpu.wired_limit_mb=49152" | sudo tee -a /etc/sysctl.conf  # persistent
```

`~/.omlx/model_settings.json` has explicit per-model entries for everything
the project uses (no `auto` defaults). Add new entries when registering new
models — `register_omlx_models.py` handles symlinks but not the settings.

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
| 8-bit completion (in flight) | 04-30 | qwen3.6-35B-A3B-8bit, qwen-coder-32B-8bit, qwen3-coder-30B-A3B-8bit | TBD |

Full results: `acceptance/<phase>/comparison.json` per phase dir.

---

## Strict gates currently in place

Tool-side (`src/luxe/tools/fs.py` — block at write time):
- `_check_placeholder_text` — refuses `<paste...here>`, `// Your X code here`,
  `// TODO: implement Y`, multi-word variants
- `_check_role_path` — fuzzy match on tokenized path components against
  `worker_*`, `drafter`, `verifier`, `architect`, etc.
- `_check_mass_deletion` — refuses overwriting ≥50-line file with ≤5-line
  stub when deletion ratio ≥10×

Post-PR strict gates (`benchmarks/maintain_suite/grade.py`):
- `destructive_diff` (deletions ≥30 + ratio ≥5×)
- `role_name_leak` (same fuzzy match as tool guard)
- `placeholder_diff` (regex match in added lines)
- `vacuous_test` (new test file passes against `base_sha` worktree)

Diff-aware grading: `_check_regex_present` now reads `git diff <base> HEAD`
and scans only `+` lines. Closes the loophole where a model touched a file
that already contained the pattern.

Bailout categorization: `_classify_bailout` in `run.py:626` produces
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

## Resume command — kicks off the in-flight 8-bit completion bake-off

```bash
cd /Users/michaeltimpe/Downloads/luxe

# Confirm oMLX state
.venv/bin/python -c "
from luxe.backend import Backend
b = Backend(); listed = set(b.list_models())
for m in ['Qwen3.6-35B-A3B-8bit', 'Qwen2.5-Coder-32B-Instruct-8bit', 'Qwen3-Coder-30B-A3B-Instruct-8bit']:
    print(f'  {\"✓\" if m in listed else \"✗\"} {m}')
"

# If anything shows ✗, restart: brew services restart omlx

python -m benchmarks.maintain_suite.run \
    --variants benchmarks/maintain_suite/variants_8bit_completion.yaml \
    --output acceptance/8bit_completion \
    --per-fixture-timeout 1800 \
    --all
```

3 cells × 5 fixtures = 15 runs, ~3-5h wall.

---

## After-the-bench checklist

When the bake-off finishes:

1. Read the printed comparison table — note which variants pass and which
   bail out (and what type of bailout).
2. **Inspect each PASS PR by hand** with `gh pr diff --repo <owner/repo>
   <num>` — historical false-positive rate is ~30-50%.
3. Run strict regrade: `python scripts/regrade_phase2.py --output
   acceptance/8bit_completion`.
4. Document findings: append a new entry to `lessons.md` if a new failure
   pattern emerges.
5. Update `configs/single_64gb.yaml` if any variant beats the current
   champion (Qwen3.6-35B-A3B-6bit) on real-PASS count.
6. Commit findings (config promotion if any) with the same bench: prefix.

---

## Open work / next steps after this bake-off

In rough priority order:

1. **Identify the fallback model** for Qwen3.6-35B-A3B-6bit's bimodal
   bailout case (when it produces ~220 chars of prose with 0 tool calls
   on JS/UI tasks). The 8-bit completion bake-off should reveal which
   8-bit variant has the most consistent tool-call engagement.
2. **Wire fallback model logic** in `cli.py:maintain` — if mono produces
   `prose_only` bailout (detectable from `single_mode_done` event), retry
   with the fallback model before giving up.
3. **Drop Gemma-4-26B-A4B** from the roster — both 4-bit and 8-bit
   uploads from mlx-community don't have `tokenizer.chat_template`. Same
   issue as StarCoder2/CodeGemma earlier.
4. **The 5 v1.0 fixtures are repeatable but small.** If the project goes
   past v1.0, expand `benchmarks/maintain_suite/fixtures.yaml` to
   ≥10 fixtures with proper task-type coverage.
5. **Phase 3 (phased mode) is 0/5 real.** Either pivot it (separate plan
   prompt, non-trivial review tooling) or shelve. The architecture works
   end-to-end but the architect's review is too lenient even with the
   tightened prompts (commit `201c92a`).

---

## Critical gotchas (things that wasted time)

- **`oMLX` `idle_timeout: null` keeps models resident forever.** Set to
  `300` initially; bumped to `1800` after phased's mid-run evictions.
- **`luxe maintain` post-run unload fires by default.** In bench mode this
  killed model warmth between fixtures — use `--keep-loaded` (already
  passed by `_luxe_maintain` in `run.py`).
- **Path-style HF symlinks need oMLX restart.** Adding a model to
  `~/.omlx/models/` doesn't make it appear in `/v1/models` until restart.
- **`mlx-community` chat templates are inconsistent.** StarCoder2-3B,
  CodeGemma-2B, Gemma-4-26B-A4B all ship with empty `chat_template` and
  fail with HTTP 400 in the chat-driven loop.
- **DeepSeek-Coder-1.3B-instruct-mlx** uses legacy `weights.NN.safetensors`
  naming; symlink alias `model.safetensors → weights.00.safetensors`
  unblocks it.
- **Bench `wall=0s tokens=0` is a known telemetry bug** — single-mode
  events aren't summed correctly into per-stage counts. Don't trust the
  comparison table's `avg_wall` / `avg_tok` columns for non-mono runs;
  read `diagnostics.json` per fixture for ground truth.
- **`stuck_loop` doesn't always mean failure** — qwen3.6-35b-a3b often
  ships a real diff then trips the stuck-loop detector on cleanup. The
  newer `stuck_after_done` vs `stuck_no_output` split (commit `03b8989`)
  resolves the misleading column.

---

## Test posture

`pytest tests/` should pass 324 tests. If it doesn't, something regressed.
Particularly important guard tests live in `tests/test_tools.py` — they
catch attempted evasions of the placeholder/role-leak/mass-deletion
guards (the bake-offs found new evasions after the initial implementation).

---

## When in doubt

`git log --oneline -20` tells the recent trajectory. `lessons.md` has
postmortems of failure patterns. The user prefers terse, action-oriented
responses — don't summarize what they can read; tell them the next step.

The user is comfortable with auto mode but draws hard lines on
destructive shared-system actions (oMLX config, sudo, force-push, deletes
outside their workspace). When in doubt, write the change but ask before
applying.
