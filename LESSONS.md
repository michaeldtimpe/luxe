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

## Let the model orchestrate real tools, don't make it re-derive them

The elara run also made a third thing clear: 32B re-deriving bare-except
/ unused-import / lint findings with `grep` is wasted compute. Every
structured lint finding invented via regex is a finding we'd have
gotten cheaper, more precisely, and without severity-mischaracterization
risk from a real analyzer that's already installed.

First step taken: add `lint` as a proper callable tool for the code,
review, and refactor agents. It wraps `ruff check --output-format=json`
via `luxe/tools/_subprocess.py:run_binary` (new shared helper that
unifies the subprocess pattern previously duplicated between
`fs.grep` and `git_tools._run`), returns structured
`{file, line, column, code, message, url}` records, and caps at 150
findings. The agent system prompts now explicitly prefer `lint` over
greps for lint-category patterns.

Binary resolution: `run_binary` looks up analyzer binaries in the
current venv's `bin/` before falling back to system `PATH`. `ruff`
and friends install via `uv sync --extra dev` into `.venv/bin/`,
which isn't on the shell's PATH when luxe runs as a daemon — the
lookup order handles that uniformly.

Follow-ups: `typecheck` (mypy), `security_scan` (bandit), `deps_audit`
(pip-audit) landed in the same shape — each tool wraps its binary with
`run_binary`, reshapes output into a `{findings: [...], count, note?}`
payload, and caps at 150 items. Defaults are tuned to reduce noise —
`security_scan` filters LOW-confidence by default (MEDIUM+), since
bandit's highest-volume lints are usually import-site (`B404`) and
subprocess-call (`B603`) notes that are "real but known-and-accepted"
for most codebases. Remaining planned: `security_taint` (semgrep) and
`secrets_scan` (gitleaks). LSP-grade taint (Pysa, CodeQL) stays out of
scope; semgrep + rulesets cover the 80% case we care about.

**Broader principle:** when a deterministic tool exists, the model
should call it and summarize. Re-deriving it with greps is
proof-by-vibes. Token budget spent on real tool output is spent on
something verifiable.

---

## Size the budget from the repo, not from a hunch

The earlier "30 min too tight → 60 min" bump was a reactive edit: the
elara run blew the wall, so the wall got bigger. But 60 min is too
generous for zoleb (tiny single-file repo) and would be too tight for
a 20k-LOC monorepo. A budget that doesn't look at the target is
always wrong in one direction.

Replaced the hardcoded `Task(max_wall_s=3600.0)` in both /review
entrypoints with a pre-flight repo survey. `luxe/repo_survey.py`
walks the clone (skipping `.git`, `.venv`, `node_modules`, `target/`,
etc.), counts source files by extension, sums LOC, and maps to a
tier:

| Tier   | LOC             | task wall | num_ctx | Source basis                |
|--------|-----------------|-----------|---------|-----------------------------|
| tiny   | <500            | 30 min    | 8k      | matches zoleb (769 LOC=small) |
| small  | 500–2 000       | 45 min    | 8k      |                              |
| medium | 2 000–10 000    | 60 min    | 24k     | elara (7 797 LOC)            |
| large  | 10 000–50 000   | 90 min    | 24k     | luxe itself (7 822 LOC=medium) |
| huge   | 50 000+         | 120 min   | 32k     | watch KV-cache RAM          |

Medium / large bumped from 16k → 24k after the 2026-04-24 elara rerun:
one inspection subtask logged ~33k cumulative prompt tokens across 16
tool calls, meaning the per-turn context kept pressing against the
16k cap and Ollama was silently dropping the oldest messages (no log
signal — the agent just quietly "forgot" earlier reads). 24k at
qwen2.5:32b Q4_K_M costs ~5 GB more KV cache than 16k; acceptable on
64 GB unified memory.

Numbers grounded in two sources: the A/B decode data in
`results/ab_ollama_vs_llamacpp/REPORT.md` (qwen2.5:32b ≈ 7.6 tok/s,
so ~1500-token subtask output ≈ 3.3 min pure decode) and the 2026-
04-23 elara run's observed 13-min/subtask inspection cost.

