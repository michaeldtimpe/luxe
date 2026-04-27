# Architecture

Two tightly-linked layers share the same process-space, package-space, and
configuration pattern:

```
┌───────────────────────────────────────────────────────────────────────┐
│                          luxe (repo root)                          │
│                                                                       │
│   ┌───────────────────────────────────────────────────────────────┐   │
│   │  harness/       evaluation + optimization                     │   │
│   │   ├─ backends.py      OpenAI-compat Backend, ToolDef, ToolCall│   │
│   │   ├─ server.py        MLX / llama.cpp lifecycle + metrics     │   │
│   │   ├─ registry.py      YAML → Pydantic candidate registry      │   │
│   │   ├─ metrics.py       RunMetrics, per-turn TurnRecord         │   │
│   │   └─ cli.py           `lux` typer entry point                 │   │
│   │                                                               │   │
│   │  benchmarks/compression_repo.py                               │   │
│   │  strategies/          preprocess/index/retrieve/compress/     │   │
│   │                       prompt_assembly pipelines (JSON-        │   │
│   │                       configured strategies)                  │   │
│   │  fixtures/            compression_repos/ + compression_tasks/ │   │
│   │  shared/trace_hints.py  pytest / traceback path parser        │   │
│   │                         (used by both the compression         │   │
│   │                         benchmark and luxe's orchestrator)    │   │
│   └───────────────────────────────────────────────────────────────┘   │
│                              ▲                                        │
│                              │ reuses Backend, ToolDef, ToolCall,     │
│                              │ _parse_text_tool_calls,                │
│                              │ shared.trace_hints                     │
│   ┌───────────────────────────────────────────────────────────────┐   │
│   │  luxe/luxe_cli/ multi-agent Claude-Code-alike                │   │
│   │   ├─ main.py          `luxe` typer entry point                │   │
│   │   ├─ repl/            REPL loop split by concern              │   │
│   │   │   ├─ core.py      dispatch, stats line, sticky mode       │   │
│   │   │   ├─ tasks.py     /tasks subcommands + tail printer       │   │
│   │   │   ├─ review.py    /review + /refactor + plan-review loop  │   │
│   │   │   ├─ status.py    banner, /context, /tools                │   │
│   │   │   ├─ models.py    /pull, /variants, /models               │   │
│   │   │   ├─ aliases.py   /alias, /pin, /memory                   │   │
│   │   │   ├─ help.py      /help registry                          │   │
│   │   │   └─ prompt.py    prompt_toolkit session setup            │   │
│   │   ├─ registry.py      LuxeConfig (YAML) — per-agent model,    │   │
│   │   │                   prompt, tools, budgets, num_ctx         │   │
│   │   ├─ session.py       append-only JSONL + bind_backend tag    │   │
│   │   ├─ backend.py       Backend factory + URL→kind derivation   │   │
│   │   ├─ providers/       BackendProvider protocol + concretes    │   │
│   │   │   ├─ openai_compat.py  /v1/models base impl               │   │
│   │   │   ├─ ollama.py    Ollama /api/* introspection             │   │
│   │   │   ├─ lmstudio.py  LM Studio /api/v0 introspection         │   │
│   │   │   └─ omlx.py      oMLX (OpenAI-compat, minimal metadata) │   │
│   │   ├─ router.py        interpreter w/ dispatch + ask_user tools│   │
│   │   ├─ heuristic_router.py keyword/regex pre-router             │   │
│   │   ├─ import_graph.py   AST-walked Python imports + neighbors  │   │
│   │   ├─ runner.py        decision → specialist dispatcher        │   │
│   │   ├─ tasks/           multi-step orchestrator                 │   │
│   │   │   ├─ orchestrator.py subtask driver, shallow-read retry   │   │
│   │   │   ├─ planner.py  goal → ordered subtasks                  │   │
│   │   │   ├─ clarify.py  screener for clarifying questions        │   │
│   │   │   ├─ model.py    Task/Subtask dataclasses + persistence   │   │
│   │   │   ├─ cache.py    task-scoped ToolCache + wrap_tool_fns    │   │
│   │   │   ├─ report.py   markdown report assembly                 │   │
│   │   │   ├─ run.py      subprocess entry (auto-saves report)     │   │
│   │   │   └─ spawn.py    fork + SIGTERM helpers                   │   │
│   │   ├─ tool_library.py  /calc's saved-formula tool library      │   │
│   │   ├─ agents/                                                  │   │
│   │   │   ├─ base.py      shared tool-use loop, usage accounting  │   │
│   │   │   ├─ general.py   chat, no tools                          │   │
│   │   │   ├─ lookup.py    single web_search (snippet-only fast)   │   │
│   │   │   ├─ research.py  web_search + fetch_url + fetch_urls     │   │
│   │   │   ├─ writing.py   fs read+write, higher temperature       │   │
│   │   │   ├─ image.py     draw_things_generate                    │   │
│   │   │   ├─ code.py      full fs + bash + web surface            │   │
│   │   │   ├─ review.py    read-only fs + read-only git            │   │
│   │   │   ├─ refactor.py  read-only fs + read-only git            │   │
│   │   │   └─ calc.py      create_tool + library-matched tools     │   │
│   │   └─ tools/                                                   │   │
│   │       ├─ fs.py         read_file, edit_file, glob, grep, ...  │   │
│   │       ├─ shell.py      bash (allowlist), scoped to CWD        │   │
│   │       ├─ web.py        DuckDuckGo + trafilatura + fetch_urls  │   │
│   │       ├─ git_tools.py  git_diff / git_log / git_show          │   │
│   │       └─ draw_things.py  HTTP /sdapi/v1/txt2img client        │   │
│   └───────────────────────────────────────────────────────────────┘   │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘

        external services (all local)
        ─────────────────────────────
        Ollama server           :11434  (all LLM inference)
        Draw Things HTTP server :7859   (image generation)
```

