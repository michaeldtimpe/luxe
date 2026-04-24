# luxe — local multi-agent Claude-Code-alike

A terminal REPL that takes a prompt, routes it to a specialist agent, and
runs fully on your Mac via Ollama + llama.cpp + Draw Things. Nine
specialists share a single router: **general**, **lookup**,
**research**, **writing**, **code**, **image**, **review**,
**refactor**, **calc**. A task orchestrator stitches specialists
together for multi-step goals, with background execution, clarifying
questions, plan-preview, and context forwarding between subtasks.

## Status

| Agent | Model | Backend | Notes |
|---|---|---|---|
| router | `qwen2.5:7b-instruct` | Ollama | Hands off to one specialist; up to 2 clarifying Qs |
| general | `qwen2.5:7b-instruct` | Ollama | Concise chat, ~1–2 s typical |
| lookup | `qwen2.5:7b-instruct` | Ollama | Single-snippet factual lookup (fast path before `research`) |
| research | `qwen2.5:32b-instruct` | Ollama | DuckDuckGo + `trafilatura` extract, cited output; `fetch_urls` for parallel reads |
| writing | `gemma-3-27b-it` | **llama-server** | Served via `llama-server --jinja`; native `tool_code` blocks parsed inline |
| code | `qwen2.5-coder:14b-instruct` | Ollama | Full tool surface; 32b variants had Ollama tool-use quirks |
| image | `qwen2.5:7b-instruct` (prompt expander) + Draw Things | Ollama + HTTP | Draw Things API on 7859 |
| review | `qwen2.5:32b-instruct` | Ollama | Read-only code review with git context; driven by `/review <url>`. Orchestrator retries tool-shy agents, pre-runs canonical greps, and verifies every cited `file:line` exists before reporting |
| refactor | `qwen2.5:32b-instruct` | Ollama | Read-only optimization suggestions with git context; driven by `/refactor <url>`. Same anti-fabrication guardrails as `review` |
| calc | `qwen2.5:32b-instruct` | Ollama | Multi-step arithmetic; `create_tool` + library-matched tools for reusable formulas |

Ollama hot-swaps, so only one Ollama-served model is resident at a time.
`llama-server` for the writing agent stays resident (Gemma 3 27B Q4_K_M +
KV cache ≈ 17–22 GB at 32–64K context).

## Prereqs