Also added `Task.num_ctx_override` — the tier's ctx value threads
through the orchestrator via `AgentConfig.model_copy(update={...})`
without touching the static YAML defaults. Only applies to
review/refactor agents; other specialists keep their configured ctx.

Printed rationale at plan time so the sizing is visible and
challengeable:

    repo survey: 17 python source file(s) · 7,797 LOC · medium → 60 min wall, 24k ctx

**Principle:** static config is the wrong place for a decision that
depends on the input. If you can compute the right value at task
spawn, do that instead.

---

## Semgrep closes the loop on severity mischaracterization

The elara rerun flagged `eval(expression, {"__builtins__": {}}, ns)`
at `elara_task.py:385` as High-severity RCE. That's a pattern-match
error — the sandbox + allowlist namespace is a real mitigation, but
bandit and grep both report it as-is without tracing data flow. The
earlier severity-validity checklist and per-category nudges moved
the needle but didn't fully solve it: the model still has to reason
about exploitability from the code.

Added `security_taint` (semgrep, `p/python` ruleset) as a callable
tool. Verified on two fixtures:

- `elara/elara_task.py:385` (sandboxed eval) → **0 findings**.
  Semgrep's taint rules correctly see the globals/locals restriction
  and ignore this site. The model was right to notice `eval`; it
  was wrong to call it High.
- Synthetic `subprocess.run(request.args.get("cmd"), shell=True)` →
  3 findings at ERROR/HIGH-or-MEDIUM confidence
  (subprocess-injection, dangerous-subprocess-use,
  subprocess-shell-true). Taint path from `request.args` to the
  sink is explicit.

The severity-validity checklist now says: for eval/exec/subprocess/
pickle/SQL patterns, call `security_taint` before assigning
severity. If semgrep doesn't flag the site, the sink is either
sandboxed or not user-reachable — downgrade or drop, don't guess.

Semgrep pulls ~100 MB of Python packages + a lazy rules cache.
First run downloads the ruleset from the registry; subsequent runs
are offline. Graceful-degrade path (`(None, "semgrep not installed.
…")`) means reviews without semgrep still work via the other four
analyzers.

**Principle for the broader toolbox:** when a tool implements the
specific reasoning you want, use it instead of prompting the model
to reason. Data-flow analysis is a solved problem for simple Python
patterns; the model's value-add is orchestrating the analyzer and
synthesizing across categories, not re-inventing taint tracking.

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

Suite lives at `luxe/scripts/run_ab_full.py`; raw numbers under
`luxe/results/ab_ollama_vs_llamacpp/REPORT.md`.

---

## Permission prompts shape your design

Claude Code's permission system blocked cloning Draw Things' community
repo to extract the gRPC proto, flagging it as "scope escalation" and
"external code integration." That nudge pushed me toward the HTTP API
path, which turned out to be simpler, better-licensed, and more
maintainable anyway. Not always right, but worth listening to.

---

## On this hardware, "compression" means retrieval, not summarization

A 70-run compression benchmark (7 strategies × qwen2.5-coder:14b and
:32b × 5 bugfix tasks, ctx=4096) measured what actually helps local
coder models fix bugs in a small fixture repo. The clean result: the
only compression that works is *being more selective about which files
to include*. Compressing the *contents* of selected files is a
regression.

Numbers (raw JSONL under `luxe/results/runs/compression_strategies/`):

- **retrieve_oracle** (exactly the relevant files): 90% pass, 496
  prompt tokens on average.
- **retrieve_full** (every file in the repo): 100% pass, 3 424 prompt
  tokens — 7× more context for 10 pct-points more pass rate.
- **file_outline_only** (AST signatures + docstrings, no bodies):
  **10% pass** at 1 237 tokens. The model can't fix a bug from a
  signature.