## Data flow — a single REPL turn

```
user types prompt
      │
      ▼
 Session.append("user", prompt)            ~/.luxe/sessions/<id>.jsonl
      │
      ▼
 router.route(prompt)
   ├─ backend.chat(…, tools=[dispatch, ask_user])  →  Ollama :11434
   ├─ optional 0-2 rounds of ask_user  →  stdin readline
   └─ RouterDecision(agent, task, reasoning)
      │
      ▼
 runner.dispatch(decision)
   ├─ draw_things.set_endpoint(cfg)        # cross-cutting config
   ├─ make_backend(agent.model)
   └─ _SPECIALISTS[decision.agent](...)
          │
          ▼
    agents/base.py run_agent loop
      ┌───────────────────────────────────────────────────┐
      │  until max_steps | max_wall_s | no tool_calls:    │
      │    response = backend.chat(messages, tools=…)     │
      │    if text-JSON tool call: recover first only     │
      │    if response.tool_calls:                        │
      │        dispatch to tool_fns[name]                 │
      │        append {role:tool,…} to messages           │
      │        Session.append tool event                  │
      │    else:                                          │
      │        final_text = response.text                 │
      │        Session.append assistant event             │
      └───────────────────────────────────────────────────┘
      │
      ▼
 REPL renders final text + stats line
   "<agent> · <s>s · <in>↑ <out>↓ tokens · <steps> steps · <tool_calls> tool calls"
```

## Backend abstraction

Two layers:

**Chat transport — `harness.backends.Backend`.** Every supported
provider speaks `/v1/chat/completions`, so one Backend client drives
all four. `luxe_cli.backend.make_backend(model, base_url=...)` wraps
it; `kind` is derived from the resolved URL (matched against
`_BACKEND_OVERRIDE_URLS`) so telemetry sees the truth instead of a
hardcoded label.

```python
Backend(kind="lmstudio", base_url="http://127.0.0.1:1234", model_id=<id>)
```

**Introspection — `luxe_cli.providers.BackendProvider`.** Listing
models, querying context length, server health, and prewarm differ
per provider (`/api/show` vs `/v1/models` vs `/api/v0/models`). The
protocol is the seam; concrete classes live in `providers/`:
- `OllamaProvider` — wraps the existing `luxe_cli.backend` functions
  (Ollama-specific endpoints).
- `OpenAICompatProvider` — base for any `/v1/models`-style server.
- `LMStudioProvider`, `OMLXProvider` — thin subclasses, each picking
  the right auth env var (`LM_API_TOKEN` vs `OMLX_API_KEY`).
- `get_provider(kind, base_url)` — single construction point.

