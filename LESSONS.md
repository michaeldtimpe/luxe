# Lessons

Non-obvious things I learned while building luxe. Mostly specific to
Apple-Silicon + Ollama + a 64 GB memory budget in April 2026; some will
date faster than others.

---

## Ollama tool-use is not uniform across models

Ollama's OpenAI-compatible `/v1/chat/completions` endpoint accepts
`tools=[…]` for every model — but whether the model actually emits
structured tool calls is model-dependent.

- **qwen2.5:*:instruct** — solid. Emits proper `tool_calls` field.
- **gemma3:\***  — refuses with `gemma3 does not support tools`
  regardless of Ollama version. Pure chat only.
- **qwen2.5-coder:\***  — officially supports tools, but often emits the
  call as raw JSON in the `content` field (no `<tool_call>` tag
  wrapper). Needs a fallback parser.
- **llama3.3:70b** — works, but OOMs on 64 GB when run at default
  32k context. A 4k-context variant via modelfile stabilizes it.
- **command-r:35b** — clean tool use, reasonable speed.

**Consequence:** the shared agent loop includes a text-JSON fallback
parser (copied from `personal_eval/agent_loop.py`) that recovers
tool calls from content. Without it, qwen-coder on Ollama is unusable.

---

## "Recover all text-parsed tool calls" is a footgun

First version recovered every text-embedded JSON tool call from one
response. qwen2.5-coder:32b once dumped a **63-call speculative plan**
in a single turn ("I will list_dir, then read X, then grep Y, …") —
which tripped the runaway-turn cap and aborted the task.

**Fix:** recover only the **first** text-parsed tool call per turn.
Models that legitimately want parallel reads can use structured
`tool_calls`; speculative text dumps contribute exactly one
productive step per turn rather than 63.

---

## Creating Ollama model variants via modelfile can lose stop tokens

```
FROM qwen2.5-coder:32b-instruct
PARAMETER num_ctx 8192
```

This inherits the base model's **template** but leaves `PARAMETER stop`
empty. Result: the model generates past `<|im_end|>` into the next
turn's `<|im_start|>`, and you see raw chat-template tokens in the
assistant response.

**Fix:** include stops explicitly.

```
FROM qwen2.5-coder:32b-instruct
PARAMETER num_ctx 8192
PARAMETER stop "<|im_end|>"
```

Even with that, qwen2.5-coder:32b on Ollama remained flaky over
multi-turn tool-heavy sessions. Moving to `qwen2.5-coder:14b-instruct`
resolved it at the cost of analysis depth.

---

## Draw Things has two API modes on the same port

The app exposes a gRPC server by default and a stable-diffusion-webui-
compatible HTTP server as a setting. Both can be on the same port
(7859 in recent versions, 7860 in older). When you toggle between
modes the port may move silently.

**Lessons:**
- Don't hardcode the port — `draw_things_url` lives in `agents.yaml`.
- On startup, health-check `/sdapi/v1/options` before claiming the
  image agent is usable.
- Avoid the gRPC path if you can — the proto is GPL-v3, which
  complicates licensing for any project that wants to vendor it.
  HTTP mode sidesteps that entirely.

---

## `ollama pull` tags sometimes need the suffix, sometimes don't

- `ollama pull mistral-small:24b-instruct-2501` → "pull model manifest:
  file does not exist"
- `ollama pull mistral-small:24b` → works

Model tags on Ollama's registry move around. Always `ollama list` after
a pull to see what the canonical name is; use that everywhere,
including in YAML configs.

**Related:** variants created with `ollama create <name> -f Modelfile`
store as `<name>:latest`, so config entries need `my-variant:latest`,
not just `my-variant`.

---

## Context window vs. throughput on a 64 GB Mac

The default Ollama context for a 32B model is 32k. That means a huge
KV cache gets allocated per request even when a conversation is 4k
tokens long. On a memory-constrained Mac that translates to pages
swapping and ~7 min per turn for qwen2.5-coder:32b.

Trade-offs I settled on:
- **Routing** (small prompts, small outputs): use the default context.
  The overhead is small because the model is small (7B).