- **retrieve_then_summarize** (LLM-summarised top-k files): **30%
  pass** at ~780 tokens — tied with "no retrieval at all" despite
  costing more context. Summaries strip the exact syntactic content
  the model needs to reproduce in its whole-file rewrite.
- **stack_trace_guided** (parse pytest's traceback, seed retrieval
  from the `path.py:LINE` mentions): 80% pass at 2 031 tokens — sits
  on the efficient frontier between oracle and full.

The format axis also mattered: whole-file rewrites (`# FILE: path\n
<body>`) got 100% apply rate vs. 12% for unified diffs on the same
prompts, because qwen2.5-coder produces correct code contents but
systematically wrong `@@ -a,b +c,d @@` counts.

**Consequences for luxe:**

1. Keep the on-demand tool-based retrieval pattern. The code/refactor/
   review agents read raw files via `read_file`; no pre-filter, no
   summarization, no outline pass. Validated by data.
2. The orchestrator pre-reads files cited in pasted tracebacks
   (`_augment_with_trace_hints` in `luxe/cli/tasks/orchestrator
   .py`) — this is the one positive transfer from the benchmark.
   Oracle-style retrieval when the user has already named the file.
3. **Do not add** a file-summarisation pass, AST outlining, or
   LLM-based context compression to any code-editing path. The data
   is loud: -50 to -70 pct pass rate vs. raw content.

Shared trace parser at `luxe/shared/trace_hints.py` is used by
both the orchestrator and the benchmark's `stack_trace_guided`
strategy. The same selectivity principle is also applied structurally:
`luxe/cli/import_graph.py` walks the repo's Python AST to build a
first-hop import / imported-by index, and `_augment_with_trace_hints`
expands each cited file with up to `max_files − 1` neighbors. The
agent's turn 1 starts with the cited module *plus* its closest
collaborators already in view — same oracle-style whole-file reads,
extended by a real relationship rather than a similarity score.

---

## Ollama silently drops messages when num_ctx is exceeded

A `/review` run at `num_ctx=16_384` logged ~33 000 cumulative prompt
tokens across a 16-tool-call inspection subtask. There were no
warnings, no errors, no `near_cap_turns` flags — the task simply took
longer and produced shallower findings than expected.

What Ollama actually does when the running conversation exceeds
`options.num_ctx`: it quietly truncates the oldest messages and
prompts the model on the truncated window. From the model's point of
view, tool results it read five turns ago have silently vanished. It
re-reads the same files, duplicates work, and sometimes "forgets" that
it already investigated an area.

The signal that exposed this was the gap between `sub.prompt_tokens`
(cumulative input across turns) and the 16k per-turn cap. Once per-
turn input routinely approaches 80% of num_ctx, you are almost
certainly losing older context silently.

**Responses:**

1. Bumped `medium` / `large` tiers in
   `luxe/cli/repo_survey.py` from 16k → 24k. Costs ~5 GB more KV
   cache on qwen2.5:32b Q4_K_M; gains the headroom the workload
   actually needs.
2. The heuristic is now: if `sub.prompt_tokens ≥ 0.5 × num_ctx`, the
   subtask probably saw truncation. Worth surfacing as a log event in
   a future pass, but for now `/tasks analyze` makes it inspectable.

**Principle:** silent failure modes of downstream services are more
dangerous than loud ones. Add a log signal the moment you know the
boundary condition; don't wait until a benchmark run points you at
it three weeks later.

---

## Fast rules cover the easy 60%; use the LLM for the ambiguous 40%

The router is an LLM tool-use call: prompt in, `dispatch(agent, task)`
out. That's correct for genuinely ambiguous prompts ("help me figure
this out"), but the other 60% of prompts carry decisive signals —
a traceback token, a file path with `.py`, `draft an essay`, `compute
15% of $42` — that a deterministic rule table routes in microseconds.
Running the LLM on those costs ~1–2 s per turn for no decision
quality gain.

