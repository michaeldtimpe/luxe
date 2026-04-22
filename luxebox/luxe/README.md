# luxe — local multi-agent Claude-Code-alike

A terminal REPL that takes a prompt, routes it to a specialist agent, and
runs fully on your Mac via Ollama + Draw Things. Six roles share a single
router: **general**, **research**, **writing**, **image**, **code** (music
deferred).

## Status

| Phase | Agent | Model | Notes |
|---|---|---|---|
| 0 | scaffolding | — | REPL + session persistence at `~/.luxe/sessions/` |
| 1 | router | `qwen2.5:7b-instruct` | Single hand-off; max 2 clarifying Qs |
| 2 | general | `qwen2.5:7b-instruct` | Concise chat; 1.7 s avg |
| 3 | research | `qwen2.5:32b-instruct` | DuckDuckGo + `trafilatura` extract, cited output |
| 4 | writing | `gemma3:27b` | Best voice across 7 candidates |
| 5 | image | `qwen2.5:7b-instruct` (prompt expander) + Draw Things | HTTP API on 7859 |
| 6 | code | `qwen2.5-coder:14b-instruct` | Full tool surface; 32b had ollama tool-use quirks |
| 7 | polish | — | Token/time stats, Ctrl-C, session resume |
| 8 | install | — | launchd plist for Ollama, `install_luxe.sh` |

Disk footprint for the selected models ≈ 80 GB. Ollama hot-swaps, so only
one model is resident at a time.

## Prereqs