- **Research / writing** (medium context): default is fine for 32B.
- **Code analysis** (can accumulate many tool results): even 32B with
  a reduced 8k–16k context window pays its cost in quality degradation
  once tool results start getting evicted. 14B with default context
  was better than 32B with limited context.

**Rule of thumb:** for tool-heavy agents, scale down the model class
before scaling down the context window.

---

## Small models confidently state false things

The research agent's most dangerous failure mode is confident
hallucination — e.g. `qwen2.5:7b-instruct` saying PostgreSQL 15 is the
latest when the snippet it just fetched shows 17 and 18. The model
defaults to training-cutoff knowledge over live sources.

**Mitigations built in:**
- System prompt explicitly says: *"Do NOT invent URLs, quotes, or facts
  absent from your sources."*
- The citation format forces the model to attach a source to each
  factual claim. Misattributions are at least **detectable** downstream.

**Mitigation I did not build:** a second "fact-checker" pass that
verifies each citation claim actually appears in the cited text.
Worth doing if research quality matters.

---

## The coding agent's bugs are often true of its own shallow reading

Across 4 real personal repos, `qwen2.5-coder:14b` made 2–4 tool calls
per analysis — typically `list_dir` + one `read_file` of the README —
and then speculated about the rest. Hallucinated bug examples:

- "`.env.example` has a hardcoded API key" → it has a placeholder.
- "SQL injection risk in db.py" → db.py uses parameterized `?` queries.
- "`docker-compose.yml` doesn't check env vars" → standard `env_file: .env` pattern.

Stronger exploration-protocol prompts (require N tool calls before
concluding) improved flying-fair's refactor accuracy (it correctly
identified duplicated `ALL_MARKETS` with the right 5-tuple shape) but
didn't help as much on other repos.

**Takeaway:** local-LLM code review is a first-pass filter, not a
deliverable. Always human-review. The genuinely good catches from the
agent across 4 repos:
- Duplicated `ALL_MARKETS` dict (flying-fair)
- 3 near-identical config.json files (nothing-ever-happens)

Those are real refactor targets. The rest was noise.

---

## Write persistence append-only, not read-then-write

Sessions land in JSONL with one event per line. Any crash mid-agent
leaves the prior events intact and only costs you the in-progress
turn. If sessions were a single JSON document that you
read-modify-write, a crash would corrupt the whole file.

Simple and worth the tiny extra I/O.

---

## Ollama and llama-server are perf-equivalent on Q4_K_M Qwen2.5

Suspected llama-server might be measurably faster than Ollama for
luxe's hot-path Qwen2.5 models. A/B'd `qwen2.5:7b-instruct`,
`qwen2.5-coder:14b-instruct`, and `qwen2.5:32b-instruct` on the same
Q4_K_M weights via the harness — three fixed-prompt decode runs each
through both servers. Decode tok/s landed within ±4% on every model;
TTFT within ±3%. Both backends hit Metal kernels on identical
weights, so identical work = identical wall.

Peak RSS *looked* different (32B: llama-server 25.7 GB vs Ollama
38.2 GB on a 19 GB weights file), but the methodology is suspect —
the sampler walks the listening process's tree and likely
double-counts Ollama's runner subprocess where mmap'd GGUF pages
appear in both parent and child. Don't trust the RSS column without
cross-checking against `htop` or Activity Monitor.

**Decision:** stay on Ollama everywhere except Gemma 3 (which already
lives on llama-server because Ollama refuses tools for it). The
migration cost — per-model server lifecycle, port management, losing
Ollama's auto-keep-alive eviction — isn't justified by zero
throughput improvement. Re-test if a future Ollama version regresses
or if a model class shows up that one backend handles materially
better.

Suite lives at `luxebox/scripts/run_ab_full.py`; raw numbers under
`luxebox/results/ab_ollama_vs_llamacpp/REPORT.md`.

---

## Permission prompts shape your design

Claude Code's permission system blocked cloning Draw Things' community
repo to extract the gRPC proto, flagging it as "scope escalation" and
"external code integration." That nudge pushed me toward the HTTP API
path, which turned out to be simpler, better-licensed, and more
maintainable anyway. Not always right, but worth listening to.