`luxe/cli/heuristic_router.py` is a pure-regex scorer with
per-agent feature tables. It returns `(agent, confidence, scores)` or
`None`, with the None triggering fallthrough to the LLM. Key design
points:

- **Score, don't classify.** Each agent gets a numeric tally over
  feature hits. The decision rule is a normalized margin between top
  and second scores, not "top score > X". This lets you see the
  second-place choice — useful when the LLM later routes differently
  and you're trying to understand why.
- **Skip the ambiguous cases, don't guess.** Short prompts (< 3
  words), meta questions (`can you …`), and low-margin scores return
  None. The LLM handles those. The heuristic never pretends to know
  when it doesn't.
- **Never score the residual.** `general` isn't in the scorer — if
  no specialist scores, the heuristic abstains and the LLM picks
  `general` itself. `review` / `refactor` also aren't scored because
  they're command-driven (`/review <url>`), not prompt-driven.
- **Replayable.** Session logs tag each decision with
  `"source": "heuristic"` or `"llm"` plus the full score breakdown,
  so offline replay can measure short-circuit rate + disagreement.

Config knobs: `LuxeConfig.heuristic_router_enabled: bool = True`
flips it off for pure-LLM regression testing;
`heuristic_router_threshold: float = 0.35` is the normalized margin
below which the heuristic abstains.

**Principle:** when a cheap oracle can answer confidently on most of
the distribution, route the hard cases to the expensive one. Don't
treat "always use the LLM" as a default; treat it as a fallback.

---

## Cross-subtask redundancy dwarfs the per-subtask win

A `/review` run on elara (17 Python files, 7.8k LOC) took ~50 minutes.
Roughly all of that was 32B-Qwen decode time; orchestration overhead
is well under 5%. But the decode time was buying a lot less than it
should have been:

- Subtask 2 (docs / orient) already ran the full security-tool suite
  — `security_scan`, `deps_audit`, `security_taint`, `secrets_scan`.
  Subtask 3, whose whole job was *security*, ran the same four tools
  twice before the shallow-inspection retry forced it to read real
  files.
- Subtasks 3, 4, 5, 6 each read the same three source files
  (`elara_kill.py`, `elara_task.py`, `tools/calculate_distances_to_chargers.py`).
  That's 4× disk hits and 4× the resulting context tokens per file on
  a repo small enough that "just hold everything in view" was always
  on the table.
- One subtask dispatched `grep path=elara_kill.py` with no `pattern` —
  a malformed call that failed at ripgrep-invocation time, wasting a
  turn on a structured error the agent could have gotten client-side.

The obvious fix — a task-scoped `ToolCache` keyed by
`(name, hash(args))` — is the cheap bit. The harder judgement was
*what to cache*. Read-only fs, read-only git, and all ten static
analyzers are deterministic during a task run (no file writes happen
on a review pass), so they're safe. Mutations (`write_file`,
`edit_file`, `bash`) and web fetches are not, and the wrapping layer
in `luxe/tasks/cache.py:wrap_tool_fns` gates them out. Errors get
cached too — a malformed call won't magically succeed on retry with
the same args, so re-running it is pure waste.

Two rules fell out of tracking the fix:

1. **Inference-bound doesn't mean language-bound.** The Python
   orchestrator costs microseconds per event; a rewrite in Go or Rust
   would have recovered nothing. The wins came from avoiding decode
   cycles the model shouldn't have spent in the first place. Track
   orchestration-level metrics separately from model-level metrics —
   `scripts/bench_orchestrator.py` does this for luxe, appending
   `wall_s / tool_calls / cache_hits / schema_rejects / tokens` to a
   JSONL history with the git rev, so a regression has an audit
   trail.
2. **Validate tool-call args client-side before dispatch.** A
   lightweight JSONSchema check (required fields + primitive types,
   no recursive object validation) inside `luxe/agents/base.py`
   converts "agent emitted garbage, tool errored with a KeyError" into
   "agent emitted garbage, got a schema error back on the same turn
   and retried." Full `jsonschema` isn't worth the dep — the common
   model failure is missing-field or wrong-primitive-type, which is
   ~40 lines of code.

