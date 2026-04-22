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
│   └───────────────────────────────────────────────────────────────┘   │
│                              ▲                                        │
│                              │ reuses Backend, ToolDef, ToolCall,     │
│                              │ _parse_text_tool_calls                 │
│   ┌───────────────────────────────────────────────────────────────┐   │
│   │  luxe/          multi-agent Claude-Code-alike                 │   │
│   │   ├─ cli.py           `luxe` typer entry point                │   │
│   │   ├─ repl.py          REPL loop + rich output + stats line    │   │
│   │   ├─ registry.py      LuxeConfig (YAML) — per-agent model,    │   │
│   │   │                   prompt, tools, budgets                  │   │
│   │   ├─ session.py       append-only JSONL per session           │   │
│   │   ├─ backend.py       Ollama /v1 factory wrapping Backend     │   │
│   │   ├─ router.py        interpreter w/ dispatch + ask_user tools│   │
│   │   ├─ runner.py        decision → specialist dispatcher        │   │
│   │   ├─ agents/                                                  │   │
│   │   │   ├─ base.py      shared tool-use loop, usage accounting  │   │
│   │   │   ├─ general.py   chat, no tools                          │   │
│   │   │   ├─ research.py  web_search + fetch_url                  │   │
│   │   │   ├─ writing.py   fs read+write, higher temperature       │   │
│   │   │   ├─ image.py     draw_things_generate                    │   │
│   │   │   └─ code.py      full fs + bash + web surface            │   │
│   │   └─ tools/                                                   │   │
│   │       ├─ fs.py        read_file, edit_file, glob, grep, ...   │   │
│   │       ├─ shell.py     bash (allowlist), scoped to CWD         │   │
│   │       ├─ web.py       DuckDuckGo + trafilatura extract        │   │
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
- Session logging (every tool call + result)
- Keyboard interrupt as a clean abort with `reason="interrupted (Ctrl-C)"`

## Configuration

`configs/agents.yaml` holds:
- Top-level: `ollama_base_url`, `draw_things_url`, `image_output_dir`, `session_dir`
- Per-agent: `model`, `system_prompt`, `temperature`, `max_steps`, `max_tokens_per_turn`, `max_wall_s`, `tools`, `enabled`

`LuxeConfig` (pydantic) validates on load. The runner applies cross-cutting
settings (Draw Things endpoint, image output dir) once per dispatch before
invoking the specialist.

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
