# Agents

All specialist agents share the loop in `luxe/agents/base.py`. The
differences are system prompt, tool surface, and model. The router +
nine specialists currently live (`general`, `lookup`, `research`,
`writing`, `image`, `code`, `review`, `refactor`, `calc`); this doc
captures the ones whose selection rationale is worth documenting.
`configs/agents.yaml` is the authoritative source for every knob.

---

## Router  — `luxe/router.py` + `luxe/heuristic_router.py`

**Model:** `qwen2.5:7b-instruct` (4.4 GB)
**Tools:** `dispatch(agent, task, reasoning)`, `ask_user(question)`
**Temperature:** 0.1
**Max clarifying rounds:** 2 (then forced to dispatch)

**Role:** Single hand-off interpreter. Reads the user prompt, picks one
specialist, passes the refined task. The specialist conversation starts
fresh — router history is not forwarded.

**Pre-router (heuristic).** Before the LLM runs, a deterministic
keyword/regex scorer
(`luxe/heuristic_router.decide`) tries to pick an agent from rule
tables (path-like token → `code`, `draft`/`essay` → `writing`, short
interrogative + factual noun → `lookup`, etc.). On a confident
decision (normalized margin ≥ `heuristic_router_threshold`, default
0.35), it short-circuits and returns a `RouterDecision` directly — no
LLM call. On ambiguous prompts (< 3 words, meta questions, low
margin), it returns `None` and falls through. The scorer never picks
`review`/`refactor` (command-driven) or `general` (the residual).
Session logs tag each decision `"source": "heuristic"` vs `"llm"` with
scores. Disable via `LuxeConfig.heuristic_router_enabled = False`.

**Selection rationale:** Tested `qwen2.5:7b-instruct` vs `gemma3:12b`
against 10 mixed prompts.
- qwen2.5:7b → 10/10 correct dispatches, 1.5 s avg
- gemma3:12b → Ollama refused: `gemma3:12b does not support tools`

qwen2.5 ships ready-to-use tool calling via Ollama's OpenAI-compat
endpoint. Gemma3 doesn't (model class limitation, not a config issue).

**Failure modes:**
- If the model emits no tool call at all, we fall back to the `general`
  agent with the raw prompt and log the reason.
- If clarifying questions loop infinitely, the second round strips
  `ask_user` from the tool set and forces a `dispatch` decision.
- Any exception from `backend.chat` → fall back to `general` with a
  descriptive reasoning string.

---

## General  — `luxe/agents/general.py`

**Model:** `qwen2.5:7b-instruct`
**Tools:** none (pure chat)
**Temperature:** 0.3
**Max steps:** 3 (basically one turn; no tools means no loop)

**Role:** Default for Q&A, explanations, definitions, small-talk.

**Selection rationale:** 8-prompt subjective eval vs `gemma3:12b`,
`mixtral:8x7b`. qwen2.5:7b won on latency (1.7 s avg) + concision
(42-word avg) while staying accurate. Gemma was slightly more concise
but 2× slower. Mixtral was verbose and sometimes hallucinated the
luxe system-prompt text into its answers.

**System prompt posture:** concise, direct, no preamble. Redirects
creative/research/code requests back to the router for re-dispatch.

---

## Research  — `luxe/agents/research.py`

**Model:** `Qwen2.5-32B-Instruct-4bit` (19 GB on Ollama; same weights
served via oMLX since 2026-04-27). Briefly swapped to
`Qwen3-30B-A3B-Instruct-2507-4bit` (MoE) on 2026-04-27 alongside
review/refactor; reverted same-day after live tests showed the MoE
silently skips `web_search`/`fetch_url` and answers from training
data with fabricated `[1]–[n]` citations (e.g. "Immigration, Refugees
and Citizenship Canada — Permanent Residence" with no URL; Bolt EV
specs cited as 60 kWh / 150 kW when the real numbers are 65 kWh /
~55 kW). The MoE's lighter tool aggression is fine for read-and-
reason agents (review/refactor kept it) but production-breaking for
research, where every claim must trace to a fetched source.
**Tools:** `web_search` (DuckDuckGo via `ddgs`), `fetch_url`
(trafilatura extraction), `fetch_urls` (same extraction, up to 4 URLs
fetched concurrently via `httpx.AsyncClient`)
**Temperature:** 0.2
**Max steps:** 10, prompt-budgeted to 2 searches + 3 fetches

**Role:** Web-enabled synthesis with inline citations.

**Selection rationale:** Compared against `qwen2.5:7b-instruct` on 4
research prompts.
- 7b: ~16 s/prompt, **hallucinated answers** (said latest PostgreSQL was
  v15 — it's 18; named Avi Wigderson as most-recent Turing winner — he
  won 2023, Bennett & Brassard won 2025).