## Live tool telemetry needs its own tap, separate from the task timeline

The task log event stream evolved from five coarse events
(start / begin / end / skip / finish) per subtask. That's fine for a
dashboard but too sparse for live debugging — when a subtask stalls
for 15 minutes, "still running" isn't useful.

The split that worked: **`/tasks tail` stays summary-mode by default**
(one line per subtask begin/end, same as before). **`/tasks tail -v`
opts into per-tool-call lines** (tool name, arg preview, duration,
bytes out, ok/error). Two event kinds —`tool_call_begin` and
`tool_call_end` — are threaded from the tool dispatch loop in
`luxe/agents/base.py` out through `runner.dispatch` → orchestrator
→ `log.jsonl` via an `on_tool_event` callback.

Two things fell out of this:

1. **Always persist, optionally render.** The events land in
   `log.jsonl` regardless of the tail mode, so `/tasks analyze` gets
   the per-tool breakdown for free on every run. `-v` just changes the
   live-render filter.
2. **Arg previews need a policy.** `read_file path=luxe/router.py`
   is useful; dumping a 2 KB `content` arg flooded the tail. The
   preview helper picks two priority keys (path, file, pattern, query,
   url, cmd, name) first, caps each value to 40 chars, and truncates
   with `…`. One-line-per-call is a hard constraint.

**Principle:** give observability a UX flag instead of conditional
logging. Keep the persisted stream complete; let the viewer decide how
much to show.

---

## The MLX engine itself wins, not the SSD cache

Adopted oMLX in April 2026 expecting the SSD-paged KV cache to be the
big win — that was the briefing's framing. The actual measurement said
something different: oMLX's **mlx-lm engine** beats Ollama's vendored
`llama.cpp` by **1.5×** on decode tok/s for every workload tested,
*before* any cache or speculative-decoding configuration. Same Q4_K_M
weights, same hardware (M1 Max 64 GB), same 30-task BFCL/HumanEval+
sweep:

| Model          | Ollama tok/s | oMLX tok/s | oMLX/Ollama |
|----------------|-------------:|-----------:|------------:|
| qwen2.5-coder-14b on HumanEval+ | 18.8 | **29.4** | 1.56× |
| qwen2.5-32b-instruct on HumanEval+ | 8.0 | **12.3** | 1.54× |
| qwen2.5-7b-instruct on decode_throughput | 32.2 | **49.4** | 1.53× |

Pass rates are identical or slightly higher on oMLX. The migration
playbook is just `endpoint: http://127.0.0.1:8000` plus
`OMLX_API_KEY` set — no hyperparameter changes, no cache tuning.

**Trade-off:** oMLX TTFT is ~60% slower than Ollama on the 32B model
(2.2 s vs 1.4 s on HumanEval+). For agents that emit multi-paragraph
outputs (`code`, `review`, `refactor`) the decode win dominates net
wall time. For agents that emit one-shot snippets (`lookup`, the
deterministic pre-router calls), the TTFT regression matters more —
keep them on Ollama.

**Principle:** measure the engine, not just the feature. The thing
the marketing touted (SSD cache) was load-bearing for the adoption
*decision* but turned out to be a footnote next to the unrelated
engine-level perf delta.

---

## Cold/warm cache_benefit_ratio is meaningless against an SSD-paged cache

The first version of `prefix_cache_decay`'s adoption gate compared
`ttft_cold / ttft_warm` between backends. The intuition: a server with
prefix caching should show a big ratio (cold first hit, fast warm
subsequent hits); higher ratio = bigger cache win.

Then I measured it on oMLX. The ratio collapsed to **~1.0×** —
because oMLX's SSD-paged cache makes the supposedly-cold first
request also hit a warm cache (it persists across processes, even
across OS restarts). Meanwhile Ollama's in-memory cache showed a
cold/warm ratio of **77×** because its first request actually was
cold. The metric was telling me oMLX had *worse* prefix caching than
Ollama, when in reality oMLX never goes cold in the first place.

