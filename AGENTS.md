# Agents

All specialist agents share the loop in `luxe/agents/base.py`. The
differences are system prompt, tool surface, and model. The router +
nine specialists currently live (`general`, `lookup`, `research`,
`writing`, `image`, `code`, `review`, `refactor`, `calc`); this doc
captures the ones whose selection rationale is worth documenting.
`configs/agents.yaml` is the authoritative source for every knob.

---

## Router  — `luxe/router.py`

**Model:** `qwen2.5:7b-instruct` (4.4 GB)
**Tools:** `dispatch(agent, task, reasoning)`, `ask_user(question)`
**Temperature:** 0.1
**Max clarifying rounds:** 2 (then forced to dispatch)

**Role:** Single hand-off interpreter. Reads the user prompt, picks one
specialist, passes the refined task. The specialist conversation starts
fresh — router history is not forwarded.

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

**Model:** `qwen2.5:32b-instruct` (19 GB)
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
luxebox's licensing. HTTP mode in Draw Things settings sidesteps that.

---

## Code  — `luxe/agents/code.py`

**Model:** `qwen2.5-coder:14b-instruct` (9 GB)
**Tools:**
- read_only: `read_file`, `list_dir`, `glob`, `grep`
- mutation: `write_file`, `edit_file` (targeted string replace)
- shell: `bash` with allowlist (`cargo pytest go python python3 rustc
  node npm pnpm yarn git ls pwd cat head tail echo wc`)
- web: `fetch_url`

**Temperature:** 0.2
**Max steps:** 30, max wall 15 minutes

**Role:** Claude-Code-like editing, with the full fs + bash + web
surface. `luxe analyze <path>` runs in read-only mode (no mutation
tools) for code review.

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
was in fact on line 3. Strong exploration-protocol prompts help but
don't eliminate this. Always human-review code-agent output.

**Scoping:** All fs tools confined to `fs.repo_root()` (process CWD
unless overridden). Bash only runs allowlisted binaries — `shlex.split`
parses, first token must be in the set.

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
   `num_ctx` (Ollama `options.num_ctx` override, useful when the agent
   needs a wider window than the loaded default), and `endpoint`
   (per-agent base URL override, e.g. pointing at a llama-server
   instance).
5. Add the agent name to `luxe.registry.AgentName` enum and (if new
   tools) `ToolName` enum.
6. Update `router.py`'s `descriptions` dict with a one-line description
   the router can use when deciding to dispatch.
7. (Optional) extend `scripts/run_luxe_eval.py` with a sub-eval for the
   new agent.