- 32b: ~60 s/prompt, **got them right** (v18; Bennett & Brassard).

`llama3.3:70b-instruct-q4_K_M` was tested but OOMs on Ollama with the
full 32k context on a 64 GB machine (weights ≈ 40 GB + KV cache).

**Failure modes:**
- Even 32b sometimes misses fresh sources. Always verify.
- Search results are DuckDuckGo, which is thinner than Google.
  SearXNG was planned as an alternative; currently not wired up.

**Parallel fetch.** `fetch_urls([url1, url2, …])` runs up to 4 reads
concurrently and returns a list of `{url, text, truncated, error}`.
The prompt tells the agent to prefer it over sequential `fetch_url`
calls once 2+ URLs are queued, which halves wall time on multi-source
research.

**Cited output:** Inline `[n]` citations map to a "Sources" list at the
end of the reply. The system prompt explicitly forbids inventing URLs
or facts outside the fetched sources.

---

## Writing  — `luxe/agents/writing.py`

**Model:** `gemma3:27b` (17 GB)
**Tools:** `read_file`, `list_dir`, `glob`, `grep`, `write_file`, `edit_file` — scoped to the folder `luxe` was launched from
**Temperature:** 0.9
**Max steps:** 8

**Role:** Creative writing, editorial review, and drafting — fiction,
poetry, brainstorming, plus reviewing existing drafts in the local
folder, revising them in place, or saving new work to disk. Default is
still inline prose; the agent only writes a file when the task calls
for it.

**Selection rationale:** 7 candidates were run through 3 prompts
(paragraph story, 3 distinct character ideas, short poem/stanza).
- **gemma3:27b** — best voice. Concrete sensory detail, distinct
  characters, grounded imagery. 27.6 s avg.
- **gemma3:12b** — 9.4 s avg (3× faster), still strong, close runner-up.
- llama3.3-70b-4k — also strong but 83 s avg (9× slower than 12b).
- mistral-small:24b, command-r:35b, mixtral:8x7b, qwen2.5:32b — all
  produced more generic/template-y output.

User picked 27b over 12b for top quality. Full eval in
`results/luxe_eval/writing/*.md`.

