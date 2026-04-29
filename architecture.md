# Architecture

> **Note:** This document describes the swarm pipeline's design principles
> and was carried over from the prototype era. It accurately covers the
> swarm half of luxe v1.0 but does not document the additions (single-mode,
> mode selection, citation linter, PR cycle, MCP, BM25/AST retrieval,
> per-stage checkpointing). The README is authoritative for the v1.0 surface;
> see also [`.claude/plans/linked-leaping-curry.md`](.claude/plans/linked-leaping-curry.md).
> Where this document says `swarm run`, read `luxe maintain --mode swarm`;
> where it says `src/swarm/`, read `src/luxe/`.

## Design Thesis

A specialist swarm replaces one general-purpose model handling an unbounded number of tool calls with a pipeline of focused agents, each scoped narrowly enough that context is never the bottleneck.

The key insight: **context exhaustion is an architectural problem, not a model problem.** Upgrading to a larger context window treats the symptom. Structuring work so each agent stays well within its window treats the cause.

## System Overview

```
┌──────────────────────────────────────────────────────────────┐
│                          CLI Layer                            │
│  swarm run <repo> <goal> --type review                       │
│  swarm check | swarm compare ./runs                          │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│                    PipelineOrchestrator                        │
│  Drives sequential stages: Architect → Workers → Validator    │
│  → Synthesizer. Handles escalation, abort, event emission.    │
└──────┬───────────┬───────────┬────────────┬──────────────────┘
       │           │           │            │
┌──────▼──┐ ┌─────▼────┐ ┌───▼──────┐ ┌───▼─────────┐
│Architect│ │ Workers   │ │Validator │ │ Synthesizer  │
│  (7B)   │ │(2.4B/14B) │ │  (7B)    │ │    (32B)     │
└─────────┘ └─────┬─────┘ └────┬─────┘ └─────────────┘
                  │             │
           ┌──────▼─────────────▼──────┐
           │       Tool Layer           │
           │  fs · git · analysis · sh  │
           └────────────┬──────────────┘
                        │
           ┌────────────▼──────────────┐
           │      ToolCache             │
           │  Per-run memoization       │
           └────────────┬──────────────┘
                        │
           ┌────────────▼──────────────┐
           │       Backend              │
           │  oMLX (OpenAI-compat)      │
           │  localhost:8000            │
           └───────────────────────────┘
```

## Data Flow

### 1. Input Resolution

The CLI accepts a local path or Git URL. URLs are shallow-cloned to a temp directory. The resolved path becomes the sandbox root for all filesystem operations.

### 2. Repo Survey

Before the architect runs, the orchestrator scans the repo:
- Counts files and lines of code (excluding .git, node_modules, __pycache__, .venv)
- Detects languages from file extensions (`.py` → python, `.ts` → typescript, etc.)
- Produces a one-line summary for the architect's context

The detected languages gate the analysis tool surface — a Python-only repo never sees `lint_js` or `typecheck_ts`.

### 3. Architect Decomposition

The architect receives:
- The user's goal
- A task-type-specific decomposition prompt (from `pipeline.yaml`)
- The repo survey summary

It produces a JSON array of micro-objectives. Each has:
- `title` — one-line description
- `role` — which worker type handles it
- `expected_tools` — estimated tool calls (max 5)
- `scope` — file or directory hint

The orchestrator appends validator and synthesizer stages automatically.

### 4. Worker Execution

Workers execute sequentially. Each worker:

1. Gets a **fresh message list** (system prompt + task prompt + prior findings)
2. Enters the agent loop (chat → tool calls → dispatch → repeat)
3. Uses **role-specific tools** (read-only, code+shell, or analysis tools)
4. Benefits from the **shared ToolCache** — file reads from earlier subtasks are instant
5. Receives **recency-weighted prior findings**:
   - Preceding subtask: 800 chars
   - 2–3 back: 400 chars
   - 4+ back: one-line summary

If a worker fails (aborted or too many schema rejects), the orchestrator attempts **escalation** — retrying with a more capable role.

### 5. Validation

The validator receives all concatenated worker findings and checks every `file:line` citation against the actual codebase using `read_file` and `grep`. Findings that can't be verified are removed or flagged `[UNVERIFIED]`.

### 6. Synthesis

The synthesizer receives validated findings and produces the final report. It uses the 32B model (the only stage that does) and has no tools — it works from provided context only.

## Key Design Decisions

### Sequential Pipeline, Not Parallel

Workers execute sequentially rather than in parallel. This is deliberate:
- Only one model needs to be loaded at a time (critical for 64 GB memory budget)
- Later workers benefit from prior findings (build on earlier context)
- Debugging is simpler — the event log is a linear timeline

### Fresh Context Per Worker

Each worker starts with a clean message list. This is the core architectural fix for context exhaustion: no single agent accumulates unbounded context from prior tool calls. The recency-weighted prior findings provide continuity without the full token cost.

### Role-Scoped Tool Surfaces

Workers only see tools relevant to their role:
- Read workers can't write files — prevents accidental mutations during analysis
- Code workers get shell access — can run tests after making changes
- Analyze workers get static analysis tools — linting, type checking, security scanning
- The validator only has `read_file` and `grep` — can't add findings, only verify

This mirrors the principle of least privilege and reduces model confusion (smaller tool schemas = fewer hallucinated tool calls).

### Single ToolCache Across All Workers

