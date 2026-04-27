# luxe

Evaluation + optimization harness for local coding LLMs on Apple Silicon.
Plan: `~/.claude/plans/linked-conjuring-fiddle.md`.

## What's here

- `harness/` — backends (MLX / llama.cpp OpenAI-compat clients), server
  lifecycle, candidate registry, metrics, JSONL IO, report generator, CLI
- `benchmarks/` — Phase A runners (HumanEval+, MBPP+, MultiPL-E Rust/Go,
  LiveCodeBench, BFCL v3, τ-bench + SWE-bench Lite skeletons) and
  `compression_repo.py` (context-compression sweep — see Compression
  benchmark below)
- `strategies/` — composable retrieval/compression pipelines for the
  compression benchmark (preprocess / index / retrieve / compress /
  prompt_assembly stages, JSON-configured per strategy)
- `fixtures/compression_repos/`, `fixtures/compression_tasks/` — small
  bug-laden Python fixture + per-task JSONs used by the compression
  benchmark
- `shared/trace_hints.py` — tiny `path.py:LINE` / `File "…", line N`
  parser used by both the `stack_trace_guided` benchmark strategy and
  luxe's orchestrator pre-retrieval
- `personal_eval/` — Phase B review (B1) and write (B2) replay on your PRs;
  self-contained mini agent loop with scoped tool surface
- `luxe_cli/` — **local multi-agent Claude-Code-alike CLI** (router + general,
  research, writing, image, code specialists). See [luxe_luxe_cli/README.md](luxe_luxe_cli/README.md).
- `configs/` — candidate + optimization YAML + `agents.yaml` for luxe
- `daily_driver/` — mlx-lm + ollama launcher scripts, Aider / OpenCode /
  LiteLLM configs, `install_luxe.sh`
- `scripts/` — phase entry points, `run_luxe_eval.py`,
  `run_compression_bench.py` + `analyze_compression_sweep.py`
- `results/` — JSONL per run; reports land here (including `luxe_eval/`
  and `runs/compression_strategies/`)

## Prereqs (user-installed)

Check each before running the phases. The smoke test below doesn't need any of
these — it uses a mock backend.

| Tool | Why | Install |
|---|---|---|
| **uv** | Python env | `brew install uv` |
| **mlx-lm** | Primary backend | `uv sync --extra mlx` |
| **huggingface-cli** | Downloads weights | comes with `huggingface-hub` |
| **gh** | PR ingestion (Phase B) | `brew install gh && gh auth login` |
| **rustc** | MultiPL-E Rust grader | `brew install rustup-init && rustup-init -y` |
| **go** | MultiPL-E Go grader | `brew install go` |
| **llama.cpp** | Optional fallback | `brew install llama.cpp` |
| **aider** | Daily driver (recommended) | `uvx aider` (no install needed) |

Disk: each 32B candidate at 4-bit is ~18 GB. Running all 6 candidates through
Phase A ≈ **120 GB** of model downloads. Keep `models/` on an external drive if
needed — override `HF_HOME` before downloading.

## Smoke test (runs now, no model needed)

```bash
python3 -m venv .venv
.venv/bin/pip install pydantic httpx pyyaml rich typer tenacity psutil jinja2 tqdm
.venv/bin/python scripts/smoke_test.py
```

Passes when you see `✓ smoke test passed` and a Phase A screening table.

## Real run order (phased, with approval points)

Each phase is a separate entry point so you can pause, inspect JSONL, and
decide whether to continue. Rough time and disk budgets are noted.

### Phase 0 — environment

```bash
uv sync --extra mlx --extra evalplus
huggingface-cli login           # for gated models (Codestral/Devstral)
uv run python scripts/smoke_test.py
```

### Phase A — canned screening (6 candidates × ~5 benchmarks)

Expect ~4–6 h per candidate × 6 = **~30 h** of compute, **~120 GB** downloads.
Run per candidate and inspect before moving on.

```bash
# Quick-pass first: 20 tasks per bench to verify every candidate runs
uv run python scripts/run_phase_a.py --limit 20

# Full run — one candidate at a time is safer
uv run python scripts/run_phase_a.py --candidate qwen2.5-coder-14b
uv run python scripts/run_phase_a.py --candidate deepseek-coder-v2-lite
# ...etc

# After each: aggregate
uv run lux report-phase-a
```

Exit criteria: pick top 2 by `code pass@1 × tool-call success rate`.

### Phase B — personal repo finalists