**System prompt posture:** match requested form exactly; avoid
preamble ("Here's a story…") and postamble; specificity over
generality; distinct tones/settings when multiple items are asked for;
reach for the fs tools when the user references a local draft
("review my notes", "tighten the second paragraph", "save it to
foo.md") and otherwise stay inline.

---

## Image  — `luxe/agents/image.py`

**LLM (prompt expander):** `qwen2.5:7b-instruct`
**Image model:** whatever is loaded in Draw Things (currently
`flux_2_klein_4b_q8p.ckpt`)
**Tools:** `draw_things_generate(prompt, negative_prompt, steps, w, h)`
**Temperature:** 0.5

**Role:** Takes a user description, expands it into a rich visual
prompt (subject + setting + style + lighting + composition), calls
Draw Things, saves PNG to `~/luxe-images/<ts>-<slug>.png`.

**Architecture:** Draw Things runs as a separate macOS app with its
HTTP server enabled (Settings → HTTP Server, port 7859 in newer
versions). luxe POSTs to `/sdapi/v1/txt2img` (stable-diffusion-webui
compatible) and decodes the returned base64 PNG.

**Health check:** On dispatch, `draw_things.health_check()` pings
`/sdapi/v1/options`. If unreachable the agent aborts cleanly with a
helpful message rather than trying.

**Not used: gRPC.** Draw Things also offers a gRPC interface on the
same port, but it requires a GPL-v3 proto file that would complicate
luxe's licensing. HTTP mode in Draw Things settings sidesteps that.

---

## Code  — `luxe/agents/code.py`

**Model:** `qwen2.5-coder:14b-instruct` (9 GB)
**Tools:**
- read_only fs: `read_file`, `list_dir`, `glob`, `grep`
- mutation fs: `write_file`, `edit_file` (targeted string replace)
- shell: `bash` with allowlist (cargo, pytest, go, python, rustc,
  node/npm/pnpm/yarn, git, and the analyzer binaries —
  `ruff`/`mypy`/`bandit`/`pip-audit`/`semgrep`/`gitleaks`/
  `eslint`/`tsc`/`clippy`/`staticcheck` — for `ruff --fix` style
  escape-hatch invocations beyond what the narrow tool schemas
  allow)
- web: `fetch_url`
- static analyzers: `lint`, `typecheck`, `security_scan`,
  `deps_audit`, `security_taint`, `secrets_scan`, `lint_js`,
  `typecheck_ts`, `lint_rust`, `vet_go` (see *Static analysis*
  below)

**Temperature:** 0.2
**Max steps:** 30, max wall 25 minutes (1500 s; raised from 15 min
after the elara `/review` timed out a correctness subtask mid-retry).

**Role:** Claude-Code-like editing, with the full fs + bash + web +
analyzer surface. `luxe analyze <path>` runs in read-only mode
(no mutation tools) for code review.

**Fixed ctx:** `num_ctx: 32768` set in `configs/agents.yaml` (the
trained native for Coder-14B). Earlier versions adaptively bumped
ctx for medium+ cwd via `_resize_for_cwd`; that hook was removed
2026-04-27 in favor of one fixed value per agent. Wall-time still
scales with repo size via the pre-flight survey on `/review` and
`/refactor`; `code` itself is a single-turn dispatch and uses
`max_wall_s` from the YAML.

**Selection rationale:** tried 4 model configurations against 4 real
personal repos.
- **14b default** — shallow exploration (2–4 tool calls per repo) but
  stable. ~2 min/repo. Picked.
- **32b default (32k ctx)** — ~7 min per turn, aborted on 15-min wall.
- **32b-8k variant** (via modelfile `PARAMETER num_ctx 8192`) — emitted
  `<|im_start|>` chat-template garbage in content; context exhausted
  after 3 tool calls.
- **32b-16k variant + explicit stop tokens** — same template leak, just
  a few turns later.

Alternate tested-and-working models for `--model` override:
`llama3.3-70b-4k:latest`, `command-r:35b`. Both have cleaner tool
use but slower turn times.

**Known limitation:** 14b hallucinates bugs it didn't read. Example:
claimed a repo's `.env.example` was "missing the API key" when the key
was in fact on line 3. The analyzer tool surface reduces this: `lint`
and `typecheck` return deterministic findings for the most common
false-positive classes before the model reaches for grep. Still human-
review the output — the underlying hallucination mode isn't fully
gone, just scoped.

**Scoping:** All fs tools confined to `fs.repo_root()` (process CWD
unless overridden). Bash only runs allowlisted binaries — `shlex.split`
parses, first token must be in the set.

---

## Review  — `luxe/agents/review.py`

**Model:** `Qwen3-30B-A3B-Instruct-2507-4bit` (MoE, 3B active per
token; ~17 GB on disk). Swapped 2026-04-27 from `Qwen2.5-32B-Instruct-
4bit` after an A/B sweep + live `/review elara` run showed the new
MoE produces a clean 9-minute report where the prior 32B took 57
minutes and repeated the same `json.loads`-error-handling finding 30+
times at fabricated line numbers. The MoE's lighter tool aggression
is a feature here: review/refactor are read-and-reason agents and
don't need the model to chain web/tool calls. Earlier history: moved
off `qwen2.5-coder:14b` in April 2026 after the 14B coder fabricated
findings on real repos. The Qwen2.5-32B intermediate stage stays in
service for `research` and `calc`, where the same MoE silently skips
required tool calls — see `research`/`calc` model notes.

**Tools:** read-only fs (`read_file`, `list_dir`, `glob`, `grep`) +
read-only git (`git_diff`, `git_log`, `git_show`) + the full
analyzer suite above. Never writes.

**Driven by:** `/review <git-url>` in the REPL or
`luxe analyze <path> --review`. Both plan a 7-subtask task
(orientation → docs → security → correctness → robustness →
maintainability → severity-grouped synthesis) pinned to the review
agent.

**Anti-fabrication guardrails** (`luxe/tasks/orchestrator.py`):

- **Shallow-inspection retry.** Subtasks that end with zero reading-
  tool calls (or with many `file:line` citations from too few reads)
  are retried with a stronger prompt. Catches the "one `list_dir`
  and a page of invented findings" pathology.
- **Forced inspection fallback.** If the retry also refuses, the
  orchestrator runs a canonical grep panel itself and asks the
  model to summarize the raw output instead of generating from
  training-data recall.
- **`file:line` citation verification.** Every cited location is
  re-read post-subtask; out-of-range or nonexistent citations get a
  `⚠️ Grounding check failed` block prepended to the finding.
- **Finding-level pattern verification.** Each backtick-quoted code
  construct in a finding's Issue/Why text is grepped in the cited
  file; absent constructs are flagged the same way. Catches claims
  like "`server.py` contains a call to `os.system`" when the file
  has no such call.
- **Severity-validity checklist.** System prompt includes explicit
  rules for eval/subprocess/`while True:`/missing-timeout patterns:
  check for sandbox, list-args, break conditions, actual kwargs
  before assigning severity.
- **Pre-flight repo survey.** Task wall sized from the target repo's
  LOC tier (see `luxe/repo_survey.py`). `num_ctx` is fixed per agent
  in `configs/agents.yaml` (32k for review/refactor) — not picked
  per-tier. The survey's `language_breakdown` also gates the
  analyzer surface (`AgentConfig.analyzer_languages`) — a pure-Python
  repo never sees `lint_js` / `vet_go` / etc.
- **Per-agent wall budget:** 1500 s (25 min). The orchestrator's
  shallow-retry can consume the first 400–600 s, so the budget needs
  to be wide enough to fit `initial attempt + retry + productive
  work`. Subtasks that still time out now survive via `/tasks
  resume <id>`, which flips blocked/skipped subs back to `pending`
  without discarding completed ones.

---

## Refactor  — `luxe/agents/refactor.py`

**Model:** `Qwen3-30B-A3B-Instruct-2507-4bit` (shared config rationale
with `review` — same MoE swap on the same day). **Tools:** identical
to review. **Driven by:**
`/refactor <git-url>`. Subtasks focus on performance → architecture
→ code size → idioms rather than security. Same anti-fabrication
guardrails, same pre-flight survey, same 1500 s per-agent wall.

---

## Static analysis — `luxe/tools/analysis.py`

10 callable analyzer tools share `luxe/tools/_subprocess.py`'s
`run_binary()` helper, which resolves binary paths to the current
venv's `bin/` before falling back to system PATH (so `uv sync
--extra dev` installs are picked up even when luxe runs as a
detached subprocess).

**Python:** `lint` (ruff), `typecheck` (mypy), `security_scan`
(bandit, filtered to `min_confidence=MEDIUM` by default),
`deps_audit` (pip-audit), `security_taint` (semgrep's `p/python`
taint rules — the only tool in the kit that does source→sanitizer→
sink analysis; use it before severity calls on eval/exec/subprocess/
pickle/SQL), `secrets_scan` (gitleaks with `--redact=100` so
credentials never reach the model).

**Cross-language:** `lint_js` (eslint), `typecheck_ts` (tsc),
`lint_rust` (cargo clippy JSONL), `vet_go` (go vet + stderr parse).
Each checks for its project marker (`package.json`, `tsconfig.json`,
`Cargo.toml`, `go.mod`) and returns `{note: "not a <lang> project"}`
when absent — the agent moves on rather than erroring.

All tools return `(result, err)` tuples where `result` is a JSON
string shaped as `{findings: [...], count, note?, truncated_at?}`
and `err` is None on success or a string on failure. Graceful-degrade
is the same pattern as `fs.grep` — `FileNotFoundError` on the binary
becomes a helpful error message (`"ruff not installed. uv sync
--extra dev pulls it in."`) that the model can read and adapt to.

Per-tool telemetry is stamped onto `ToolCall` (`wall_s`, `ok`,
`bytes_out`) in the dispatch loop. `/tasks analyze <id>` prints a
per-subtask breakdown with totals and an analyzer-vs-reader
adoption ratio; `scripts/summarize_runs.py` aggregates the same
data across every run in `~/.luxe/tasks/` as a CSV.

---

## Adding a new agent

1. Create `luxe/agents/<name>.py` — copy general.py or research.py as a
   template. It's just a `run()` wrapper around `run_agent()` from
   `agents/base.py` with its tool set baked in.
2. Add a tools module if needed at `luxe/tools/<name>.py` exposing
   `tool_defs()` and a `TOOL_FNS` dict.
3. Register in `luxe/runner.py`'s `_SPECIALISTS` dict.
4. Add a config entry to `configs/agents.yaml` with the model, prompt,
   budgets, and tools list. Optional knobs: `min_tool_calls` (refuse
   to finalize until the agent has made at least N tool calls),
   `num_ctx` (fixed per-mode context window — Ollama-effective via
   `options.num_ctx`, oMLX/llama-server honor server-side
   `--max-kv-size`), `provider` (key in the top-level `providers:`
   map — preferred), or `endpoint` (legacy direct URL override,
   takes precedence over `provider:` if both are set). See the
   "Provider migration" section in `luxe/luxe_cli/README.md` for
   the full list of declared providers.
5. Add the agent name to `luxe_cli.registry.AgentName` enum and (if new
   tools) `ToolName` enum.
6. Update `router.py`'s `descriptions` dict with a one-line description
   the router can use when deciding to dispatch.
7. (Optional) extend `scripts/run_luxe_eval.py` with a sub-eval for the
   new agent.
