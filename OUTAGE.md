# OUTAGE — Anthropic is down. Do this.

Offline emergency card. Print it with `luxe outage` (or `/outage` in a session).
No network needed to read it, no network needed to run anything on it except
`luxe update` and `luxe pull`.

## 1. Can I work right now?

```
luxe ready              # host preflight, seconds, no model. exit 0 = go
luxe ready --backend m5 # judge another host's endpoint instead
```

Every `✗`/`!` line prints a runnable `→ fix` under it. Do those first.
Then start:

```
luxe chat               # anywhere. read-only, conversation. --repo resolves up to the git root
luxe code               # REQUIRES a project. write tools ON from turn one
luxe-chat / luxe-code   # dotfiles wrappers; they pass --repo "$PWD" for you
```

Useful startup flags (both commands): `--repo <dir>` · `--backend <name>` ·
`--dev` (write + unrestricted bash on) · `--web` · `--ctx <tier>` ·
`--resume <id>` · `--model`-per-slot via `--chat-model/--plan-model/--code-model`.

## 2. Gates — what is off by default and how to turn it on

| gate | default | command | unlocks |
|------|---------|---------|---------|
| write | OFF (`luxe chat`) / ON (`luxe code`) | `/write` | `write_file`, `edit_file`, `bash` |
| bash | allowlisted | `/bash` | unrestricted shell (chains, pipes, redirects); needs write mode |
| web | OFF | `/web` | `web_fetch`, `web_search`, `web_answer` (search/answers need keys) |
| ctx | role default | `/ctx small\|medium\|large\|xlarge\|huge` | bigger context window, clamped to the model's ceiling |
| MCP | none | startup only: `luxe chat --mcp <name>` | namespaced `mcp__<server>__<tool>` |

A read-only session honestly reports it has no file-creation tool. That is the
gate, not a missing feature — `/write`.

## 3. Per-host cheat sheet

| host | RAM | interactive main | fallback | also cached |
|------|-----|------------------|----------|-------------|
| m5 | 128 GB | `Qwen3.6-35B-A3B-6bit` | `Qwen3.6-27B-6bit` | `GLM-4.5-Air-4bit` |
| m1 | 64 GB | `Qwen3.6-35B-A3B-4bit` | `Qwen3.6-27B-4bit` | `Qwen3.6-35B-A3B-6bit` (bench champion) |
| m4 | 48 GB | `Qwen3.6-35B-A3B-4bit` | `Qwen3.6-27B-4bit` | — |

- Capacity over speed, m5 only: `/model all GLM-4.5-Air-4bit` (~2× the wall clock).
- Weak host, strong model: `luxe chat --backend m5` (needs `OMLX_API_KEY_M5` + the tailnet).
- Main missing or failing? The session auto-degrades to the fallback and says
  so — in the status line, in `/doctor`, and in `debug.log`.

## 4. Recovery

```
luxe ready                       # what is actually broken
luxe pull --list                 # local models; flags DANGLING store entries
luxe pull <model>                # mount (kappa/alpha) first, HuggingFace second
luxe pull <model> --remove       # free disk; refuses manifest models without --force
luxe unload                      # free oMLX RAM
brew services restart omlx       # after provisioning, or when the build is stale
luxe update                      # fetch → rebase onto origin/main → uv sync
luxe smoke                       # real generation drill: weights → endpoint → turn → tool call
luxe smoke --chat --code         # agentic drills in a planted scratch repo
luxe smoke --backend m5          # drill a remote host's manifest from here
luxe net                         # DNS → TCP → TLS → HTTP ladder + every backend
```

**Dangling weights** (the HF-cache-wipe signature): the model is listed but the
server cannot load it. `luxe pull --list` and `luxe ready` both flag it —
`luxe pull <model>` re-provisions.

**Stale oMLX**: a server left running across a `brew upgrade` executes from a
deleted Cellar tree. It passes health and lists its catalog, then fails a lazy
import with a bogus `ModuleNotFoundError` / `[Errno 2]`. `luxe ready` warns on
it; `brew services restart omlx` fixes it.

## 5. Forensics — every session leaves a trail

```
~/.luxe/sessions/<id>/debug.log        # always-on DEBUG log; tool dispatch, retries, ctx%
~/.luxe/sessions/<id>/transcript.jsonl # turns, incl. kind="error" records for failed ones
~/.luxe/sessions/<id>/transcript.md    # after /export
~/.luxe/runs/<run_id>/events.jsonl     # per-step tool_call events
```

Headless diagnosis (no TUI, pipes fine into a log):

```
printf 'your question\n/quit\n' | luxe chat --repo <dir>
```

In-session: `/doctor` (preflight) · `/status` (one-screen dump) · `/tools`
(the real tool surface + what gates each) · `/net` · `/export` · `/diff`.

## 6. If luxe itself is broken

```
cd ~/Downloads/luxe && uv sync --extra chat --extra dev --extra analyzers
uv run pytest -q
git log --oneline -5          # what changed last
```

Textual missing → the line REPL is the automatic fallback; `uv sync --extra chat`
restores the TUI.