Each `--repo` takes `path:language`. The ingester uses `gh` to list merged
PRs, filters to small/medium diffs with reviewer comments, and caches the
corpus under `personal_eval/corpus/<repo>.json`.

```bash
uv run python scripts/run_phase_b.py \
    --candidate qwen2.5-coder-32b \
    --repo ~/code/my-rust-proj:rust \
    --repo ~/code/my-py-proj:python \
    --repo ~/code/my-go-proj:go \
    --per-lang 5

# Repeat with the 2nd finalist, then:
uv run lux report-phase-b
```

Exit criteria: winner has the best combined B1 F1 + B2 test-pass rate.

### Phase C — optimization configs on the winner

```bash
uv run python scripts/run_phase_c.py --winner qwen2.5-coder-32b
```

This re-runs the Phase A suite against 4 optimized configs (`kv-q8`,
`spec-only`, `prompt-cache-only`, `all-on`). Baseline is assumed done from
Phase A (pass `--run-baseline` to force).

### Phase D — verdict

```bash
uv run python scripts/run_phase_d.py --winner qwen2.5-coder-32b
```

Emits `results/phase_d.md` with per-config deltas and the acceptance-gate
verdict. Gate checks (all must pass):

- Every benchmark regresses by ≤ 2 absolute points
- Tool-call success regresses by ≤ 1.5 absolute points
- Phase B write-replay passes within 1 task of baseline
- No single benchmark drops by > 5 points (hard floor)

### Daily driver

Start the winner's server, then pick a client. See `daily_driver/README.md`
for the full Aider / OpenCode / Claude-Code-via-LiteLLM breakdown.

```bash
bash daily_driver/start_mlx_server.sh       # terminal 1
cd ~/your-repo && uvx aider --config ~/Downloads/luxe/daily_driver/aider.conf.yml
```

## luxe — local multi-agent CLI

Separate from the evaluation harness above, `luxe_cli/` is a self-contained
Claude-Code-alike CLI that runs entirely on Ollama + Draw Things. A small
router picks one of five specialists (general / research / writing / image
/ code) and hands off. Full docs, prereqs, and model picks:

- [luxe_luxe_cli/README.md](luxe_luxe_cli/README.md) — usage + per-agent model selections
- Install: `bash daily_driver/install_luxe.sh`
- Uninstall: `bash daily_driver/install_luxe.sh --uninstall`

## Compression benchmark

Separate phase that measures how retrieval/compression strategies affect
local coder models on repo-scoped bugfix tasks. Strategies are JSON
pipelines of 5 stages (preprocess / index / retrieve / compress /
prompt_assembly); tasks are per-repo JSONs that name a failing pytest
command. Runs land under `results/runs/compression_strategies/`.

```bash
# Smoke (single strategy, single task, 14B)
uv run python scripts/run_compression_bench.py \
    --candidate qwen2.5-coder-14b \
    --strategy baseline_whole_file \
    --limit 1

# Full sweep: 7 strategies × 14B + 32B × 5 tasks, context pressure at 4k
uv run python scripts/run_compression_bench.py \
    --candidate qwen2.5-coder-14b,qwen2.5-coder-32b \
    --strategy retrieve_none_wf,retrieve_oracle_whole_file,baseline_whole_file,retrieve_full_wf,stack_trace_guided_wf,file_outline_only_wf,retrieve_then_summarize_wf \
    --num-ctx 4096

# Aggregate
uv run python scripts/analyze_compression_sweep.py --show-errors
```

Headline result from the April 2026 sweep (see `LESSONS.md` for the
writeup): **selectivity-based retrieval is the only compression that
works on local coder models** — oracle retrieval gets 90% pass at ~7×
fewer prompt tokens than dumping the whole repo, while LLM
summarization and AST outlining regress pass rate by 50-70 pct.

## Known gaps (intentional)

- **τ-bench** and **SWE-bench Lite** are implemented as skeletons. Full
  integration requires cloning their upstream repos and (for SWE-bench) a
  Docker-based evaluator. Replace `_load_upstream` in `tau_bench.py` and run
  the SWE-bench harness against the patches we emit to
  `results/swebench_lite_patches/predictions.jsonl`.
- **Qwen3-Coder-32B** is in the registry marked `active: true` but the HF repo
  path is speculative. If the model isn't live on HF when you start Phase A,
  set `active: false` in `configs/candidates.yaml`.
- **QLoRA on your codebase** is explicitly out of scope for the first pass
  (aggressive tier). Revisit if the balanced optimization passes the gate but
  leaves quality on the table against your repos.