- macOS with Homebrew
- `brew install ollama llama.cpp uv ripgrep`
- [Draw Things.app](https://drawthings.ai/) — enable **HTTP server** in
  the app's settings (default 7859 in newer versions; 7860 in older)
- For research: no extra setup — DuckDuckGo runs in-process via `ddgs`
- For writing: `llama-server` (comes with `llama.cpp`) serves Gemma 3
  27B with native function-call support; see **Starting llama-server**
  below

## Install

```bash
cd <your-clone>/luxe
uv sync --extra dev          # installs analyzer tools (ruff/mypy/bandit/pip-audit/semgrep)
bash daily_driver/install_luxe.sh
brew install gitleaks         # optional: enables the `secrets_scan` tool
```

That:

- Symlinks `luxe` to `~/.local/bin/luxe`
- Installs a launchd plist so `ollama serve` runs at login
- Verifies Ollama is reachable on `127.0.0.1:11434`

`--extra dev` pulls the static-analysis binaries into `.venv/bin/`
(~300 MB including semgrep's lazy rulesets). The `code`, `review`,
and `refactor` agents call them as tools; missing binaries degrade
gracefully — if you skip `--extra dev`, the review still runs with
the fs/git/web tools, just without the analyzer findings.

Pull the Ollama-served models (they'll pull on demand too):

```bash
ollama pull qwen2.5:7b-instruct
ollama pull qwen2.5:32b-instruct
ollama pull qwen2.5-coder:14b-instruct
ollama pull mixtral:8x7b-instruct-v0.1-q3_K_M
```

## Starting llama-server (writing agent)

Gemma 3 doesn't advertise the `tools` capability in Ollama's manifest,
so it 400s on any tool-enabled request. We run it under `llama-server`
with `--jinja` and parse its native ```tool_code``` blocks. Launch it
once per login; it stays resident:

```bash
llama-server \
  -hf ggml-org/gemma-3-27b-it-GGUF:Q4_K_M \
  --jinja -ngl 99 -c 32768 --parallel 1 \
  --host 127.0.0.1 --port 8080
```

First run downloads ~16 GB to `~/.cache/huggingface/hub/`. Subsequent
starts boot in ~1 s. `/context` in the REPL shows the loaded context
window and approximate RAM usage per server.

The writing agent's config points at `http://127.0.0.1:8080` via the
per-agent `endpoint` field in `configs/agents.yaml` — other agents keep
going to Ollama on `11434`.

## Use

```bash
luxe                       # REPL — type a prompt, router picks an agent
luxe list                  # list saved sessions
luxe resume                # continue most recent session
luxe --session <id>        # resume a specific session
luxe agents                # show configured agents + models
luxe analyze <path>        # one-shot read-only code review on a repo
luxe analyze <path> --review  # route through the review agent (background task)
luxe clean --days 7        # delete session files older than N days (default 7)
luxe update                # git pull + pip install -e . from the luxe repo root
```

Example REPL session:

```
$ luxe
╭──────────────────────────────────────────────╮╮╮
│ .:. luxe .:.                version: f669a5d │││
│ model:  qwen2.5:7b-instruct   params: 7.6 B  │││
│ folder: ~/code/myproj           mode: router │││
╰──────────────────────────────────────────────╯╯╯
luxe> what is the difference between concurrency and parallelism?
→ routed to general
Concurrency is about *dealing* with multiple tasks at once…
general · 2.4s · 210↑ 52↓ tokens · 1 steps · 0 tool calls
ctx: 210/32,768 (99% free) · qwen2.5:7b-instruct

luxe> /writing
→ sticky agent set to writing
luxe (writing)> review the notes in this folder and suggest a three-act structure
→ routed to writing (sticky)
...
```

The banner shows above every prompt: version (git hash), current model,
param count, folder, and mode. `mode` is either `router` or an agent
name (when sticky is engaged — see **Sticky mode** below). The tripled
right edge picks three fresh palette colors per render (one per vertical
stripe), mirroring the `.:.` title markers' per-refresh rotation.

## REPL commands

**Core**

| Command | Notes |
|---|---|
| `/help` | Show the command cheatsheet |
| `/agents` | List configured agents with model + param count |
| `/models` | List Ollama models currently available with param count |
| `/variants [family]` | Show released sizes per model family; green = installed, dim = pullable |
| `/pull <tag>` | Download a model from Ollama's registry with a live progress bar; prompts to assign it to an agent on success |
| `/context` | Show loaded ctx, max ctx, approximate KV-cache RAM, and server process RSS per agent |
| `/quit` · `/exit` (or Ctrl-D) | Exit; session auto-saves |

**Direct dispatch** — bypass the router:

```
/general   <prompt>
/research  <prompt>
/writing   <prompt>
/code      <prompt>
/image     <prompt>
/review    <git-url>
/refactor  <git-url>
```

Running an agent-flag alone (e.g. `/writing`) pre-warms the model and
engages sticky mode without a prompt.

**Sticky mode.** After any dispatch (direct or routed), subsequent
plain-text prompts go to the same agent automatically — the banner
reads `mode: <agent>`. Use `/clear` to drop sticky and return to
router-driven routing for the next prompt. `/new` also clears sticky.

**Turn control**

| Command | Notes |
|---|---|
| `/retry` | Rerun the last prompt with the same agent |
| `/redo <agent>` | Rerun the last prompt with a different agent |
| `/model <tag>` | One-off model override for the next turn |
| `/params <text>` | Force the banner's `params:` display (useful if auto-detection can't resolve); `/params clear` to unset |
| `/pin <text>` | Prepend a sticky note to every subsequent prompt |
| `/pins` / `/unpin [n]` | List / remove pins |
| `/history [n]` | Show the last n session events (default 10) |

**Sessions**

| Command | Notes |
|---|---|
| `/session` | Show current session id + path |
| `/save <name>` | Bookmark current session under `<name>` |
| `/sessions` | List saved sessions |
| `/resume <id-or-name>` | Switch to another session (name, full id, or unique prefix) |
| `/new` | Start a fresh session (reset totals, pins, sticky) |
| `/clear` | Drop the sticky agent — next prompt re-routes |

**Memory & aliases** (persisted in `~/.luxe/`)

| Command | Notes |
|---|---|
| `/memory` / `/memory view` / `/memory clear` | Open or inspect `~/.luxe/memory.md` |
| `/alias add <name> <expansion>` | Define `/<name>` as a shortcut |
| `/alias list` / `/alias remove <name>` | Manage aliases |

**Input niceties** (prompt_toolkit)

- `↑` / `↓` arrow keys: browse prompt history (persistent at `~/.luxe/history`)
- `Alt-Enter` (or `Esc` then `Enter`): insert a newline without submitting
- Pasting a multi-line block: arrives as one prompt via bracketed paste
  — no more each-line-submits problems with prose prompts

## Tasks — multi-step orchestration

For goals that need more than one specialist, the task orchestrator
plans, asks clarifying questions, previews the plan, runs subtasks
serially (each seeing prior findings), and produces a save-able report.

```
/tasks <goal>              plan + run in the background (detached subprocess)
/tasks --sync <goal>       plan + run synchronously in the REPL
/tasks                     list recent tasks (with alive marker)
/tasks status [id]         full subtask breakdown; prefix id is fine
/tasks log [id]            tail the task's structured log.jsonl
/tasks tail [id] [-v]      live-follow event stream; `-v` adds per-tool-call lines
/tasks watch [id]          auto-refreshing dashboard
/tasks abort [id]          SIGTERM a running task; SIGKILL after 5 s
/tasks resume [id]         re-run blocked/skipped subtasks in place; keeps done ones
/tasks save [id]           stitch subtask outputs into md/txt report
/tasks analyze [id]        per-subtask tool-usage breakdown + adoption ratio
```

**Verbose tail (`-v`).** The default tail renders one line per subtask
begin/end (agent, model, wall time, tool-call count, token totals).
Adding `-v` surfaces every individual tool call as it happens:

```
│ [review] · qwen2.5:32b-instruct · Security issues
│   → grep pattern=token= path=luxe
│   ✓ grep 0.42s · 1.2KB
│   → read_file path=luxe/prefs.py
│   ✓ read_file 0.10s · 3.4KB
│ ✓ sub 03 · 4m 12s · 8 tool calls · …
```

Each line shows the tool, a two-key arg preview, duration, and result
size. Errors render as `✗ <tool> <wall>s · err: <message>`. Events are
persisted to `log.jsonl` either way; `-v` only changes what the tail
renders.

**Resume.** When a task exits with blocked/skipped subtasks (e.g. a
per-agent wall budget expired mid-inspection), `/tasks resume <id>`
flips those back to `pending` and re-spawns. Completed subtasks are
preserved — their `result_text` still seeds `_augment_with_prior` for
anything that runs next — so resume doesn't re-pay the time already
spent. Only meaningful on non-alive tasks; for a live one, `/tasks
abort` first.

**What happens when you run `/tasks <goal>`:**

1. **Clarifying questions.** A small screener LLM decides whether the
   goal is specific enough to plan. If not, it emits up to 3 focused
   questions; your answers get folded into the goal.
2. **Planning.** A planner LLM decomposes the goal into 1–8 ordered
   subtasks, each with a recommended specialist.
3. **Plan preview.** You get a `plan>` prompt where you can:
   - `<enter>` — run as shown
   - `abort` — cancel before anything runs
   - `agent <i> <name>` — change subtask *i*'s assigned agent
   - `drop <i>` — remove a subtask and re-index
   - `add <title>` — append a new subtask
4. **Execution.** Background by default (detached subprocess, survives
   REPL exit). Each subtask sees a summary of previously completed
   subtasks, so later steps build on earlier findings instead of
   starting from scratch.
5. **Report.** Sync mode auto-prompts for a filename; background
   `/review` and `/refactor` runs auto-write `REVIEW-<id>.md` (or
   `REFACTOR-<id>.md`) into the cloned repo when they finish, with a
   `📝 saved` line in `/tasks tail`. `/tasks save <id>` still works
   and lets you rename or switch to `.txt`.

State lives at `~/.luxe/tasks/<task-id>/`:

- `state.json` — full Task with subtasks and structured `ToolCall` list
- `log.jsonl` — one event per state change (start / begin / end /
  retry_transport / tool_use_retry / forced_inspection /
  grounding_issues / report_saved / abort_sigterm / finish); tailed by
  `/tasks log`
- `stdout.log` — raw stdout/stderr of the background subprocess
- `repo_path` (for `/review` + `/refactor` only) — the clone root

Background tasks are resilient to `/exit`: they keep running, and a
subsequent `luxe` launch shows them in `/tasks list`. SIGTERM is
polled at subtask boundaries (graceful); SIGKILL after 5 s grace is
forceful and reconciles state to `aborted`.

## Code intelligence — `/review` and `/refactor`

Both take a git URL. They scan the **current folder** for an existing
clone matching the URL's `origin` (pulled via `git pull --ff-only`);
if none found, they `git clone` into the cwd as a subdirectory.

The runtime wraps the repo in a background task that pins every
subtask to the dedicated agent:

- **`/review <url>`** — security → correctness → robustness →
  maintainability. Severity-grouped markdown report on save.
- **`/refactor <url>`** — performance → architecture → code size →
  idiomatic improvements. Impact-ranked report.

Both are read-only (no `write_file`, no `bash`). They identify and
report; applying fixes is a separate conversation.

```
luxe> /review https://github.com/foo/bar
resolving https://github.com/foo/bar...
updated: Already up to date. → /cwd/bar
4 subtasks (all → review):
  1. ...
plan> <enter>
→ launched T-20260422T… (pid 12345, cwd /cwd/bar)
monitor with /tasks status T-20260422…
when done, save the report with /tasks save T-20260422…
```

**Headless entry point.** `luxe analyze <path> --review` runs the same
review pipeline without the REPL: it plans, persists, and spawns the
background task, then prints the task id and a `/tasks tail` hint so
you can pick it up in a REPL session later (or just watch the log
file under `~/.luxe/tasks/<id>/`).

**Adaptive budgets.** `/review` and `/refactor` pre-flight-survey the
cloned repo (file count, LOC, language breakdown via
`luxe/repo_survey.py`) and pick a task wall + `num_ctx` tuned to its
size tier:

| Tier   | Source LOC       | Task wall | `num_ctx` |
|--------|------------------|-----------|-----------|
| tiny   | < 500            | 30 min    | 8k        |
| small  | 500–2 000        | 45 min    | 8k        |
| medium | 2 000–10 000     | 60 min    | 24k       |
| large  | 10 000–50 000    | 90 min    | 24k       |
| huge   | 50 000+          | 120 min   | 32k       |

Medium / large run at 24k ctx after observing a real review burn ~33k
cumulative prompt tokens across 16 tool calls in one subtask at the
prior 16k budget — which meant Ollama was silently dropping older
messages (no warning in the log) whenever the agent's running context
hit the cap. 24k on `qwen2.5:32b` Q4_K_M adds ~5 GB of KV cache over
16k; acceptable on 64 GB unified memory.

The chosen decision prints at plan time:
`repo survey: 17 python source file(s) · 7,797 LOC · medium → 60 min
wall, 24k ctx`. Per-subtask overrides on top: the synthesis subtask
gets a doubled `max_tokens_per_turn` so the severity-grouped report
doesn't truncate mid-category. The `code` agent has the same
self-sizing hook — dispatching in a medium+ cwd bumps its own
`num_ctx`/`max_wall_s` for that turn.

**Per-agent wall budgets** on `review` / `refactor` / `code` are
1500 s (vs. the 600 s default). With one mid-run tool-depth retry and
a ~1 MB repo, completing an inspection subtask has been observed in
the 700–900 s range; the 1500 s cap leaves headroom before a blocked
subtask triggers `/tasks resume`.

## Static-analysis tool surface

The `code`, `review`, and `refactor` agents can call real analyzers
as tools. A grep match is evidence; an analyzer finding is already
located, classified, severity-tagged, and paired with an upstream
rule URL. The per-category orchestrator hints nudge each inspection
subtask toward the right tool before it reaches for `grep`.

**Python:**

| Tool | Wraps | Use case |
|---|---|---|
| `lint` | `ruff check --output-format=json` | Style, unused imports, bugbear, bare-except. |
| `typecheck` | `mypy --output json` | Type errors, missing returns, unreachable code. |
| `security_scan` | `bandit -r -f json` | In-source security patterns (weak crypto, pickle, hardcoded creds). `min_confidence=MEDIUM` default filters noise. |
| `deps_audit` | `pip-audit --format json` | Known-CVE dependency audit on the live env or a requirements file. |
| `security_taint` | `semgrep --config p/python --json` | Source→sanitizer→sink taint reasoning for eval/exec/subprocess/pickle/SQL. Correctly ignores sandboxed or non-user-reachable sinks that `security_scan` would flag uniformly. |
| `secrets_scan` | `gitleaks detect --no-git --redact=100` | Hardcoded credentials (AWS/GCP/GitHub/Slack/Stripe tokens, private keys). Matches are redacted before reaching the model. |

**Cross-language:**

| Tool | Wraps | Requires |
|---|---|---|
| `lint_js` | `eslint --format json` | `package.json` + eslint (via `npm install --save-dev`) |
| `typecheck_ts` | `tsc --noEmit --pretty false --project <dir>` | `tsconfig.json` + typescript |
| `lint_rust` | `cargo clippy --message-format=json` | `Cargo.toml` + clippy toolchain |
| `vet_go` | `go vet ./...` | `go.mod` + Go toolchain |

Each tool returns a uniform `{findings: [...], count, note?}` JSON
payload capped at 150 items. Missing binaries or missing project
markers produce a helpful note (`"not a Rust project"`, `"ruff not
installed. uv sync --extra dev pulls it in"`) — the agent sees the
message and adapts instead of crashing.

`/tasks analyze <id>` after any review prints a per-subtask breakdown
of tool calls with wall-time, bytes emitted, and ok/total ratio,
plus an "adoption: analyzer X% · reader Y% · orientation Z%" summary
so you can see whether the prompt nudges are landing on real runs.

**Language gating.** On `/review` / `/refactor`, the pre-flight repo
survey records the repo's `language_breakdown` and threads the language
set into the dispatched agent's `AgentConfig`. `analysis.tool_defs`
then hides analyzers whose language family isn't represented — a
pure-Python repo never sees `lint_js`, `typecheck_ts`, `lint_rust`, or
`vet_go` in its tool prompt. `secrets_scan` is always exposed
(credentials appear in any language). An empty survey (unknown repo)
falls through to the full ten-tool surface. This shaves ~400 tokens
off the tool-description prompt per turn on single-language repos.

## Routing — heuristic pre-router

The router first runs a deterministic keyword/regex scorer
(`luxe/heuristic_router.py`) against the prompt. When the scorer is
confident — a clear path-token + code verb for `code`, a `draft` /
`essay` hit for `writing`, a short interrogative + factual noun for
`lookup`, etc. — it short-circuits the LLM router and returns the
decision directly. On ambiguous prompts (< 3 words, meta questions,
low-margin scores) it returns None and falls through to the LLM.

The scorer never picks `review` / `refactor` (command-driven, not
prompt-driven) or `general` (the residual — a miss means the LLM
decides). Session logs tag each decision `"source": "heuristic"` vs
`"llm"` with the confidence + score breakdown so you can replay
decisions offline.

Config knobs (`LuxeConfig`):

- `heuristic_router_enabled: bool = True` — turn off for pure-LLM
  routing (A/B testing, regression checks).
- `heuristic_router_threshold: float = 0.35` — minimum normalized
  margin between the top two agent scores before a short-circuit is
  allowed. Lower values short-circuit more aggressively at the cost of
  disagreement with the LLM on close calls.

## Pre-retrieval — traceback + import graph

When a subtask's title or an earlier subtask's result_text mentions
`path.py:LINE` style references (pasted pytest output, a Python
long-form traceback), the orchestrator resolves those paths and
pre-reads them into the agent's prompt before dispatch. This is the
oracle-style selectivity that the compression benchmark validated:
whole-file pre-reads help local coder models, summarised/outlined
context hurts them.

Seed paths are then **expanded via a lightweight Python import graph**
(`luxe/import_graph.py`): for each cited file, up to `max_files` total
first-hop neighbors (files it imports + files that import it) are
added to the pre-read block. The graph is an AST walk over all `.py`
files under the repo root, cached on a `max(mtime)` key so rebuilds
only happen when a file actually changes. One seed traceback → the
cited file plus its 2 most relevant neighbors, and the agent starts
turn 1 with the right module already in view.

Scope is Python only for now; TS / Go / Rust use the same pattern but
need per-language parsers and are deferred until the Python version
proves out.

## Tool surfaces per agent

| Agent | Tools |
|---|---|
| general | — (chat only) |
| lookup | `web_search` (snippets only — no fetch) |
| research | `web_search`, `fetch_url`, `fetch_urls` (parallel, up to 4 URLs) |
| writing | `read_file`, `list_dir`, `glob`, `grep`, `write_file`, `edit_file` — scoped to cwd |
| image | `draw_things_generate` |
| code | fs (read/write/edit) + `glob`/`grep`/`list_dir` + `bash` (allowlist) + `fetch_url` + 10 analyzers (see above) |
| review | read-only fs + 3 git tools + 10 analyzers |
| refactor | read-only fs + 3 git tools + 10 analyzers |
| calc | `create_tool` + tools auto-matched from the saved library (see `luxe/tool_library.py`) |

The writing agent is served by `llama-server` with a Python-signature
prelude injected into the system prompt — Gemma 3's native
function-call format is ```tool_code``` blocks (Python call syntax),
which luxe parses via `ast.literal_eval` back into structured
`ToolCall` values. Tool results round-trip as ```tool_output``` blocks
inside a user message to satisfy Gemma's strict user/assistant
alternation.

Code agent's bash allowlist: `cargo pytest go python python3 rustc
node npm pnpm yarn git ls pwd cat head tail echo wc`.

## Swapping models

Edit `configs/agents.yaml`. Each agent has a `model` field — any tag
that `ollama list` shows is valid. Per-agent HTTP endpoint overrides
(`endpoint: http://127.0.0.1:8080`) let a single agent point at
llama-server while the rest stay on Ollama.

Ad-hoc overrides via the REPL:

```
/model llama3.3-70b-4k:latest   # next turn only
```

Or via CLI:

```bash
luxe analyze ~/my-repo --model llama3.3-70b-4k:latest
```

## Sessions

Stored as append-only JSONL at `~/.luxe/sessions/<timestamp>-<slug>.jsonl`.
Each line records one turn (user / router / assistant / tool). Safe to
inspect, copy, or delete. The writing agent's session history is
replayed on multi-turn conversations with synthesized tool_code +
tool_output pairs so Gemma sees real file data across turns instead of
defending hallucinated prose.

### Pruning

Session files grow indefinitely. Two ways to trim:

- **Per-session** — `Session.prune(max_turns=200)` keeps the last N
  events and atomically rewrites the file (tempfile + rename, so a
  crash mid-prune can't wipe history).
- **Per-user** — `luxe clean --days N` deletes session files whose
  mtime is older than N days (default 7). Safe to run on a cron.

`read_all()` is also hardened against `OSError` (permission / unreadable
file) — it returns `[]` instead of crashing so a corrupt session never
takes the REPL down on resume.

## User preferences (`~/.luxe/`)

Separate from the repo-tracked `configs/agents.yaml`. Per-user state
managed through REPL commands but plain-text so you can edit or back
it up by hand.

| File | Written by | Read by |
|---|---|---|
| `memory.md` | `/memory` (opens `$EDITOR`) | Appended to every specialist's system prompt as `# User memory (persistent)` — capped at 2 000 chars |
| `bookmarks.json` | `/save <name>` | `/resume <name>` and `/sessions` |
| `aliases.yaml` | `/alias add` | Every line of input — expansion happens before routing |
| `sessions/` | Every turn | `luxe list`, `luxe resume`, `/resume`, `/sessions` |
| `history` | prompt_toolkit | `↑` / `↓` arrow keys in the REPL |
| `tasks/<task-id>/` | `/tasks <goal>` | `/tasks status`, `/tasks log`, `/tasks save` |

## Budgets & caches

**`AgentConfig.min_tool_calls`** — per-agent knob (in `configs/agents.yaml`)
that refuses to accept a "final answer" until the agent has made at
least N tool calls. When the model returns text with zero tool calls
and the threshold isn't met, the loop appends a nudge message
(`"You must use tools to ground your answer…"`) and gives the model
another step rather than returning early. `max_steps` still caps the
total retries, so a stuck agent aborts cleanly instead of looping
forever. Use for agents that must investigate before answering —
especially `review` and `refactor`, where speculative answers
without tool use are the main failure mode.

```yaml
# configs/agents.yaml
- name: review
  min_tool_calls: 3   # force at least 3 reads/greps before finalizing
  max_steps: 12
```

**`AgentConfig.num_ctx`** — optional per-agent Ollama context-window
override in `configs/agents.yaml`. Passed to the chat request as
`options.num_ctx` so you can raise the window for a single tool-heavy
agent without rebuilding a modelfile. Leave unset to inherit whatever
the server loaded (typically 8k for Ollama). Useful mainly for `code`
/ `review` / `refactor` when sessions accumulate large tool results.

```yaml
- name: code
  num_ctx: 16384   # more tool-result headroom
```

**Per-turn token cap — soft warning.** After every agent run, the
REPL shows a yellow `⚠ N turn(s) used ≥80% of max_tokens_per_turn`
line when a turn came close to the cap — a strong signal the
synthesis was probably truncated and the YAML budget should go up.
Task `end` events surface the same count so `/tasks tail` flags it
per subtask.

**`LUXE_CACHE_TTL_S`** — env var controlling TTL on the
`backend.py` caches (`_PARAMS_CACHE`, `_CTX_CACHE`). These back
`/context`, `/models`, and the banner; without a TTL they went stale
after a `/pull` unless `clear_caches()` was called explicitly.
Default: `300` seconds. Set to `0` to effectively disable caching
(entries expire on next access). `clear_caches()` still wipes
everything manually.

```bash
LUXE_CACHE_TTL_S=60 luxe   # refresh model metadata every minute
```

**Task-scoped tool cache.** `luxe/tasks/cache.py:ToolCache` memoizes
deterministic read-only tool calls across subtasks of a single Task
(read_file, list_dir, glob, grep, git_diff/log/show, the ten static
analyzers). Allocated fresh by the orchestrator on every `run()`;
mutations (`write_file`, `edit_file`, `bash`) and web fetches are
excluded. Each subtask's `end` event carries `cache_hits` /
`cache_misses`, and the task `finish` event rolls up the totals.

**Client-side tool-arg schema.** `luxe/agents/base.py:_validate_args`
checks each tool call's arguments against the declared JSONSchema
(`required` fields + primitive `type`s) before dispatching to the fn.
Rejections come back to the model as structured errors on the same
turn and increment `AgentResult.schema_rejects` →
`Subtask.schema_rejects`. Catches the common `grep` / `read_file`
without-required-field pattern a turn earlier than the old path.

## Benchmarking orchestrator changes

`scripts/bench_orchestrator.py` tracks orchestration-level performance
over time. The metrics that actually move between commits on a local-
inference setup aren't decode tok/s (set by the model); they're wall
time, tool calls, cache hits, schema rejects, and context-token spend.

```bash
# Backfill a baseline from a finished ~/.luxe/tasks run.
.venv/bin/python scripts/bench_orchestrator.py import <task-id> --label baseline

# Run a fresh review task against a specific repo, append to history.
.venv/bin/python scripts/bench_orchestrator.py run \
    "Review this repo for security and correctness" \
    --cwd /path/to/target --label post-fix

# Show the last 5 rows with per-metric deltas against the prior row.
.venv/bin/python scripts/bench_orchestrator.py show -n 5
```

History lands in `results/orchestrator_bench/history.jsonl`, one
record per row, stamped with the short git rev.

## Known limitations

- **Coding depth is limited by the model class.** `qwen2.5-coder:14b`
  (the `code` agent) reads 2–4 files per analysis — enough to catch
  structural issues, not enough for deep bug hunting. `review` and
  `refactor` moved to `qwen2.5:32b-instruct` and are guarded by a
  four-layer anti-fabrication check (shallow-inspection retry →
  orchestrator-run greps → `file:line` citation verification →
  finding-level pattern verification that greps each claimed code
  construct against the cited file). Real analyzers are also
  available as callable tools: `lint` (ruff), `typecheck` (mypy),
  `security_scan` (bandit), `deps_audit` (pip-audit), and
  `security_taint` (semgrep, with `p/python` taint rules so
  sandboxed eval and list-arg subprocess don't get false-flagged) —
  the agents are prompted to prefer them over regex when one applies.
  `/review` and `/refactor` pre-flight the target repo with
  `repo_survey.analyze_repo()` and size the task wall + num_ctx
  from the LOC tier (tiny=30m/8k → huge=120m/32k), so budgets
  track repo size instead of a hardcoded hunch. Still not a
  replacement for human review — treat agent output as a first pass,
  not a final verdict.
- **Writing agent uses llama-server.** It's a separate process — if
  port 8080 is unreachable, the writing agent 400s. `/context` shows
  the server's RSS and loaded context.
- **SIGTERM is only polled between subtasks.** If a subtask is
  mid-HTTP-call, that call completes before the subprocess notices an
  abort request. SIGKILL after 5 s grace is the hard-stop path.
- **Research agent won't beat a frontier LLM.** DuckDuckGo results
  are thinner than Google, and `qwen2.5:32b` sometimes defers to
  training data over fetched pages. For canonical facts, verify.
- **Draw Things port:** older versions default to 7860, newer to
  7859. Update `draw_things_url` in `configs/agents.yaml` if needed.

## Uninstall

```bash
bash daily_driver/install_luxe.sh --uninstall
```

Removes the launchd plist and the `luxe` symlink. Leaves models,
sessions, and logs in place (delete manually if you want: `~/.luxe`,
`~/luxe-images`, `~/Library/Logs/luxe`). `llama-server` is not
managed by install/uninstall — stop it manually with
`pkill -f llama-server` if needed.
