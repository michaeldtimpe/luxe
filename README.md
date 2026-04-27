# luxe

[![luxe-tests](https://github.com/michaeldtimpe/luxe/actions/workflows/luxe-tests.yml/badge.svg)](https://github.com/michaeldtimpe/luxe/actions/workflows/luxe-tests.yml)

Two companion projects that run large-language-model workloads entirely on
a MacBook Pro — no cloud, no API keys (beyond what you choose to use), no
outbound calls except the web searches you explicitly allow.

> **New here?** [LESSONS.md](LESSONS.md) captures the hard-won findings
> behind the choices below — model selection, tool-use quirks per agent,
> prompt-cache vs. context-size trade-offs, Ollama vs. llama-server A/B
> results. Start there if you want the *why* before the *what*.

## Contents

- **`luxe/`** — the core workspace.
  - **`luxe/harness/`** — Apple-Silicon-friendly evaluation + optimization
    harness for local coding LLMs. OpenAI-compat backends (MLX /
    llama.cpp), candidate registry, benchmark runners, metrics.
  - **`luxe/luxe_cli/`** — a local, multi-agent Claude-Code-alike CLI. A
    small router picks one of nine specialists (general / lookup /
    research / writing / code / image / review / refactor / calc) and
    hands off. Runs on Ollama + oMLX + LM Studio + llama.cpp + Draw
    Things via a per-agent provider config, with a task orchestrator
    for multi-step goals that runs background subprocesses
    and stitches specialists together.

## Quick start (luxe CLI)

```bash
cd luxe
uv sync
bash daily_driver/install_luxe.sh
luxe                       # interactive REPL
```

Fuller walkthrough in **`luxe/luxe_luxe_cli/README.md`**.

## Quick start (luxe evaluation harness)

```bash
cd luxe
uv sync --extra mlx --extra evalplus
uv run python scripts/smoke_test.py     # no weights needed
uv run python scripts/run_phase_a.py --limit 20
```

Fuller walkthrough in **`luxe/README.md`**.

## Docs

- [README.md](README.md) — this file
- [ARCHITECTURE.md](ARCHITECTURE.md) — cross-cutting system design
- [AGENTS.md](AGENTS.md) — per-specialist details (models, prompts, tools)
- [LESSONS.md](LESSONS.md) — what I learned building this

## Status

- **luxe harness**: Phase A–D runners, daily-driver launchd plists,
  `lux` CLI. Tested with MLX + Ollama; llama.cpp supported.
- **luxe CLI**: router + 9 specialists live. Task orchestrator with
  background execution, clarifying questions, plan-preview, and
  context forwarding between subtasks. Writing agent served via
  `llama-server` for Gemma 3 27B with native tool-call support.
  Deterministic keyword/regex pre-router short-circuits the LLM router
  on decisive prompts (path + code verb → `code`, draft + essay →
  `writing`, etc.). `/tasks resume <id>` re-runs blocked/skipped
  subtasks without redoing completed ones; `/tasks tail <id> -v` adds
  per-tool-call lines to the live event stream.
- **Code intelligence**: `/review` and `/refactor` run on
  `Qwen3-30B-A3B-Instruct-2507-4bit` (MoE, 3B active per token —
  swapped 2026-04-27 from Qwen2.5-32B after a real `/review` ran in
  9 min vs 57 min with much less fabrication; see LESSONS.md). The
  same MoE was tried on `/research` and `/calc` and reverted same-
  day — those agents need aggressive tool use, which the MoE Instruct
  silently skips. Static-analysis surface is unchanged: 10 tools
  (`ruff`/`mypy`/`bandit`/`pip-audit`/`semgrep`/`gitleaks` for
  Python, `eslint`/`tsc`/`clippy`/`go vet` cross-language).
  Pre-flight repo survey sizes the task wall per clone (`num_ctx` is
  fixed per agent in `configs/agents.yaml`); a four-layer
  anti-fabrication check (shallow-retry → forced
  inspection → `file:line` citation verification → construct-
  presence verification) annotates suspect findings. `code` agent
  on `Qwen2.5-Coder-14B-Instruct-MLX-4bit` with the same analyzer
  tools — see AGENTS.md for the per-agent breakdown.
- **Backend split**: as of 2026-04-27 every agent serves through
  **oMLX** (port 8000, MLX-format weights). Initial migration
  2026-04-24 moved `code` / `review` / `refactor` after a sweep
  showed +50–60% decode tok/s vs Ollama at parity-or-better
  HumanEval+ pass rate; the rest followed three days later. `writing`
  uses Gemma 3 27B served via oMLX (still requires the
  `tool_code` prelude in the writing agent prompt — Gemma's chat
  template doesn't render the OpenAI `tools=` parameter). See
  `LESSONS.md` for the measurement methodology — including the
  premature-rollback episode and the 2026-04-27 asymmetric-MoE
  finding (Qwen3-30B-A3B Instruct wins read-and-reason agents,
  breaks tool-required ones).
- **Browser tool**: `research` and `lookup` agents can drive a real
  headless Chrome via `browse_navigate` + `browse_read` (CDP, allowlist-
  gated). Unblocks JS-rendered content where static `fetch_url` returns
  empty. Read-only by design; `LUXE_BROWSER_ALLOWLIST` env var
  overrides the default starter set. See `luxe/luxe_luxe_cli/README.md`.
- **Bench-history metrics**: `bench_orchestrator.py` records per-task
  `reads_per_edit` and `tool_loop_ratio`, plus a sliding-window
  `composite_health` z-score that flags rows with `⚠ INFLECTION` when a
  run diverges from the trailing 10-row baseline. Captures behavioral
  drift the wall-time / token-count metrics can't see.

## Hardware target

MacBook Pro with **64 GB unified memory**. Models up to ~40 GB (e.g.
`llama3.3:70b` quantized Q4_K_M) run but need care around context size;
see `LESSONS.md` for the specifics.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
