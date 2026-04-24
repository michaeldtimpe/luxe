# luxe

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
  - **`luxe/luxe/`** — a local, multi-agent Claude-Code-alike CLI. A
    small router picks one of nine specialists (general / lookup /
    research / writing / code / image / review / refactor / calc) and
    hands off. Runs on Ollama + llama.cpp + Draw Things, with a task
    orchestrator for multi-step goals that runs background subprocesses
    and stitches specialists together.

## Quick start (luxe CLI)

```bash
cd luxe
uv sync
bash daily_driver/install_luxe.sh
luxe                       # interactive REPL
```

Fuller walkthrough in **`luxe/luxe/README.md`**.

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
  `qwen2.5:32b-instruct` with a 10-tool static-analysis surface
  (`ruff`/`mypy`/`bandit`/`pip-audit`/`semgrep`/`gitleaks` for
  Python, `eslint`/`tsc`/`clippy`/`go vet` cross-language). Pre-
  flight repo survey sizes task wall + `num_ctx` per clone; a
  four-layer anti-fabrication check (shallow-retry → forced
  inspection → `file:line` citation verification → construct-
  presence verification) annotates suspect findings. `code` agent
  stays on `qwen2.5-coder:14b-instruct` with the same analyzer
  tools — see AGENTS.md for the per-agent breakdown.

## Hardware target

MacBook Pro with **64 GB unified memory**. Models up to ~40 GB (e.g.
`llama3.3:70b` quantized Q4_K_M) run but need care around context size;
see `LESSONS.md` for the specifics.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