**Fix:** P3 now compares **absolute TTFT median** at 16k between
backends (`omlx ≤ 50% × ollama`). The cold/warm ratio is still
computed and surfaced as supplemental info — useful for *Ollama*
where the cache is in-memory and process-bound, but useless as a
gate against a persistent cache.

**Principle:** the metric you reach for to measure a cache assumes
the cache is invalidated between observations. If the cache survives
your "reset," you're not measuring the cache, you're measuring the
warm path twice.

---

## Speculative decoding is conditional, not a default

Tested two implementations of draft-model speculative decoding on
`qwen2.5-coder:14b` against baseline (HumanEval+, decode_throughput,
prefix_cache_decay × 30 tasks each, 0.5B coder draft):

| Workload | Output length | omlx +DFlash | llama-server +spec |
|---|---|---|---|
| decode_throughput (~1000 tok prose) | LONG | **1.64×** ✅ | 0.81× ❌ |
| humaneval_plus (~50–100 tok code) | MEDIUM | 0.91× ❌ | **1.51×** ✅ |
| prefix_cache_decay (~50 tok answers) | SHORT | 0.92× ≈ | 0.96× ≈ |

Neither is a universal win, and the two implementations optimize for
**opposite output-length regimes**. The reason is the per-request
setup cost — drafting needs a small initial K/V scaffold, and that
cost gets amortized across the decoded tokens. Long decodes amortize
the setup; short decodes don't, and you go net-negative.

DFlash's amortization curve is later — wins only on the 1000+ token
range. llama-server's spec implementation pays its setup cost faster
and hits the break-even point in the 50–100 tok range, which is the
HumanEval+ shape. The "speculative decoding gives 2× decode" claim in
the briefing assumed long outputs without saying so; the truth is
shape-dependent and engine-dependent.

**Practice:** spec decoding is per-agent / per-workload, not a
backend-wide flag. Enable for `writing` (long-form prose) and `calc`
(multi-step output); skip for `lookup` (one-shot answers) and `code`
agents that emit short tool-call snippets.

---

## Bearer auth on `/v1/`, cookie auth on `/admin/api/`