`ToolDef.to_openai()` produces the standard `{type:"function", function:{…}}`
schema; any OpenAI-compat server accepts it. `_parse_tool_calls` handles
both the structured `tool_calls` field and raw text JSON from models
that skip the `<tool_call>` wrapper.

## Tool dispatch

Every tool module (`luxe/tools/*.py`) exports:
- `tool_defs()` → `list[ToolDef]` describing the schema
- A `*_FNS` dict mapping tool name → `(args: dict) -> (result, error)`

The shared agent loop (`luxe/agents/base.py`) takes those two, handles:
- Structured tool calls (Ollama's normal path)
- Text-JSON fallback (first call only — speculative plans get 1 entry, not 63)
- Budgets: `max_steps`, `max_wall_s`, `max_tool_calls_per_turn`
- Per-turn token accounting (sums `response.timing.prompt_tokens` / completion)
- Per-call telemetry: each `ToolCall` is stamped with `wall_s`, `ok`,
  `bytes_out` as it runs so downstream (`state.json`, `/tasks analyze`,
  `scripts/summarize_runs.py`) can break wall and adoption down by tool
- Session logging (every tool call + result)
- Keyboard interrupt as a clean abort with `reason="interrupted (Ctrl-C)"`

### External-binary wrappers

`luxe/tools/_subprocess.py:run_binary()` is the shared subprocess
runner for every tool that shells out to an external binary (git,
ripgrep, and the 10 static analyzers). It resolves binary paths
against the current venv's `bin/` before falling back to system
PATH, so tools installed via `uv sync --extra dev` work even when
luxe runs as a detached subprocess with an unchanged shell
environment. Per-axis `httpx.Timeout` on the backend client
(`harness/backends.py`) gives slow 32B decodes 20 minutes of read
headroom without starving quick connect/write failures.

### Static-analysis tools

`luxe/tools/analysis.py` holds 10 analyzer wrappers following the
same `run_binary` + `(result, err)` pattern as `fs.grep`:

- **Python:** `lint` (ruff), `typecheck` (mypy), `security_scan`
  (bandit), `deps_audit` (pip-audit), `security_taint` (semgrep),
  `secrets_scan` (gitleaks).
- **Cross-language:** `lint_js` (eslint), `typecheck_ts` (tsc),
  `lint_rust` (cargo clippy), `vet_go` (go vet).

Each tool reshapes its native JSON/JSONL/text output into a uniform
`{findings: [...], count, note?}` payload capped at 150 items.
Missing binaries or missing project markers produce a helpful
`note` the model can read and adapt to rather than crashing. See
`luxe/luxe_cli/README.md` for the full per-tool reference.

## Configuration

`configs/agents.yaml` holds:
- Top-level: `ollama_base_url` (legacy fallback URL),
  `draw_things_url`, `image_output_dir`, `session_dir`
- `providers:` — named backend endpoints, e.g.
  `lmstudio: { base_url: "http://127.0.0.1:1234", kind: lmstudio }`.
  Agents reference these by key.
- `default_provider:` — provider used when an agent doesn't set
  `provider:` or `endpoint:`.
- Per-agent: `model`, `system_prompt`, `temperature`, `max_steps`,
  `max_tokens_per_turn`, `max_wall_s`, `tools`, `enabled`,
  `min_tool_calls` (investigation floor), `num_ctx` (fixed
  per-mode context window — Ollama-effective via `options.num_ctx`,
  oMLX/llama-server honor server-side `--max-kv-size`), `provider`
  (key in the providers map — preferred), `endpoint` (legacy
  per-agent base URL — explicit URL wins over `provider` for the
  migration window).

`LuxeConfig.resolve_endpoint(agent)` is the single dispatch lookup:
`agent.endpoint` → `providers[agent.provider]` →
`providers[default_provider]` → `ollama_base_url` (legacy).
Session JSONL records carry `provider` + `base_url` so cross-backend
A/B comparisons can filter by which provider served each turn —
tagged automatically by `Session.bind_backend(...)` wrapped around
each dispatch.

`LuxeConfig` (pydantic) validates on load. The runner applies cross-cutting
settings (Draw Things endpoint, image output dir) once per dispatch before
invoking the specialist.

### Task-level and subtask-level budget overrides

The static per-agent budgets above are the defaults. `num_ctx` is
fixed per agent in `configs/agents.yaml` and not overridden at
runtime — see "Per-mode ctx" below for the values and rationale.

The remaining override axes:

- **`Task.max_wall_s`**. Set by `/review` and `/refactor` based on a
  pre-flight repo survey (`luxe/repo_survey.py:analyze_repo` →
  `size_budgets` → tier table). Bigger repos get longer task walls;
  ctx is unchanged.
- **`Subtask.max_tokens_per_turn_override`**. Populated by the planner
  at plan time — synthesis subtasks get a doubled output cap so the
  severity-grouped report doesn't truncate mid-category.

Both override axes thread through
`luxe/tasks/orchestrator.py:_cfg_with_task_overrides`, which derives
an `AgentConfig.model_copy(update={...})` just for that task's
dispatches.

### Pre-retrieval for trace-bearing tasks

`luxe/tasks/orchestrator.py:_augment_with_trace_hints` scans the
subtask title plus prior completed subtasks' `result_text` for
`path.py:LINE` / `File "path.py", line N` references (via
`shared.trace_hints.parse_trace_paths`) and pre-reads up to 3 cited
files, prepending them as a `# Files mentioned in the error you're
debugging` block before `_augment_with_prior` output. Zero-overhead
on tasks with no trace paths. This is the positive transfer from
the compression benchmark (April 2026): oracle-style selectivity is
the one compression technique that measurably helps; summarization
and outlining regressed pass rate, so we deliberately don't
summarise file contents — they land raw.

Seed files are then expanded via `luxe/import_graph.py`. The module
AST-walks every `.py` file under the repo root, extracting `import X` /
`from X import Y` / relative-import edges and building a forward
(`imports`) + reverse (`imported_by`) index, cached on a `max(mtime)`
key so rebuilds only happen when files change. `neighbors(graph,
path)` returns the first-hop union — imports first, then importers,
capped. The augmentation pre-reads the cited file plus up to
`max_files − len(seeds)` neighbors, giving the agent the cited module
plus its closest collaborators in one turn.

### Heuristic pre-router

`luxe/heuristic_router.py` is a deterministic keyword/regex scorer
called from `router.route()` before the LLM pass. Each agent gets a
per-feature score table (path-like tokens + code verbs → `code`,
`draft`/`essay`/`chapter` → `writing`, short-interrogative + factual
noun → `lookup`, currency markers → `lookup`, `compare`/`synthesise`
→ `research`, `draw`/`generate an image` → `image`, arithmetic chars +
compute verbs → `calc`); `decide()` returns `(agent, confidence,
scores)` or `None`. Short prompts (< 3 words), meta questions (`can
you …`), and low-margin decisions return `None` and fall through to
the LLM router — the heuristic never pretends to know when it
doesn't. `review` / `refactor` aren't scored (command-driven); nor is
`general` (the residual). Session logs tag each decision with
`"source": "heuristic"` vs `"llm"` for offline replay / A/B.

### Language-gated analyzer surface

`luxe/tools/analysis.tool_defs(languages=…)` filters the ten-tool
analyzer catalog by language family. On `/review` / `/refactor` the
pre-flight repo survey records
`RepoSurvey.language_breakdown` and threads it through
`Task.analyzer_languages` →
`_cfg_with_task_overrides` → `AgentConfig.analyzer_languages`. A pure-
Python repo never sees `lint_js` / `typecheck_ts` / `lint_rust` /
`vet_go` in its tool prompt; `secrets_scan` is always exposed. ~400
tokens saved per turn on single-language repos — the tool-description
block is dead weight otherwise.

### Task-scoped tool cache + schema validation

`luxe/tasks/cache.py` defines a per-Task `ToolCache` that memoizes
deterministic read-only tool calls by `(name, hash(args))`. The
`Orchestrator` allocates a fresh cache on every `run()` and threads it
through `runner.dispatch` → `review|refactor|code.run` →
`wrap_tool_fns`. The wrapping layer gates membership: `read_file`,
`list_dir`, `glob`, `grep`, the ten static analyzers, and the three
read-only git tools are cached; `write_file`, `edit_file`, `bash`,
and `web_*` never are. The pre-fix pattern had review subtasks 2–6
each re-reading the same three source files and re-running the same
four security analyzers — the cache collapses those repeats into one
real invocation, and the orchestrator emits per-subtask
`cache_hits` / `cache_misses` in its `end` events so the tail + bench
harness can see the payoff directly.

Client-side JSONSchema validation lives in
`luxe/agents/base.py:_validate_args`. Before each tool fn dispatch,
the agent checks `required` fields are present and primitive types
match the declared schema; a mismatch returns a structured error via
the normal tool-result path (so the model sees it and retries) and
increments `AgentResult.schema_rejects` → `Subtask.schema_rejects` →
the task-level finish event. This catches the common model mistake
(`grep path=foo.py` with no `pattern`) one turn earlier than the
previous behaviour, which let the malformed call reach the ripgrep
subprocess and surface as a noisier runtime `KeyError`.

### Observability: summary vs. verbose tail

`/tasks tail` has two modes driven by a single flag. The default
renders one line per subtask begin/end. `/tasks tail <id> -v` adds
per-tool-call lines (name, two-key arg preview, wall time, bytes out,
ok/error). Implementation: `luxe/agents/base.py:run_agent` accepts an
`on_tool_event` callback; `runner.dispatch` threads it through; the
orchestrator wires each subtask's callback to `_emit`, so every tool
call lands in `log.jsonl` regardless of render mode. `-v` only
changes the live-render filter.

### Task resumption

`luxe/tasks/model.reset_incomplete_subtasks(task)` flips any
blocked/skipped/running subtasks back to `pending` (clearing their
metrics + error fields) and persists. `/tasks resume <id>` then
re-spawns via the same `spawn_background` → `run.py` → `Orchestrator`
path. The orchestrator already skipped non-`pending` subtasks
(`if sub.status != "pending": continue`), so resume is effectively
free of special-case logic on the execution side — completed subtasks
stay done and their `result_text` continues to seed
`_augment_with_prior`.

### Orchestrator performance history

`luxe/scripts/bench_orchestrator.py` is an append-only history
of task-level orchestration metrics. Records land in
`results/orchestrator_bench/history.jsonl`, one JSON object per run,
stamped with the short git rev. Three subcommands:

- `import <task-id>` reads a finished `~/.luxe/tasks/<id>` off disk,
  reconstructs totals from `state.json` + `log.jsonl`, and appends a
  row. Used to backfill baselines from completed sessions without
  re-running.
- `run "<goal>" [--cwd <path>]` plans + runs a fresh task against the
  Orchestrator, then appends. Requires Ollama + the review model.
- `show [-n N]` tails the history with per-row deltas (wall time,
  tool calls, cache hits/misses, schema rejects, tokens) so a
  regression between two commits is visible at a glance.

The point isn't to micro-benchmark model decode — that's what
`harness/metrics.py` and the existing benchmarks cover. It's to track
the things a language/runtime change can actually move: how often the
orchestrator spent wall time on work it already had cached, how often
it dispatched malformed tool calls, how often it spent tokens re-
reading the same files.

## Scoping / safety

- **Code agent filesystem access** is scoped to `fs.repo_root()` which
  defaults to the process CWD. `_safe()` raises `PermissionError` on any
  path that escapes the root after `.resolve()`.
- **Bash** uses an allowlist (leading binary must match). No shell
  metacharacters are interpreted — `shlex.split` parses, first token is
  the binary.
- **Web** calls are minimal: DDG search + URL fetch. No cloud LLM calls
  ever. Queries leave the machine; fetched content is extracted
  server-side via `trafilatura` before being fed to the local model.

## Session persistence

Every turn (user, router decision, tool call, tool result, assistant) is
appended as one JSONL line at
`~/.luxe/sessions/<ISO-timestamp>-<slug>.jsonl`.

Crash-safe (append-only, no in-memory buffer). `luxe resume` points to the
latest file; on open, the REPL shows the last 4 events so the user has
context even without full context rehydration.

## Service integration

- `daily_driver/launchd/com.luxe.ollama.plist` keeps `ollama serve`
  running at login (127.0.0.1:11434, 5-minute keep-alive).
- `daily_driver/install_luxe.sh` installs the plist, symlinks `luxe` to
  `~/.local/bin/`, and verifies the HTTP endpoint.
- Draw Things is a separate app the user launches; luxe probes
  `/sdapi/v1/options` at startup and marks the image agent unavailable
  (with a helpful error) if unreachable.
