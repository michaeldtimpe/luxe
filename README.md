# local-llm

Two companion projects that run large-language-model workloads entirely on
a MacBook Pro — no cloud, no API keys (beyond what you choose to use), no
outbound calls except the web searches you explicitly allow.

## Contents

- **`luxebox/`** — the core workspace.
  - **`luxebox/harness/`** — Apple-Silicon-friendly evaluation + optimization
    harness for local coding LLMs. OpenAI-compat backends (MLX /
    llama.cpp), candidate registry, benchmark runners, metrics.
  - **`luxebox/luxe/`** — a local, multi-agent Claude-Code-alike CLI. A
    small router picks one of five specialists (general / research /
    writing / image / code) and hands off. Runs on Ollama + Draw Things.

## Quick start (luxe CLI)

```bash
cd luxebox
uv sync
bash daily_driver/install_luxe.sh
luxe                       # interactive REPL
```

Fuller walkthrough in **`luxebox/luxe/README.md`**.

## Quick start (luxebox evaluation harness)

```bash
cd luxebox
uv sync --extra mlx --extra evalplus
uv run python scripts/smoke_test.py     # no weights needed
uv run python scripts/run_phase_a.py --limit 20
```

Fuller walkthrough in **`luxebox/README.md`**.

## Docs

- [README.md](README.md) — this file
- [ARCHITECTURE.md](ARCHITECTURE.md) — cross-cutting system design
- [AGENTS.md](AGENTS.md) — per-specialist details (models, prompts, tools)
- [LESSONS.md](LESSONS.md) — what I learned building this

## Status

- **luxebox harness**: Phase A–D runners, daily-driver launchd plists,
  `lux` CLI. Tested with MLX + Ollama; llama.cpp supported.
- **luxe CLI**: all 9 build phases complete. Router + 5 specialists live.
  Code agent uses `qwen2.5-coder:14b-instruct` with known depth limits —
  see LESSONS.md.

## Hardware target

MacBook Pro with **64 GB unified memory**. Models up to ~40 GB (e.g.
`llama3.3:70b` quantized Q4_K_M) run but need care around context size;
see `LESSONS.md` for the specifics.

## License

Not yet licensed. Personal project; license TBD.