- macOS with Homebrew
- `brew install ollama uv ripgrep`
- [Draw Things.app](https://drawthings.ai/) — enable **HTTP server** in the
  app's settings (default port 7859 in newer versions; 7860 in older)
- For research: no extra setup — DuckDuckGo runs in-process via `ddgs`

## Install

```bash
cd ~/Downloads/local-llm/luxebox
uv sync
bash daily_driver/install_luxe.sh
```

That:

- Symlinks `luxe` to `~/.local/bin/luxe`
- Installs a launchd plist so `ollama serve` runs at login
  (`~/Library/LaunchAgents/com.luxebox.ollama.plist`)
- Verifies Ollama is reachable on `127.0.0.1:11434`

Pull the selected models on first use (they'll pull on demand too):

```bash
ollama pull qwen2.5:7b-instruct
ollama pull qwen2.5:32b-instruct
ollama pull gemma3:27b
ollama pull qwen2.5-coder:14b-instruct
```

## Use

```bash
luxe                       # REPL — type a prompt, router picks an agent
luxe list                  # list saved sessions
luxe resume                # continue most recent session
luxe --session <id>        # resume a specific session
luxe agents                # show configured agents + models
luxe analyze <path>        # one-shot read-only code review on a repo
```

Example REPL session:

```
$ luxe
╭───────────────────────────────────────────────────────╮
│ luxe — local multi-agent CLI                          │
╰───────────────────────────────────────────────────────╯
luxe> what is the difference between concurrency and parallelism?
→ routed to general (The user is asking a definitional question about computer science concepts.)
Concurrency is about *dealing* with multiple tasks at once…
general · 2.4s · 210↑ 52↓ tokens · 1 steps · 0 tool calls
ctx: 210/32,768 (99% free) · qwen2.5:7b-instruct
session totals: 1 turns · 2.4s · 210↑ 52↓ tokens

luxe> /writing a haiku about the ocean
→ routed to writing (direct /writing flag)
The gulls turn seaward…

luxe> /pin respond in British English
pinned #1: respond in British English

luxe> /save ocean-stuff
saved bookmark ocean-stuff → 20260421T194000
```

## REPL commands

Everything below the banner is the REPL. Anything that doesn't start with
`/` gets routed; `/`-prefixed tokens are commands or direct-dispatch flags.

**Core**

| Command | Notes |
|---|---|
| `/help` | Show the command cheatsheet |
| `/agents` | List configured agents and their models |
| `/models` | List Ollama models currently available locally |
| `/quit` (or `/exit`, Ctrl-D) | Exit; session auto-saves |

**Direct dispatch** — bypass the router:

```
/general   <prompt>
/research  <prompt>
/writing   <prompt>
/image     <prompt>
/code      <prompt>
```

**Turn control**

| Command | Notes |
|---|---|
| `/retry` | Rerun the last prompt with the same agent |
| `/redo <agent>` | Rerun the last prompt with a different agent |
| `/model <tag>` | One-off model override for the next turn (e.g. `/model llama3.3-70b-4k:latest`) |
| `/pin <text>` | Prepend a sticky note to every subsequent prompt |
| `/pins` | List current pins |
| `/unpin [n]` | Remove pin #n (default: clear all) |
| `/history [n]` | Show the last n session events (default 10) |

**Sessions** (see [Sessions](#sessions) for storage details)

| Command | Notes |
|---|---|
| `/session` | Show current session id + path |
| `/save <name>` | Bookmark current session under `<name>` |
| `/sessions` | List saved sessions (bookmarks first, then recent) |
| `/resume <id-or-name>` | Switch to another session (accepts bookmark name, full id, or unique id prefix) |
| `/new` | Start a fresh session (reset totals + pins) |

**Memory & aliases** (persisted in `~/.luxe/` — see below)

| Command | Notes |
|---|---|
| `/memory` | Open `~/.luxe/memory.md` in `$EDITOR` |
| `/memory view` | Print current memory |
| `/memory clear` | Delete memory |
| `/alias add <name> <expansion>` | Define `/<name>` as a shortcut (e.g. `/alias add q /research quick lookup:`) |
| `/alias list` | List aliases |
| `/alias remove <name>` | Remove an alias |

After every turn, luxe prints three lines: the turn stats, a `ctx:` line
showing how much of the model's declared context window the prompt used,
and a running session total (turns, wall time, tokens).

## Swapping models

Edit `configs/agents.yaml`. Each agent has a `model` field — any tag that
`ollama list` shows is valid. Ad-hoc overrides via CLI:

```bash
luxe analyze ~/my-repo --model llama3.3-70b-4k:latest
```

Eval scripts run a model against a canned set of prompts:

```bash
uv run python scripts/run_luxe_eval.py router
uv run python scripts/run_luxe_eval.py general --all
uv run python scripts/run_luxe_eval.py research
uv run python scripts/run_luxe_eval.py writing --all
```

Reports land in `results/luxe_eval/<agent>/<model>.md`.

## Tool surfaces per agent

| Agent | Tools |
|---|---|
| general | — (chat only) |
| research | `web_search`, `fetch_url` |
| writing | — (chat only, higher temperature) |
| image | `draw_things_generate` |
| code | `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `list_dir`, `bash` (allowlist), `fetch_url` |

Code agent's bash allowlist: `cargo pytest go python python3 rustc node npm pnpm yarn git ls pwd cat head tail echo wc`. Scoped to the CWD where you launched `luxe` (or the `--repo` arg to `luxe analyze`).

## Sessions

Stored as append-only JSONL at `~/.luxe/sessions/<timestamp>-<slug>.jsonl`.
Each line records one turn (user / router / assistant / tool). Safe to
inspect, copy, or delete.

## User preferences (`~/.luxe/`)

Separate from the repo-tracked `configs/agents.yaml`. Everything here is
per-user state, managed through REPL commands but plain-text so you can
edit or back it up by hand.

| File | Written by | Read by |
|---|---|---|
| `memory.md` | `/memory` (opens `$EDITOR`) | Appended to every specialist's system prompt as `# User memory (persistent)` — capped at 2 000 chars |
| `bookmarks.json` | `/save <name>` | `/resume <name>` and `/sessions` — maps friendly names to session ids |
| `aliases.yaml` | `/alias add` | Every line of input — if the first token matches an alias, it expands before routing |
| `sessions/` | Every turn | `luxe list`, `luxe resume`, `/resume`, `/sessions` |

Memory is the simplest way to inject persistent preferences ("be terse",
"prefer Rust examples") without editing YAML. Keep it short — it's loaded
fresh on every turn, so it costs context tokens each time.

## Known limitations

- **Coding depth is limited by the model class.** `qwen2.5-coder:14b` reads
  2–4 files per analysis, which is enough to catch structural issues
  (duplicate configs, consolidation opportunities) but not enough for
  deep bug hunting. Think of it as a code reviewer's *first pass*, not a
  final verdict — always human-review the output.
- **qwen2.5-coder:32b on Ollama is flaky** with multi-turn tool use —
  either too slow at default 32k context or leaks `<|im_start|>` tokens
  at reduced context. `llama3.3-70b-4k` or `command-r:35b` are worth
  trying for deeper analysis — slower but cleaner tool use.
- **Draw Things port:** older versions default to 7860, newer to 7859.
  If your config is wrong, `luxe` gets a connection error — update
  `draw_things_url` in `configs/agents.yaml`.
- **Research agent won't beat a frontier LLM** — DuckDuckGo results are
  thinner than Google, and `qwen2.5:32b` sometimes defers to training
  data over fetched pages. For canonical/current facts, verify.

## Uninstall

```bash
bash daily_driver/install_luxe.sh --uninstall
```

Removes the launchd plist and the `luxe` symlink. Leaves models, sessions,
and logs in place (delete manually if you want: `~/.luxe`, `~/luxe-images`,
`~/Library/Logs/luxebox`).
