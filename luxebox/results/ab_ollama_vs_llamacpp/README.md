# A/B benchmark — Ollama vs `llama-server`

Decide whether luxe should swap its Ollama backend for `llama-server`
on the three Qwen2.5 models it currently runs there. Gemma 3 27B is
already on `llama-server` (Ollama refuses tools for Gemma) and is out
of scope.

## What gets compared

Three models, both backends, the same Q4_K_M weights:

| Candidate id | Ollama tag | GGUF (llama-server) |
|---|---|---|
| `qwen2.5-7b-instruct` | `qwen2.5:7b-instruct` | `bartowski/Qwen2.5-7B-Instruct-GGUF` · `Q4_K_M` |
| `qwen2.5-coder-14b` | `qwen2.5-coder:14b-instruct` | `bartowski/Qwen2.5-Coder-14B-Instruct-GGUF` · `Q4_K_M` |
| `qwen2.5-32b-instruct` | `qwen2.5:32b-instruct` | `bartowski/Qwen2.5-32B-Instruct-GGUF` · `Q4_K_M` |

Four benchmarks per (model, backend) cell:

- **`decode_throughput`** — three fixed prompts (~50 / ~300 / ~4000
  input tokens) each asking for ~1024 tokens of continuation. Pure
  TTFT / decode tok/s / prefill rate / peak RSS signal.
- **`bfcl_v3`** — single-turn function-call accuracy via `bfcl-eval`.
  Tool-use reliability is what luxe lives or dies on. **Not in the
  default --bench list** because `bfcl-eval` pins `tree-sitter==0.21.3`
  which conflicts with `evalplus` — it lives in a separate venv. See
  "Running BFCL" below.
- **`humaneval_plus`** — Python correctness. Both backends serve the
  same weights, so quality should match within noise; surfacing it
  also flags any tokenizer/template divergence.
- **`luxe_replay`** — replays a recorded luxe session JSONL through
  both backends turn-by-turn. Most realistic; signal lives in
  per-turn TTFT for prefill-heavy mid-session turns. **Requires
  fixtures dropped into `replay_inputs/` — see below.**

## Prereqs

- Ollama running at `127.0.0.1:11434` (handled by the existing
  launchd plist).
- Each model already pulled into Ollama: `ollama pull qwen2.5:7b-instruct`
  etc. The runner preloads each model on first request, but it can't
  pull them for you.
- llama.cpp installed (`brew install llama.cpp`) for `llama-server`.
  The first llama-server launch will download the relevant GGUF from
  HuggingFace (~5–20 GB per model).
- `uv sync` in the luxebox repo root.

## Running

Full sweep — every candidate, every benchmark, both backends. Plan on
several hours; the 32B model is the long pole:

```
uv run python scripts/run_ab_benchmark.py
```

Smoke test (one model, one bench, 3 tasks, both backends — verifies
the wiring end-to-end without paying real benchmark time):

```
uv run python scripts/run_ab_full.py \
  --candidate qwen2.5-coder-14b \
  --bench decode_throughput \
  --limit 3 -y
```

Note: the **first** time you run this for a new candidate, llama-server
needs to download the GGUF (~5–20 GB). Either let `run_ab_full.py`
prefetch in Phase 1 (default), or accept a multi-minute "waiting for
server on :PORT" pause on the first call.

Just the perf microbench (skip code-correctness and replay):

```
uv run python scripts/run_ab_benchmark.py --bench decode_throughput
```

Re-render the report from existing JSONL without re-running:

```
uv run python scripts/run_ab_benchmark.py --report-only
```

## Running BFCL (separate venv)

`bfcl-eval` can't share luxebox's main venv (tree-sitter version
clash with `evalplus`). One-time setup:

```
cd luxebox
uv venv .venv-bfcl --python 3.11
.venv-bfcl/bin/pip install -e .
.venv-bfcl/bin/pip uninstall -y evalplus       # avoid the conflict
.venv-bfcl/bin/pip install bfcl-eval
```

Then run the BFCL bench from that venv:

```
.venv-bfcl/bin/python scripts/run_ab_benchmark.py \
  --candidate qwen2.5-coder-14b \
  --bench bfcl_v3 \
  --limit 50
```

The JSONL it writes lands in the same `results/runs/...` tree, so the
final report stitches BFCL numbers in alongside everything else when
you next run `--report-only`.

## Outputs

```
results/ab_ollama_vs_llamacpp/
├── REPORT.md          # side-by-side table, per-model verdict
├── REPORT.csv         # machine-readable
├── replay_inputs/     # drop your luxe session JSONLs here
└── runs/<phase>/<candidate>/<config>/<bench>.jsonl
                       # raw per-task records, resumable
```

The runner is **resumable**: if you Ctrl-C mid-sweep, re-running the
same command picks up where it left off (one record per task in the
JSONL is the checkpoint).

## Adding replay fixtures

The `luxe_replay` benchmark needs sanitized session files. To prepare
one:

1. Pick a representative session from `~/.luxe/sessions/`.
2. Copy it into `results/ab_ollama_vs_llamacpp/replay_inputs/` with a
   descriptive name (e.g. `router_short.jsonl`, `code_midlength.jsonl`,
   `research_multi_fetch.jsonl`).
3. Scrub anything you don't want re-sent to a model: `sed`-edit user
   turns that contain personal data, or just delete them; the replay
   walks the file top-down so removing turns is safe.

Three fixtures (router-only short, /code mid-length, /research with
fetches) is what the plan recommends; add more if you want broader
coverage.

## What "winning" looks like

The report's per-candidate verdict is a one-liner. The decision rule:

- **`llama-server` wins on both TTFT and decode tok/s** for all three
  models, by ≥10%, with no quality regression on humaneval_plus or
  bfcl_v3 → migrate luxe to `llama-server`.
- **Mixed results** (one model improves, another regresses; or speed
  improves but tool-call success drops on bfcl_v3) → stay on Ollama,
  document the tradeoff in `LESSONS.md`.
- **Ollama wins or ties** → stay on Ollama, the migration cost isn't
  justified.

## Known caveats

- **Q4_K_M parity is trusted, not bit-exact**: Ollama's bundled GGUF
  for `qwen2.5:7b-instruct` and the HF Bartowski quant aren't
  guaranteed to be the same blob. Both are Q4_K_M; weight differences
  shouldn't materially affect perf numbers but could nudge quality
  scores by a fraction of a percent. The token-parity guard in the
  runner catches anything that would invalidate a tok/s comparison.
- **Cold load amortization**: each (model × backend) combination
  loads weights once, then runs every requested benchmark. Switching
  models still pays a ~30–60s cold load.
- **Ollama's keep-alive**: the runner doesn't unload models from
  Ollama between benchmarks. Ollama's own 5-minute keep-alive will
  evict eventually.
- **Peak RSS for Ollama** is sampled from whatever process owns the
  listening socket — usually the parent `ollama` server. If Ollama
  spawns a separate runner subprocess for the model, the parent's
  RSS may underestimate true memory use; cross-check with `/context`
  in the luxe REPL or `htop`.
