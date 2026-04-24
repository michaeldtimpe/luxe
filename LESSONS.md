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

## A tool-shy review agent fabricates more than it finds

A 2026-04-23 `/review` run on a tiny HTTP-server repo made the coder
model's failure mode concrete. Seven subtasks; `qwen2.5-coder:14b` did
one `list_dir` on subtask 1 and then emitted a "critical SQL injection
in `server.py`" finding — complete with a quoted `query = f"SELECT *
FROM users..."` that does not exist anywhere in `server.py`. The file
is an `http.server` handler with zero database code. Subtasks 3–6 did
not call a tool at all; the final "severity-grouped report" reprinted
the hallucinated SQLi as the headline critical issue.

Three pathologies stacked:

1. **Model prefers plausible output over refusal.** Given a
   structured-finding prompt, a 14B coder-instruct fills in the shape
   of a bug report whether or not the data supports it.
2. **Forced-inspection greps are only as good as their precision.** The
   `secrets` recipe matched `secrets.token_hex(8)` — Python's *safe*
   RNG API — and a tool-shy model happily wrote it up as a medium-sev
   "potential exposure."
3. **Planner-decomposed subtasks don't self-scope.** Subtask 1's
   title was "List the root directory," but the agent saw the full
   `/review` goal text and produced a full security review from one
   `list_dir` call. Orientation, inspection, and synthesis subtasks
   all looked like inspection subtasks to the model.

Fix was layered, not a single knob:

- **Model swap** for review/refactor from `qwen2.5-coder:14b-instruct`
  to `qwen2.5:32b-instruct`. These agents reason over code rather than
  writing it, and general-instruct 32B is both less eager to fabricate
  and already proven stable on Ollama tool use (research + calc).
- **Per-subtask scope notes** injected by the orchestrator for
  review/refactor agents: orientation subtasks are told not to produce
  findings; synthesis subtasks are told not to introduce new ones;
  inspection subtasks are scoped to one category.
- **Shallow-detection citation check.** If the final text cites ≥4
  `file:line` pairs from fewer than 2 reading-tool calls, the shallow
  retry kicks in even if the agent called `read_file` once for show.
- **Orchestrator-run grep exclude patterns.** `secrets.token_*`,
  `secrets.SystemRandom`, `os.urandom` get stripped from the secrets
  panel before the model sees it. Empty panels must emit a single
  "No findings in this category." line and nothing else.
- **`file:line` citation verification.** After every review/refactor
  subtask, the orchestrator re-reads each cited location and confirms
  the file exists and the line is in range.
- **Finding-level pattern verification.** Parses each `**File:** ... /
  **Why:** ...` finding block, extracts backtick-quoted code tokens
  from the claim text (skipping `**Suggested fix:**` so recommendations
  aren't treated as claims), and greps the cited file for each. A
  fabricated "os.system call in server.py" — the 32B follow-up's
  dominant failure mode — gets flagged because `grep 'os.system'
  server.py` → 0 matches. False claims are marked with a
  `⚠️ Grounding check failed` block prepended to the subtask output
  so the synthesis pass and the reader both see what to distrust.

**Broader takeaway:** when a small model fabricates, adding more
prompt discipline gets you a diminishing return. The model-swap + post-
hoc verification buys more than any amount of "please cite only real
files" in the system prompt. Verify in code what you can't trust the
model to self-report.

**Follow-up finding from a second rerun on a mid-sized repo** (`elara`,
2026-04-23): the grounding check correctly did NOT fire because every
citation pointed at real code. The remaining failure mode is subtler
— *cherry-picked-true claims with wrong severity*:

- `eval(expression, {"__builtins__": {}}, ns)` at `elara_task.py:385` —
  flagged High for "remote code execution." `eval` IS there, but the
  restricted globals + math-only namespace make it a hardened
  expression evaluator, not a RCE sink.
- `subprocess.run(["ps", "aux"], ...)` at `elara_kill.py:98` — flagged
  Medium for "command injection." List-args to `subprocess.run` are
  NOT shell-injection vectors; only `shell=True` or user-controlled
  string args are.
- `while True:` on a worker's queue drain at `elara_memory.py:179` —
  flagged High for "infinite loop." The loop breaks on a None
  sentinel; it's a normal worker idle loop.
- `for name in dir(mod):` at `elara_task.py:234` — flagged Medium for
  "unbounded loop." `dir()` returns a finite list.

These aren't substring hallucinations; they're shallow pattern-match
severity. A programmatic check can't easily catch them — verifying
exploitability needs data-flow tracing, which is beyond the model's
reliable reach and beyond what a ~200-line orchestrator helper can
do. Partial mitigation added in configs/agents.yaml: a
severity-validity checklist that spells out each of these traps
explicitly so the model has to consider the mitigation before
emitting the finding. Effective in testing; not a substitute for a
human reading the code.

Second lesson from the same run: a 32B review agent on a mid-sized
repo (~15 Python files) spent 13+ min on single inspection subtasks
(3 sequential grep/read cycles, ~1.5 K output tokens). The original
30-min task wall budget ran out after 5 of 7 subtasks — synthesis
pass was skipped. Bumped `/review` and `/refactor` task wall to 60
min (`luxe/repl/review.py`, `luxe/review.py`). Per-subtask agent
wall stays at 15 min.

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
