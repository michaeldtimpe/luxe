# Swarm

Specialist swarm pipeline for multi-model code task orchestration. A proof-of-concept that tests whether decomposing code tasks across right-sized local models outperforms single-model approaches.

## Why This Exists

The [luxe](https://github.com/michaeldtimpe/luxe) CLI runs code review and refactor tasks on a single Qwen2.5-32B model. When a task generates 28+ tool calls, the model accumulates 60k–376k tokens against a 32k context window. Ollama silently truncates, causing re-reads and fabricated citations. A 40-minute review producing 58 unverified citations wastes more time than a pipeline of focused agents that each take 2 minutes.

Swarm tests the hypothesis: **a pipeline of role-scoped specialists using smaller, faster models avoids context exhaustion and produces higher-quality results.**

## Model Roster

All models run locally via oMLX on Apple Silicon (64 GB target).

| Role | Model | Size | Context | Throughput |
|------|-------|------|---------|------------|
| Architect | Qwen2.5-7B-Instruct-4bit | 5 GB | 8k | ~37 tok/s |
| Worker (reads) | DeepSeek-Coder-V2-Lite-4bit | 8.8 GB | 16k | ~45 tok/s |
| Worker (code) | Qwen2.5-Coder-14B-4bit | 8 GB | 32k | ~22 tok/s |
| Worker (analyze) | Qwen2.5-Coder-14B-4bit | 8 GB | 32k | ~22 tok/s |
| Validator | Qwen2.5-7B-Instruct-4bit | 5 GB | 8k | ~37 tok/s |
| Synthesizer | Qwen2.5-32B-Instruct-4bit | 19 GB | 32k | ~10 tok/s |

Pipeline stages are sequential — only one model loaded at a time. oMLX handles model swapping.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Python 3.11+ and a running [oMLX](https://github.com/nicholasgasior/omlx) server on `localhost:8000`.

## Quick Start

```bash
# Verify oMLX is running and models are available
swarm check

# Review a local repository
swarm run /path/to/repo "review for security issues and code quality" --type review

# Review a GitHub repo (auto-clones)
swarm run https://github.com/user/repo "find bugs and security vulnerabilities" --type review

# Implement a feature
swarm run /path/to/repo "add input validation to the user API endpoints" --type implement

# Bug investigation and fix
swarm run /path/to/repo "investigate the race condition in session handling" --type bugfix

# Generate documentation
swarm run /path/to/repo "update README and add docstrings to public API" --type document

# Summarize a codebase
swarm run /path/to/repo "summarize the architecture and key modules" --type summarize

# Repository management
swarm run /path/to/repo "audit dependencies and update CI config" --type manage

# Save report and metrics
swarm run /path/to/repo "full security audit" --type review --save-report --output ./runs

# Compare multiple runs
swarm compare ./runs
```

## Task Types

Each task type configures a different pipeline shape — only the stages needed for that kind of work.

| Type | Pipeline | Use Case |
|------|----------|----------|
| `review` | Architect → Read + Analyze → Validator → Synthesizer | Code review, security audit, quality check |
| `implement` | Architect → Read → Code → Validator → Synthesizer | Feature development, adding functionality |
| `bugfix` | Architect → Read → Analyze → Code → Validator → Synthesizer | Bug investigation and patching |
| `document` | Architect → Read → Code → Validator → Synthesizer | Documentation generation and updates |
| `summarize` | Architect → Read → Synthesizer | Codebase analysis and overview |
| `manage` | Architect → Read → Analyze → Code → Validator → Synthesizer | Dependency updates, config cleanup, CI fixes |

## Pipeline Architecture

```
                ┌─────────────┐
                │  Architect   │  Decomposes goal into 4–12 micro-objectives
                │    (7B)      │  with role tags and scope hints
                └──────┬───────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
  ┌───────────┐  ┌───────────┐  ┌───────────┐
  │ Worker     │  │ Worker     │  │ Worker     │  Role-specific tool surfaces
  │ (read)     │  │ (analyze)  │  │ (code)     │  Fresh context per task
  │ 2.4B       │  │ 14B        │  │ 14B        │  Cached tool results
  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘
        └──────────────┬──────────────┘
                       ▼
                ┌─────────────┐
                │  Validator   │  Checks file:line citations
                │    (7B)      │  Removes unverified findings
                └──────┬───────┘
                       ▼
                ┌─────────────┐
                │ Synthesizer  │  Severity-grouped final report
                │   (32B)      │  No tools — pure synthesis
                └──────────────┘
```

## Configuration

Pipeline behavior is driven by `configs/pipeline.yaml`. Key sections:

- **models** — Model ID mapping per role
- **roles** — Per-role config: context window, max steps, temperature, tool allowlist
- **task_types** — Pipeline shape and architect prompt per task type
- **escalation** — Fallback chain when a worker fails (read → analyze → code)

## Metrics

Every run produces structured metrics saved to `./runs/run_<id>.json`:

- **Wall time** per stage and total
- **Token usage** (prompt + completion) per role
- **Throughput** (tok/s) per model
- **Context pressure** peak per subtask
- **Tool calls** count, cache hit rate
- **Escalations** and blocked subtasks

Use `swarm compare ./runs` to see side-by-side comparisons across runs.

## Testing

```bash
pytest tests/ -v
```

30 tests covering config loading, tool execution, context pressure, architect parsing, and pipeline data models. Tests run without a live oMLX server — they validate the framework logic, not model inference.

## Project Structure

```
swarm/
├── configs/pipeline.yaml           # Model roster, role configs, task types
├── src/swarm/
│   ├── backend.py                  # oMLX client (OpenAI-compatible API)
│   ├── config.py                   # Pydantic config loading + validation
│   ├── context.py                  # Token estimation, pressure monitoring, elision
│   ├── cli.py                      # Click CLI: run, check, compare
│   ├── tools/
│   │   ├── base.py                 # ToolDef, ToolCall, ToolCache, dispatch, validation
│   │   ├── fs.py                   # read_file, list_dir, glob, grep, write_file, edit_file
│   │   ├── git.py                  # git_diff, git_log, git_show
│   │   ├── analysis.py             # Language-gated: ruff, mypy, bandit, eslint, tsc, clippy
│   │   └── shell.py                # Allowlisted bash execution
│   ├── agents/
│   │   ├── loop.py                 # Shared agent loop with tool dispatch + telemetry
│   │   ├── architect.py            # Goal → micro-objectives with role tags
│   │   ├── worker.py               # 3 variants with role-specific tool surfaces
│   │   ├── validator.py            # Citation verification
│   │   └── synthesizer.py          # Final report assembly
│   ├── pipeline/
│   │   ├── model.py                # PipelineRun, Subtask, StageMetrics
│   │   └── orchestrator.py         # Full pipeline driver
│   └── metrics/
│       ├── collector.py            # Extract RunMetrics from completed runs
│       └── report.py               # Rich tables for summaries + comparisons
└── tests/                          # 30 tests
```

## Relationship to luxe

Swarm is a standalone testing ground. If the pipeline approach proves viable, the architecture ports back to luxe as a new dispatch mode alongside the existing single-model approach. The tool interfaces (`ToolDef`, `ToolFn`, `ToolCache`), agent loop, and config patterns were designed to mirror luxe's existing code so the integration path is straightforward.