oMLX's `/v1/chat/completions` accepts an `Authorization: Bearer
<key>` header — standard OpenAI-compat. Its `/admin/api/*` endpoints
(used to configure DFlash, list models, trigger HF downloads) reject
the same Bearer token with 401 and require a **session cookie**
obtained by `POST /admin/api/login` with `{"api_key": "<key>"}`.

This split isn't called out in the OpenAPI spec — the
`securitySchemes` section only lists `HTTPBearer`. The first attempt
at `omlx_configure_dflash.py` failed with a confusing 401 from the
admin endpoint because the same key worked everywhere else.

**Fix:** the admin client is a separate `httpx.Client` that does the
login first and reuses the cookie jar. Keep it as a context manager
so the cookie session is scoped to one CLI invocation.

**Principle:** when one base URL serves both an OpenAI-compatible
surface and a vendor-specific admin surface, assume the auth schemes
diverge. Probe both before committing to a wiring.

---

## Adding a new tool name needs the Literal AND the agent runner

The browser tool's first wiring shipped with `browse_navigate` /
`browse_read` added to (a) `cli/tools/browser.py`, (b) `agents.yaml`'s
tool list, and (c) the agent runners (`cli/agents/research.py` and
`lookup.py`). It worked in isolation but broke `cli/registry.py:
load_config()` with a Pydantic `literal_error` — the `ToolName`
Literal type didn't include the new tool names, so config validation
rejected every agent that listed them.

The Literal exists deliberately: it catches typos in `agents.yaml` at
load time rather than at first tool invocation. The cost is that
adding a new tool requires touching four places, and the failure mode
is a long Pydantic dump that doesn't immediately point at the missing
Literal entry.

**Principle:** when validation lives in a type rather than a runtime
check, document the full add-a-tool checklist next to the type. The
checklist for luxe:

1. Implement the tool fn + `ToolDef` in `cli/tools/<name>.py`
2. Add to `cli/registry.py:ToolName` Literal
3. List in the relevant agent in `configs/agents.yaml`
4. Import and merge `tool_defs` + `TOOL_FNS` in `cli/agents/<agent>.py`
5. Update the agent's system prompt with usage guidance

Skipping any one step has a different failure mode: missing the
Literal kills config load entirely; missing the agent runner means
the tool is registered in config but never available at dispatch.

---

## Cached benchmark slots are sticky — name them carefully

`benchmarks/_common.py:run_benchmark()` skips task IDs already present
in the JSONL log (it's resumable on purpose). That works fine for
`--limit 30` re-runs of the *same* config — you pick up where you left
off — but it silently no-ops when you re-run a sweep against the same
`config_id` after changing the underlying backend setup.

Hit this when configuring DFlash on the 14B coder: the second sweep
(with DFlash on) wrote into `omlx_q4km/` — the same slot as the
no-DFlash baseline — and the JSONL skipped every task. The DFlash
measurements got mixed into the baseline file, the "comparison" was
noise, and it took a re-run with `rm -rf <slot>` to recover.

**Fix:** added `--config-suffix _dflash` to `run_ab_benchmark.py` so
variant runs land in a distinct dir (`omlx_q4km_dflash/`) without
polluting the baseline cache. The slot name is the identity — change
ANY input that affects the measurement and you need a new slot.

**Principle:** when a benchmark runner is resumable, the config
identity has to encode every dimension of the experiment. A config
suffix per variant beats a single bucket per backend.

---

## Single-turn benchmarks miss the multi-turn growth regime

The April 2026 oMLX migration was decided on HumanEval+ at 30 tasks ×
~1k prompt tokens × ~50 output tokens per task. oMLX won by 1.54× on
the 32B model. Migrated `review` and `refactor` agents same day.

A real `/review` invocation regressed badly on oMLX — same agent,
same model, same target repo:

| Subtask | Ollama wall | oMLX wall | Notes |
|---|---|---|---|
| sub 01 (~7k prompt) | 1m 11s | 1m 07s | parity |
| sub 02 (~10–13k prompt) | 2m 04s | 2m 53s | **+40%** |
| sub 03 (~13k+ prompt) | 3m 49s total | **>8m** to first tool call | **catastrophic** |

The reason: `/review`'s subtask 03 receives the concatenated outputs
of subtasks 01 and 02 via `_augment_with_prior`, so its prompt is
~13k tokens at start. The HumanEval+ benchmark prompts are ~1k. oMLX
32B's TTFT at 1k was 1.6× slower than Ollama; at 13k it was 6× slower.
Decode speed wins (which is what HumanEval+ measured) couldn't
recover the prefill cost on a tool-heavy multi-turn loop.

**Rollback:** `review` + `refactor` reverted to Ollama. `code`
(14B, smaller prompt, milder TTFT regression) stayed on oMLX where
the HumanEval+ pattern matches the actual workload (single-turn code
generation, modest prompt growth).

**Diagnosis was visible only in the per-tool-call event log:**
`/tasks tail <id>` shows only subtask begin/end. To see the gap
between subtask-begin and the first tool call, you have to crack
open `~/.luxe/tasks/<id>/log.jsonl` and read the
`tool_call_begin` events. The bench harness's metrics didn't capture
this regime at all.

**Principle:** when an agent's prompt grows monotonically over
subtasks (orchestrator workflows, RAG, long-context reasoning), the
benchmark prompt-length distribution must match the production one.
A tool-heavy multi-turn workload measured against a single-turn
benchmark is a different beast. Add a sweep tier at the actual
prompt-length percentile (16k–32k for review/refactor) before
trusting the migration call. See `benchmarks/prefix_cache_decay.py`
for the 4k/16k/32k slicing — extend that pattern to
`humaneval_plus_long_prefix` or similar before the next backend
swap.