All workers and the validator share one `ToolCache` instance per pipeline run. Benefits:
- A file read by worker 1 is instant for worker 5
- The validator's citation checks are nearly free if workers already read those files
- Cache hit rate is a useful signal — high rates suggest workers are overlapping

### Escalation Over Retry

When a worker fails, the system doesn't retry with the same model. It escalates to a more capable role:
- `worker_read` (2.4B) → `worker_analyze` (14B): more parameters, broader tool surface
- `worker_analyze` (14B) → `worker_code` (14B): same model, but with mutation tools

The rationale: if a 2.4B model can't handle a task, retrying won't help — it needs more capability. Escalation is limited to 1 retry to prevent cascading costs.

### Context Pressure as a Safety Net

Every agent loop iteration checks context pressure (`estimated_tokens / ctx_limit`). If pressure exceeds 0.7, old tool results are elided — replaced with one-line stubs like `[elided: read_file -> 4096 bytes]`. This preserves message structure (required for OpenAI-compatible alternation) while freeing tokens.

In practice, tight scoping (max 5 tool calls per micro-objective, role-appropriate context windows) means elision rarely triggers. But it's there as a safety net.

## Module Architecture

### Configuration (`config.py`)

Pydantic models that validate `pipeline.yaml`:

```
PipelineConfig
├── omlx_base_url: str
├── models: dict[str, str]           # model_key → model_id
├── roles: dict[str, RoleConfig]     # role_name → config
│   └── RoleConfig
│       ├── model_key, num_ctx, max_steps
│       ├── max_tokens_per_turn, temperature
│       └── tools: list[str]
├── task_types: dict[str, TaskTypeConfig]
│   └── TaskTypeConfig
│       ├── pipeline: list[str]      # ordered role sequence
│       └── architect_prompt: str
└── escalation: EscalationConfig
    └── worker_read → worker_analyze → worker_code
```

### Backend (`backend.py`)

Single `Backend` class wrapping httpx. Talks to oMLX at `localhost:8000` via the `/v1/chat/completions` endpoint. Returns `ChatResponse` with text, structured tool calls, and `GenerationTiming` (prompt tokens, completion tokens, wall time, throughput).

Backends are cached per model within a pipeline run to avoid re-creating HTTP clients.

### Tool System (`tools/`)

Three-layer architecture:

1. **Schema** (`ToolDef`) — name, description, JSON Schema parameters. Converted to OpenAI format via `to_openai()`.
2. **Implementation** (`ToolFn`) — function `(args: dict) → (result: str, error: str | None)`. Each tool module exports a `TOOL_FNS` dict.
3. **Dispatch** (`dispatch_tool`) — validates, executes, times, and caches. Returns `ToolCall` with metrics.

Tool modules:
- `fs.py` — read_file, list_dir, glob, grep (read-only) + write_file, edit_file (mutation). All scoped to repo root via `_safe()`.
- `git.py` — git_diff, git_log, git_show. Delegates to `git` CLI.
- `analysis.py` — 8 static analyzers, language-gated. Delegates to real tools (ruff, mypy, bandit, eslint, etc.).
- `shell.py` — allowlisted bash execution (20 binaries). Scoped to repo root, output capped at 8 KB.

### Agent Loop (`agents/loop.py`)

`run_agent()` is the shared execution engine. All four agent types (architect, worker, validator, synthesizer) call through it with different system prompts, tool surfaces, and configs.

The loop handles:
- Context pressure monitoring + tool result elision
- Structured and text-recovered tool call parsing
- Schema validation with self-correction feedback
- Per-call timing and telemetry
- Abort on max_steps or backend error

### Pipeline Models (`pipeline/model.py`)

```
PipelineRun
├── id, goal, task_type, repo_path
├── status: pending → running → done | blocked
├── subtasks: list[Subtask]
│   └── Subtask
│       ├── title, role, scope, expected_tools
│       ├── status: pending → running → done | blocked | skipped
│       ├── result_text, tool_calls
│       ├── metrics: StageMetrics
│       └── escalated_from: str | None
├── architect_result, validator_result, synthesizer_result
├── final_report
└── events: list[dict]  # timestamped event log
```

### Orchestrator (`pipeline/orchestrator.py`)

`PipelineOrchestrator.run()` drives the full pipeline:
1. Set repo root and detect languages
2. Run architect → parse micro-objectives → build subtasks
3. For each subtask: run worker → handle escalation → emit events
4. Run validator on collected findings
5. Run synthesizer for final report
6. Return completed `PipelineRun`

### Metrics (`metrics/`)

- `collector.py` — Extracts `RunMetrics` from a `PipelineRun`: aggregated tokens, tool calls, cache rates, per-role breakdowns.
- `report.py` — Rich console tables for single-run summaries and multi-run comparisons.

## Comparison to luxe's Current Architecture

| Dimension | luxe (current) | swarm (PoC) |
|-----------|---------------|-------------|
| Models per task | 1 (32B for everything) | 4–5 (right-sized per role) |
| Context strategy | Accumulate in one window | Fresh per worker, recency-weighted augmentation |
| Tool surface | Same tools for all subtasks | Role-scoped (read vs code vs analyze) |
| Validation | Post-hoc regex (`_verify_citations`) | Agent-driven verification pass |
| Synthesis | Same model that did analysis | Dedicated 32B, runs once, no tools |
| Failure handling | Retry same model | Escalate to more capable role |
| Memory budget | ~19 GB (one 32B model) | ~19 GB peak (models swap, not stack) |
