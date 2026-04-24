# Architecture

Two tightly-linked layers share the same process-space, package-space, and
configuration pattern:

```
┌───────────────────────────────────────────────────────────────────────┐
│                          luxebox (repo root)                          │
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
│   │  luxe/          multi-agent Claude-Code-alike                 │   │
│   │   ├─ cli.py           `luxe` typer entry point                │   │
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
│   │   ├─ session.py       append-only JSONL per session           │   │
│   │   ├─ backend.py       Ollama /v1 factory wrapping Backend     │   │
│   │   ├─ router.py        interpreter w/ dispatch + ask_user tools│   │
│   │   ├─ runner.py        decision → specialist dispatcher        │   │
│   │   ├─ tasks/           multi-step orchestrator                 │   │
│   │   │   ├─ orchestrator.py subtask driver, shallow-read retry   │   │
│   │   │   ├─ planner.py  goal → ordered subtasks                  │   │
│   │   │   ├─ clarify.py  screener for clarifying questions        │   │
│   │   │   ├─ model.py    Task/Subtask dataclasses + persistence   │   │
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

`harness.backends.Backend` is the common interface. `luxe.backend.make_backend`
just points it at Ollama's `/v1`:

```python
Backend(kind="mlx", base_url="http://127.0.0.1:11434", model_id=<ollama_tag>)
```

Benefits:
- Same type works for MLX server, llama-server, Ollama.
- `ToolDef.to_openai()` produces the standard `{type:"function", function:{…}}` schema; any OpenAI-compat server accepts it.
- `_parse_tool_calls` handles both the structured `tool_calls` field and raw text JSON from models that skip the `<tool_call>` wrapper.

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
`luxebox/luxe/README.md` for the full per-tool reference.

## Configuration

`configs/agents.yaml` holds:
- Top-level: `ollama_base_url`, `draw_things_url`, `image_output_dir`, `session_dir`
- Per-agent: `model`, `system_prompt`, `temperature`, `max_steps`,
  `max_tokens_per_turn`, `max_wall_s`, `tools`, `enabled`,
  `min_tool_calls` (investigation floor), `num_ctx` (Ollama
  `options.num_ctx` override), `endpoint` (per-agent base URL, e.g.
  llama-server for Gemma 3)

`LuxeConfig` (pydantic) validates on load. The runner applies cross-cutting
settings (Draw Things endpoint, image output dir) once per dispatch before
invoking the specialist.

### Task-level and subtask-level budget overrides

The static per-agent budgets above are the defaults. Two override
axes sit on top:

- **`Task.num_ctx_override`** (`luxe/tasks/model.py`). Set by
  `/review` and `/refactor` based on a pre-flight repo survey
  (`luxe/repo_survey.py:analyze_repo` → `size_budgets` → tier
  table). Threads through `luxe/tasks/orchestrator.py:
  _cfg_with_task_overrides`, which derives an
  `AgentConfig.model_copy(update={"num_ctx": ...})` just for that
  task's dispatches.
- **`Subtask.num_ctx_override` / `Subtask.max_tokens_per_turn_override`**.
  Populated by the planner at plan time — synthesis subtasks get a
  doubled output cap so the severity-grouped report doesn't
  truncate mid-category. Precedence: subtask > task > agent default.

The `code` agent has its own `_resize_for_cwd()` hook in
`luxe/agents/code.py` that surveys the current working directory at
dispatch time and bumps `num_ctx`/`max_wall_s` for medium+ repos.
No task wrapper needed — the hook runs before `run_agent()`.

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

- `daily_driver/launchd/com.luxebox.ollama.plist` keeps `ollama serve`
  running at login (127.0.0.1:11434, 5-minute keep-alive).
- `daily_driver/install_luxe.sh` installs the plist, symlinks `luxe` to
  `~/.local/bin/`, and verifies the HTTP endpoint.
- Draw Things is a separate app the user launches; luxe probes
  `/sdapi/v1/options` at startup and marks the image agent unavailable
  (with a helpful error) if unreachable.
