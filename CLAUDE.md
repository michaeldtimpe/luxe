# Claude Code instructions for luxe

Auto-loaded at session start. Points at the durable contracts and the
short list of project-specific gotchas.

## Single-champion policy

**luxe pins exactly one MoE model: `Qwen3.6-35B-A3B-6bit`** (configured
in `configs/single_64gb.yaml`). The M5 Max m5max_moe bake-off (2026-05-10)
confirmed it across all eligible MoE candidates: 10/10 perfect, fastest
wall (40.0s avg), highest TPS (72.7), no bailouts. Larger MoE
candidates (Qwen3-Coder-Next-80B, GLM-4.5-Air-106B) also passed but
offered no win on speed/efficiency.

All ongoing development is centered on this single champion. Practical
implications:

- **Do not introduce model-fan-out**: no per-task model selection, no
  router, no A/B against another model unless the user explicitly asks
  for a re-bench. The bake-off is settled.
- **Tuning and substrate fixes target this model's failure modes**.
  When proposing changes (prompts, gates, tool surface), evaluate them
  against `Qwen3.6-35B-A3B-6bit` first; other-model evidence is
  secondary unless the user specifies a wider sweep.
- **The champion is platform-stable**: it ran on M1 Max (64 GB) and is
  the M5 Max winner. There is no platform-specific MoE champion split
  to maintain.
- **Don't keep alternate model configs warm**: configs in
  `configs/_archive/` are reference-only. Don't promote them.

If a re-bench is ever needed, follow `~/Downloads/luxe/RESUME.md` §
"M5 Max MoE bake-off" structure and produce results under
`acceptance/m5max_moe_<rebench-id>/`.

**Benched and NOT promoted.** **Gemma 4** (`gemma-4-26b-a4b-it-6bit` MoE +
`gemma-4-31b-it-{4,6}bit`), 4×10 on m5, 2026-08-19: each printed 9/10 against
the reference's 10/10 at up to 4.7× less wall, but hand-reading the MoE arm's
nine passes found 5 REAL / 3 THIN / 1 VACUOUS — **three left the repo worse
than `base_sha` with `gates_triggered` empty**, and it made zero
`lint`/`typecheck`/`git_diff` calls on any fixture. Tool calling itself works
natively (zero `textfallback_drop`/`tool_reject`; the empty-`chat_template`
and dropped-`tools` blockers are gone) and there is no vlm-engine penalty —
so the refutation is about verification behavior, not capability plumbing.
Evidence: `acceptance/gemma4_r1_2026_08_19/HAND-VERIFY.md`, lessons.md
2026-08-19. Don't re-open without new information.

**Sanctioned exceptions — `luxe chat` slots + per-host manifests.** The
interactive front-end (`src/luxe/chat/`) has two sanctioned carve-outs from
single-champion, both scoped to `luxe chat`/`luxe code` and never the
benchmark/maintain path (luxe.sdd):
(a) opt-in `chat`/`plan`/`code` model slots via `configs/chat.yaml` `slots:`;
(b) **per-host manifests** (`hosts:` in chat.yaml, 2026-07-30 fallback-kit
pivot): each fleet host declares an interactive main + fallback pair sized to
its RAM — m5 (128 GB) = champion + 27B-6bit; m1 (64 GB) and m4 (48 GB) =
35B-A3B-4bit main + 27B-4bit fallback (MoE-first, flipped 2026-07-30: Qwen3.6
is multimodal so oMLX runs it on the slow vlm engine — dense-27B prefills at
~65 tok/s with no cache reuse; the MoE holds ~10s turns). **The interactive
default on m1/m4 is deliberately NOT the champion** — do not "restore" it. The champion pin is a benchmark pin:
`single_64gb.yaml` still selects it and m1 keeps its weights via the
manifest's `keep:` list. A host with no `hosts:` entry behaves exactly as
before (champion everywhere). (c) **The m5-only capacity model**
(2026-08-03/04): `GLM-4.5-Air-4bit` (106B-A12B, bake-off-passing) lives in
m5's `keep:` + `visible_models` for capacity-over-speed sessions — opt-in
per session via `/model all GLM-4.5-Air-4bit`, never a slot default, never
a manifest main/fallback, never on m1/m4 (60 GB doesn't fit), never the
bench champion. Overnight drill verdict 9/9 at ~2× wall:
`acceptance/glm_capacity_drills/REPORT.md` (local),
`scripts/capacity_drills.py` to re-run. (d) **The OpenRouter chat backend**
(2026-08-17): `backends: openrouter` with `engine: openrouter` — a cloud,
metered endpoint reachable only by opting in per session
(`luxe chat --backend openrouter`, or `/backend openrouter`). Everything on it
is billable, so cost is visible in the status bar/footer/`/usage` and bounded
by a hard `budget_usd` the session refuses to cross. Never a slot default,
never a host-manifest main/fallback, never a bench/`smoke`/`ready` target, and
the benchmark path still reads `omlx_base_url` only. Its per-backend
`visible_models:` is the shortlist; `/model find <text>` searches the live
catalog. Do not extend fan-out beyond these.

## Fallback kit (2026-07-30 pivot — read this before touching chat)

Luxe's mission narrowed after the 2026-07-29 Anthropic outage: it is the
**local fallback dev tool** for the fleet (m1 · m4 · m5), and it has to WORK
when reached for — availability over capability. Concretely:

- **Two entry points, one engine**: `luxe chat` (anywhere, read-only,
  conversation) and `luxe code` (REQUIRES a project, write tools ON from turn
  one, bash still gated). Wrappers `luxe-chat`/`luxe-code` live in
  `~/dotfiles/bin`. Shared body `cli._run_interactive`.
- **Per-host main+fallback manifests** (`hosts:` in configs/chat.yaml) with
  **loud auto-degrade**: main missing from the catalog / failing to load /
  failing a turn on a healthy endpoint → the session switches to the declared
  fallback and says so (status line, `/doctor`, debug.log). See chat.sdd.
- **Manifest models are locally cached, verified, and protected**:
  `luxe pull` provisions (kappa mount preferred, HF via oMLX admin API),
  `/doctor` + `luxe pull --list` detect DANGLING store symlinks (the
  HF-cache-wipe signature — a listed model the server can't load),
  `luxe pull <name> --remove` deletes but refuses manifest models sans
  --force.
- **`luxe ready`** (alias `luxe doctor`) is the point-in-time host preflight
  (seconds, no model): `/doctor`'s checks against a stand-in session, printed
  through the SHARED renderer `chat.inspection.render_doctor`, then a verdict
  — exit 0 on ok/warn, 1 on any FAIL, 2 on a bad `--backend`. Offline-safe
  (doctor's one ≤4s `update` fetch degrades quietly). Every `Check.fix` is a
  runnable command. **`luxe outage` / `/outage`** print `OUTAGE.md`, the
  ≤120-line offline emergency card (one reader, `luxe.outage.load_card`; a
  test asserts every `luxe <sub>` it names is a registered command, so it
  can't rot). There is deliberately **no scheduled/cron/launchd/alerting
  counterpart** — user decision; don't add one.
- **`luxe smoke`** is the aliveness drill (minutes): manifest → weights →
  endpoint → catalog → one real turn + tool call on main → one turn on
  fallback. Run it after provisioning and on a schedule; exit 0 = ready.
  **`luxe smoke --chat --code`** runs the agentic drills: real run_single
  turns in a planted scratch repo (--code = fix a bug + failing test,
  verified by pytest + git diff; --chat = read-only file-grounded answer).
  `--backend m5` drills a remote host's manifest models from here.
  Headless diagnostics: pipe turns into the line REPL
  (`printf 'msg\n/quit\n' | luxe chat --repo <dir>`); post-hoc forensics in
  `~/.luxe/sessions/<id>/` (debug.log, transcript) + `~/.luxe/runs/`.
  See README § "Self-testing luxe". Chat bash runs with luxe's venv bin
  prepended to PATH (tools/shell.py `_chat_bash_env`) so agent test runs
  (`pytest`) work on every host — bench bash env untouched.
- **Every session writes `~/.luxe/sessions/<id>/debug.log`** (always-on;
  chat/debuglog.py) and failed turns persist kind="error" transcript records —
  post-outage diagnosis must not depend on what the TUI happened to show.
- **`luxe update`** (wrapper `luxe-update`) is the one-word host update:
  fetch → show incoming → rebase onto origin/main → `uv sync --extra chat
  --extra dev --extra analyzers --extra web` (the canonical host sync);
  no-op when current. `/doctor`'s `update` check is the only networked doctor
  line (≤4s fetch; offline = quiet OK, never a warning — doctor runs during
  outages). Startup banners stay offline-pure (local refs only).
- gemma is out of the roster (no tool support); the bench apparatus is cold
  storage — capability re-benching only on explicit request.

## Fleet sibling — neo (luxe over llama-server) and where model truth lives

luxe is the living center of the fleet's model/agent work; sibling repos are
satellites and **their m5 clones go stale (bit twice).** When a question
touches micro-mind/neo state, trust the checkouts ON neo (`ssh neo`,
`~/Downloads/{micro-mind,neo-llm-bench,luxe}`) or a freshly fetched
`origin/main` — never an unfetched m5 clone. Detail: memory
`project_micromind_champion_neo.md`, `acceptance/luxe_neo_unification_2026_08/REPORT.md`.

- **neo (A18 Pro, 8 GB) runs luxe** as of 2026-08-13; **micro-mind and
  neo-llm-bench are retired** (superseded for deployment — their checkouts +
  `lessons.md` stay on neo as the historical record for small-model work).
  luxe is the one agent codebase fleet-wide.
- **neo's ENGINE is not retired:** llama.cpp `llama-server` router mode
  (`--models-preset ~/dotfiles/luxe/neo-models.ini`, port 8080, LaunchAgent
  `com.micromind.llama-server`). Champion since the 2026-08-03 bake-offs:
  **`Qwen3-4B-Instruct-2507-Q4_K_M`** (GGUF, ctx 16384, q8 KV, single-model,
  no fallback) — smallest model that passes luxe's real code drill on that box
  (the 1.5B's 0% BFCL multi-turn floor is a size artifact; coder variants lost
  to instruct at every size; 14B is Metal-unrunnable on 8 GB). **MLX/oMLX is
  not an option there** (refuted 2026-08-13, `acceptance/mlx_neo_probe_2026_08/`).
- **neo's luxe config is OUT of this repo:** `~/dotfiles/luxe/neo.yaml`
  (a `hosts.neo` manifest + `backends.local` at 127.0.0.1:8080,
  `engine: llama-server`), passed via `--config` / `$LUXE_CONFIG`. So neo has
  **no `hosts:` entry in `configs/chat.yaml`, by design** (an earlier in-place
  edit + `--skip-worktree` caused 78 commits of drift); the chat.sdd statement
  is about this repo's file, not neo lacking a manifest.
- **`engine:` on a `backends:` entry** (`omlx` default | `llama-server`)
  switches ONLY luxe's oMLX-specific diagnostics (endpoint label/fixes,
  API-key check, weights-location line, stale-Cellar probe, `luxe pull`'s
  refusal). The request is byte-identical — every supported engine is
  OpenAI-compatible.
- **`luxe smoke` on neo now PASSES locally** (2026-08-13: `ready` exit 0,
  `smoke` 3s, `smoke --chat --code` 38s). The old "bare smoke fails on neo"
  pin is RETIRED; drilling m5 from neo still works but is no longer the only
  option.
- **Boot persistence is a KNOWN OPEN GAP (2026-08-13):** the LaunchAgent is
  blocked at login by macOS **Background Task Management** (`disposition = 9`,
  enabled-but-not-allowed) — needs a one-time GUI approval in System Settings →
  Login Items & Extensions. Until then, after a reboot:
  `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.micromind.llama-server.plist`.
  Diagnosis in `~/dotfiles/luxe/README-neo-router.md`. **Do not add a
  cron/launchd watchdog** — same standing decision as `luxe ready`'s.

## Interactive front-end (`luxe chat` / `luxe compare`)

Added 2026-06-01 (additive; benchmark path byte-identical). See `RESUME.md`
2026-06-01 handoff, memory `project_luxe_chat_interactive_overhaul.md`, and
`src/luxe/{chat,compare,memory}/<dir>.sdd` — **walk the relevant `.sdd`
before editing anything here.** This section lists only the invariants a
session must not rediscover the hard way; mechanism lives in the `.sdd` + code.

- **`luxe chat`** — REPL. Each turn = one `run_single` call; conversation +
  project memory inject ONLY via the `run_single(extra_context="")` seam
  (default `""` = byte-identical). Read-only tools by default (`/write` toggles).
- **Conversation by default (2026-07-29 fix).** Freeform turns get the
  `chat_conversational` persona; the maintenance persona applies only to
  `/plan`, `/goal`, `/use <slot>`. Do NOT re-key the persona on the routed
  slot — `_infer_task_type` is a keyword heuristic that misclassifies ordinary
  messages as coding (the "chats become coding sessions" bug). Slots pick the
  MODEL only.
- **Multi-backend (chat-only carve-out, luxe.sdd):** `configs/chat.yaml`
  `backends:` → BackendEntry; `/backend` lists/switches (health-checked, never
  unloads the OLD server), `--backend` at startup. Keys from env only (m5 →
  `OMLX_API_KEY_M5`), never YAML. Absent block ⇒ synthesized `local` from
  `omlx_base_url`; **benchmark/maintain read `omlx_base_url` only.**
- **Progress deadlines (B6, 2026-07-31 — NOT chat-only).** oMLX keepalives
  reset `httpx.Timeout` (a per-read deadline), so no finite `timeout_s` bounds
  a stalled request. `Backend` keeps a SECOND clock on *progress*:
  `stall_timeout_s` (1800s) before the first token, `decode_stall_timeout_s`
  (120s) once tokens flow; keepalives never count. **Raising `timeout_s` is
  not a fix.** The non-stream path is benchmark/maintain, which could
  previously wedge forever. Numbers live in `backend.py`.
- **`/attach <path>`** stages file contents one-shot for the next turn (48KB/
  file, 128KB/turn, binary refused, injected as `<attached_files>`).
- **TUI:** multi-line pastes → `[pasted N lines]` chip; `--resume`/`/resume`
  work inside the TUI. `[chat]` extra (`uv sync --extra chat`); without it,
  line-REPL fallback.
- **Model roster + tool capability (2026-07-30).** `visible_models:` is the
  working set `/model` offers; the rest are hidden. Local weights carry NO
  glyph (only ☁ network / ⇅ remote). `chat/modelcaps.py` detects tool support
  from the chat template: gemma-3 has none and oMLX *silently drops* the
  `tools` array for it, so luxe withholds the tool surface — gemma is
  selectable but NOT the default (a default must be able to read a file).
- **Start anywhere (2026-07-30; `chat/project.py`).** Subject resolves to git
  (walks UP to the git root), dir (marker), or none; `$HOME` and above never
  count. `--repo` resolves upward too. No-project = no index, no repo lock,
  read tools work, `bm25_search`/`find_symbol` withheld. `/project` switches,
  `/index` builds.
- **Startup indexing is bounded + single-pass (2026-07-30).** `cli._build_chat_indexes`
  runs `fswalk.scan_source_files` once (was 3 uncapped walks — 210s from `~`).
  Caps: `LUXE_INDEX_MAX_FILES` (8000), `LUXE_INDEX_MAX_MB` (96),
  `LUXE_INDEX_NO_GIT=1`. **Benchmark/maintain keep the unbounded walk.**
- **`/pull` — model weights (2026-07-29; `modelstore.py`, `luxe pull`).**
  Mounted volume first, else HF **through oMLX's own downloader**
  (`/admin/api/hf/*`, cookie session). Never write a second `snapshot_download`
  (races the server over the HF cache). Mount imports resolve symlinks AND
  Synology `XSym` stubs; dangling links abort; stages in `.partial`. Full
  detail in memory `project_luxe_model_provenance_and_pull.md`.
- **`/claude` diagnoses Claude Code itself (2026-08-13; `claudecode.py`,
  `luxe claudecode`, tool `claude_code_diag`).** Answers which billing path
  each running session is on. **Three tested invariants (chat.sdd Must-not) —
  the sanctioned boundary for the `~/.claude` prohibition:** env vars reported
  by NAME only; Keychain metadata-only, never `-w` except the regex-validated
  expiry DATE; transcripts read for METADATA only, never `message.content`,
  never `~/.claude/CLAUDE.md`. Never launches/kills/reconfigures.
- **ctrl+c CLEARS the prompt, never quits (2026-08-13).** Busy → cancel turn;
  idle+text → clear input; idle+empty → nothing. Exits stay ctrl+d/ctrl+q/`/quit`.
- **Read-only session commands (chat/inspection.py):** `/theme` `/tools`
  `/status` `/unload` `/retry` `/export` `/diff` `/doctor`. `/diff` = files
  THIS session wrote vs HEAD; `/export` renders the PERSISTED transcript;
  `/doctor` preflights and prints a fix per warning.
- **MCP attaches at STARTUP only via `--mcp` (chat-only; `--mcp-config`,
  default configs/mcp.yaml).** No mid-session attach. Namespaced
  `mcp__<server>__<tool>`; `gate_tools` follows `/write`; `--mcp-read-only`
  drops them. **Servers ISOLATED per connection (2026-07-31): one task +
  `AsyncExitStack` each — don't reintroduce a shared stack; catch
  `BaseException` at any anyio connect boundary** (lessons.md 2026-07-31,
  memory `project_luxe_relay_mcp_chat.md`). Relays wired via dotfiles; no
  hostname/token in this repo.
- **Web tools `/web`-gated, default OFF (2026-07-31; `src/luxe/web/`, walk
  web/web.sdd).** `web_fetch`, `web_search` (Brave/Tavily via luxe.secrets),
  `web_answer` (Brave Answers, a separate product). **NEVER add these to
  `TOOL_FNS`** (a benchmark reaching the live internet is not reproducible) —
  chat-only via the extra-tool seam. Egress guard refuses non-public hosts on
  every redirect hop; `is_private` is NOT enough (tailnet 100.64.0.0/10 reports
  non-private — use `is_global`). `src/luxe/web/` is the ONLY web/browser stack
  (absorbed `browser.py` 2026-08-03) — don't add a second.
- **`--ephemeral` / `/ephemeral` leaves nothing behind (2026-08-11;
  `luxe.ephemeral`).** Per-site suppression of every luxe write site (sessions,
  runs, `.luxe/memory.md`+facts, reports, gitkit, caches). **Keep that list
  complete — `update_ledger` runs every turn and an unguarded writer recreates
  the session dir.** `--ephemeral --resume` is an error. Does NOT touch the
  write tools (repo edits still happen; this is about what luxe RECORDS). NOT
  implemented by redirecting `luxe_home()` (luxe.sdd forbids it).
- **Read-only default ≠ missing capability.** Full mutation surface
  (`write_file`/`edit_file`/`bash`) exists; `make_read_only_role` strips it
  until `/write`. The agent honestly reports the gate.
- **`/ctx <small|medium|large|xlarge>`** clamped to the role's `num_ctx_max`
  (`0` = no expansion); NOT dynamic. Benchmark/maintain ignore it.
- **`/bash` toggles unrestricted shell (chat-only; default OFF = hardened
  allowlist).** ON + write → `make_bash_fn(unrestricted=True)`. The default
  `TOOL_FNS["bash"]` and the benchmark path stay allowlisted (tools.sdd).
- **Status bar (`chat/status.py`):** order `path·git·ctx·cache·start·last·
  write·bash·web·[eph]·slot·model`. Provenance glyph (`chat/origin.py`): `⌂`
  local disk · `☁` network/cloud-sync · `⇅` remote. Colours follow the ACTIVE
  Claude statusline theme via `chat/theme.py`, which reads ONLY
  `~/.claude/statusline-theme` (the name file — NOT the memory subsystem; the
  `~/.claude` prohibition is scoped to context/memory). `luxe chat --dev`
  starts write+bash ON. Hidden exit aliases `/exit`, `/q`.
- **`luxe compare run/review`** — side-by-side single-task comparison, blind + vote.
- **`src/luxe/memory/`** — `~/.luxe/sessions/` transcripts + curated-first
  project memory (`.luxe/memory.md`); must NOT read `~/.claude/` or repo
  `CLAUDE.md`. Two MACHINE-MANAGED fenced blocks (2026-08-04): `luxe:brief`
  (`luxe init`/`/init`) and `luxe:notes` (`chat/notes.py`). **`memory.project.splice_block`
  is the ONLY writer — it replaces just its own block and preserves every other
  byte (tests prove it); `facts.jsonl` is never touched.** Notes failure is a
  SILENT skip that must never block exit. Config `notes: true|false`.
- **`backend.py` streaming is gated.** `run_single`/`run_agent` take an
  `on_token`; set → stream (chat live tail). **Benchmark/maintain pass
  `on_token=None` → `stream=False` → byte-identical; never pass `on_token`
  from that path.**

## gitkit — repo-analysis (`luxe gitaudit` / `luxe gitchange`)

Read-only repo analysis + an apply-ready change planner. Package `src/luxe/gitkit/`;
walk `gitkit.sdd` first. TWO commands (collapsed from the original four 2026-06-07;
old names `gitsummary`/`gitreview`/`gitrefactor`→`gitaudit`, `gitplan`→`gitchange`
are hidden back-compat aliases):

- **`gitaudit`** — ONE read-only report: orientation + bugs/security + structural
  advice. Also `/gitaudit` in `luxe chat`. `--base <ref>` / `--pr <N>` switch to a
  DIFF AUDIT (internal kind `gitaudit-diff`, "Diff audit" report: change-scoped,
  no survey, never writes `map/`; tags are `likely-introduced` vs `pre-existing
  (touched code)` with the hunk-overlap prior + caveat rendered in Python — see
  `diffscope.py`). `--min-severity` filters the DISPLAY only (saved report always
  complete; honesty line counts what was hidden).
- **`gitchange`** — apply-ready structured `gitplan/v1` JSON plan (schema string
  stays `gitplan/v1` — do NOT rename) + the gated `gitchange --apply` / `luxe
  gitapply` executor (gitkit's SOLE sanctioned agent-write path, six invariants in
  `apply.py`/`gitkit.sdd`).

Both auto-route by repo footprint: small → SINGLE-PASS; large → the staged DEEP
map-reduce (`deep.py`: survey → per-chunk → synthesis, per-repo HEAD-keyed `map/`
cache). Deep re-runs are INCREMENTAL by default (2026-06-10): the v2 breadcrumb
carries blob shas + per-chunk notes cache under `map/notes/<kind>/`; only dirty
chunks re-run (sha-validated; synthesis always re-runs; loud logging;
`--no-incremental` / `--rebuild-map` escape hatches; anti-drift compaction
triggers force a full rebuild — contract in `gitkit.sdd`). Prompts are `GIT_AUDIT_*`/`GIT_CHANGE_*` + deep `GIT_SURVEY/*_CHUNK/*_SYNTH/
DEEP_FORMAT/DEEP_REDUCE` in `agents/prompts.py` (gitkit.sdd Forbids inline prompts).

**Load-bearing design finding** (validated by sweeps + a chunk-conclude A/B,
2026-06; memories `project_deep_gitplan`, `project_gitaudit_conclude_experiment`):
the champion will NOT self-package — on large chunks it rambles 55–71k chars and
never emits the report header. So **separate detection from packaging**: chunk
prompts request a concise MARKDOWN list (a JSON-only chunk contract makes it ramble
worse), and Python recovers/packages the findings deterministically
(`deep._heuristic_findings` matches the numbered-bold finding lines it emits;
`_render_report` assembles). Prevention prompts ("emit header first" / "stop
exploring") were REFUTED — do not try to prompt-discipline conclusion; improve the
deterministic recovery instead.

## Tool-call taxonomy — evidence before hardening (2026-08-04)

`scripts/toolcall_taxonomy.py` mines `~/.luxe/{runs,sessions}` (read-only) for
tool-call failure classes: schema rejects, unknown-tool dispatches, duplicate
storms, silent text-fallback drops, empty responses, aborts, turn errors.
Evidence bar: **≥5 occurrences across ≥2 distinct sessions** in the window.

```bash
uv run python scripts/toolcall_taxonomy.py --days 45 --context-days 400 \
    --out acceptance/toolcall_taxonomy_2026_08/REPORT.md
```

The 2026-08-04 run: **no class cleared the bar, so no hardening shipped** —
verdicts per candidate in
`acceptance/toolcall_taxonomy_2026_08/C2-VERDICTS.md`; the silent
text-fallback drop (the highest-expected-value candidate on paper) is
**refuted at 0 occurrences in both windows**. Run it under
`uv run` (it reads the live tool registry; plain `python3` degrades to a
static snapshot and says so). See `lessons.md` 2026-08-04 for why the
hand-written tool list had to go.

**Measurement gaps closed same day (user-approved follow-up, additive
telemetry only — see `agents.sdd` § "Tool-call telemetry events"):** the
loop now emits `tool_reject` (reason=schema|unknown_tool, name + message)
and `textfallback_drop` (dropped names) into events.jsonl, and
`backend._chat_stream` logs the same retry `decision=` line as the
non-stream path so chat outage retry history reaches debug.log. None of
this touches messages, dispatch, or control flow — the loop's model-visible
behavior is unchanged, only the records got richer. The taxonomy script
prefers direct events and suppresses its legacy proxies so mixed corpora
(records straddling 2026-08-04) never double-count.

## Architecture: SpecDD Lever 2 `.sdd` chain

Every directory of consequence has a `<dir>/<dir>.sdd` contract listing
**Must / Must not / Owns / Forbids**. Walk the chain when editing:

- `src/luxe/luxe.sdd` — root invariants (no swarm/micro/phased; temp=0; pinned work_dir; no MoE Instruct-2507; no `origin/<branch>` reads)
- `src/luxe/agents/agents.sdd` — prompt registry is the single source of truth
- `src/luxe/tools/tools.sdd` — honesty guards + Forbids enforcement order
- `benchmarks/maintain_suite/maintain_suite.sdd` — bench rules (vacuous_test gates, `--keep-loaded`, sidecar regrade)

Read the relevant `.sdd` before editing any file under that subtree.

## Default-ON: TieredCompact context compaction

`LUXE_TIERED_COMPACT` defaults to **ON** as of 2026-05-28 (forge-hybrid cycle
closeout, commit `9be486c`). All `run_agent` callers — SWE-bench,
maintain_suite, BFCL — get 3-phase context compaction at
`phase_thresholds=(0.50, 0.85, 0.95)`. Validated at n=75 × 2 reps: resolves
equivalent to baseline (within noise), ~42-56% wall reduction, zero new
damages. It is the largest behavior change shipped in 2026-05.

- **Disable for ablation**: `LUXE_TIERED_COMPACT=0` — the first knob to try
  when a workload behaves unexpectedly (see the shared ablation list under
  "server-truth context calibration").
- **Retune**: `LUXE_TIERED_COMPACT_PHASE_THRESHOLDS="p1,p2,p3"` or
  `LUXE_TIERED_COMPACT_THRESHOLD=<f>` (single-knob, sets all 3 phases).
- See `src/luxe/agents/agents.sdd` § "forge-hybrid Phase 2 (A) compaction
  invariants" for the pinned tuning rationale + counter-discipline rules.

## grep was silently dead in every non-interactive run (fixed 2026-08-12)

`_grep` shells to `rg PATTERN` with no path argument. In that form ripgrep
reads **stdin** when stdin is an inherited pipe — EOF, no matches, exit 1 —
which luxe reported as `(no matches)`. Silently, for every search, in every
non-interactive session: benchmarks launched from a script, CI, `luxe smoke`,
and the headless `printf 'msg\n/quit\n' | luxe chat` form. TTY sessions were
fine, which is why it survived. Fixed with `stdin=subprocess.DEVNULL` (do NOT
append a `.` path — it prefixes results with `./` and changes the format the
golden request pins). **Any benchmark number produced by a piped run predates
working grep.**

## Tool limits are announced, not discovered by failing

- `list_dir`/`glob` annotate files `read_file` would refuse (`_oversize_note`)
  — only oversized ones, so ordinary listings stay byte-identical.
- `grep` reports both its caps (`--max-count=150` per file, 32 KB output).
- `read_file` states the windowed call to make, and refuses a single
  over-budget line by naming `grep` instead of looping.
- **The read cap scales with ctx** — `budget_for_ctx()` / `set_read_budget()`,
  **ON by default on BOTH paths**; opt out with the exact string
  `LUXE_TOOL_BUDGET_CTX=0`. The fixed 256 KB predates the `/ctx` tiers: in real
  tokens one max-size read is 480% of the DEFAULT 32K window and 60% of the
  largest window luxe can open, so scaling with ctx means scaling DOWN (~13 KB
  at 32768). Two call sites, two dated arms: maintain/bench 2026-08-12
  (`acceptance/toolbudget_ab_2026_08_12/REPORT.md`, once per pipeline), chat
  2026-08-24 (`acceptance/chat_bigread_2026_08_24/REPORT.md` — OFF hung
  unrecoverably at both windows, ON completed and used the `offset=` resume;
  set per turn). Don't merge the call sites; BFCL is still unwired and inert.

## Wire format: vendor fields are TOP-LEVEL, never `extra_body`

`extra_body` is an OpenAI **SDK** convention — the SDK flattens it before
sending. `backend.chat` posts raw JSON, so nesting emitted a literal
`{"extra_body": {...}}` field no server reads. `num_ctx` and `repeat_penalty`
were both dropped on the wire for the life of the file; that is why **C10's
repeat_penalty result is retracted** (inert by construction, not measured —
RESUME.md). Fixed 2026-08-11: top level, and `repeat_penalty` goes out under
both spellings (llama.cpp `repeat_penalty` / oMLX `repetition_penalty`).
Note `num_ctx` reaching the server is **not** evidence the window was
negotiated — oMLX has no per-request context knob at any spelling and enforces
the model's native length with a 400.

## Default-ON: truncated-turn retry

`LUXE_TRUNCATED_TURN_RETRY` defaults to **ON** as of 2026-08-10. A turn that
hits `max_tokens_per_turn` returns `finish_reason="length"` and, mid-prose,
carries no tool call. The loop's terminal test (`if not tool_calls:`) never
consulted `finish_reason`, so a CUT-OFF turn was indistinguishable from a model
that finished and chose to answer: the run ended `aborted=False` with no diff,
no gate fired, and the harness recorded a clean completion. The loop now
replays the cut-off text, nudges, and continues — bounded at 2 retries.

- **Disable for ablation**: `LUXE_TRUNCATED_TURN_RETRY=0` (only the exact
  string "0"). One of the shared first-things-to-try (see calibration below).
- **Bound the cost**: `LUXE_TRUNCATED_TURN_MAX_RETRIES=<n>` (default 2, the
  benched value; malformed degrades silently). Each retry is a full capped
  generation (~2.5 min at 8,192 tokens), so a rambling chat turn can spend
  ~8 min before ending. `=0` never fires but leaves the mechanism ON (keeps
  `retry_enabled=True`, so still distinguishable from an ablation in the
  corpus). Bench/maintain must keep the unset default.
- **It says so out loud now** (2026-08-11): `run_agent`/`run_single` take a
  display-only `on_notice` callback (chat wires it to the transcript); default
  `None` = benchmark path unchanged.
- **Benched** 3 reps × 10 × 2 arms: 27/30 → **30/30**, zero regressions. But
  **the evidence is narrow** — it fired 3 times, all on the one capped-turn
  `implement` fixture the suite holds: no-harm is broad, it-helps is n=1. See
  `agents.sdd` § "Truncated-turn retry" +
  `acceptance/truncated_turn_ab_2026_08_10/REPORT.md`.
- `terminal_turn_truncated` telemetry stays UNGATED — it is the only record
  distinguishing "finished" from "cut off".

## Default-ON: server-truth context calibration

`LUXE_CTX_SERVER_TRUTH` defaults to **ON** (shipped 2026-08-12, `967124d`;
**benched same day — HOLD**). `estimate_tokens` (len//4) reads 2-3.7× low on
code and tool JSON, so every compaction threshold was firing near 1.0-1.9 of
the real window and phases 2/3 could never fire before the server rejected
the prompt. The loop now recalibrates each step from the response's
`usage.prompt_tokens` and folds the ratio into `calibrated_ctx_limit`; the
pinned thresholds keep their values, what changes is what the fraction
means.

- **Benched** 3 reps × 10 on shipped defaults = **30/30**, identical score to
  the pre-change references at less wall and fewer tokens; compaction fires
  more often and reaches phase 3 (early-and-small replaced late-and-huge). See
  `acceptance/ctx_server_truth_2026_08_12/REPORT.md`.
- **Disable for ablation**: `LUXE_CTX_SERVER_TRUTH=0` restores the
  pre-2026-08-11 estimate reading exactly.
- **Shared first-things-to-try** when a workload behaves unexpectedly: disable
  the three default-ON levers — `LUXE_TIERED_COMPACT=0`,
  `LUXE_TRUNCATED_TURN_RETRY=0`, `LUXE_CTX_SERVER_TRUTH=0`.

## Opt-in modes (default off, byte-identical when disabled)

Six subsystems are gated by env vars and default to **off**. Each has
invariants in its `.sdd` you must read before enabling:

- **Reflect / verify stage** (`LUXE_REFLECT=1`) — a separate `backend.chat`
  critique pass. Verify-only by default (non-perturbing). See
  `src/luxe/agents/agents.sdd` § "Reflection / verify stage invariants".
- **Adaptive policy** (`LUXE_ADAPTIVE_POLICY=1`) — convergence-score-based
  intervention-intensity modulation. **Bias-not-lock**: never gates dispatch.
  Slew-rate limited via `LUXE_ADAPTIVE_MAX_INTENSITY_DELTA_PER_STEP`. See
  `agents.sdd` § "Stage 3 / v1.11 adaptive-policy invariants".
- **Cohort priors** (`LUXE_LOAD_PRIORS=1`) — reads
  `~/.luxe/cohort-history/<instance>.json`. **Log-only in v1.11** (does not
  influence intervention intensity); promotion deferred to v1.11.1+.
- **Respond terminal tool** (`LUXE_RESPOND_TERMINAL=1`) — a `respond()` tool
  with 4 watchdog gates. Champion 0/14 adoption (n=14 smoke 2026-05-28, with
  or without prompt guidance); refute in `lessons.md`.
- **Trajectory-shape early_bail suppression** (`LUXE_EARLY_BAIL_TRAJECTORY_SHAPE=1`)
  — suppresses `early_bail` during deep localized reading with stable
  convergence. Fired 0/14 at n=14 (too narrow for this champion at
  num_ctx=32768); needs `LUXE_ADAPTIVE_POLICY=1` for `score_log`.
- **Post-write idle repeat counting** (`LUXE_POST_WRITE_IDLE_REPEATS=1`) —
  counts a repeated post-write call toward the `post_write_idle` streak
  (closes the `read_file` `_DEDUP_EXEMPT` blind spot). **REFUTED at n=2 A/Bs
  (2026-08-10): no score, firing, or wall change — keep OFF, do not re-bench.**
  See `agents.sdd` § "Post-write idle repeat counting".

If you toggle any of these on, walk the relevant `.sdd` section first —
unbiased flips can silently change benchmark behavior.

## When working on this repo

1. **Mono only.** No swarm/micro/phased — they're retired. Don't add
   feature flags to bring them back. The `Forbids:` rules in
   `src/luxe/luxe.sdd` are tool-side enforced.
2. **Prompts go through `src/luxe/agents/prompts.py`.** Never inline
   prompt strings in `single.py` or `cli.py` — variant cells un-couple
   from runtime and the bake-off becomes uninterpretable.
3. **Bench-as-truth.** Don't trust paper analysis. Run
   `python -m benchmarks.maintain_suite.run --variants <yaml>` and
   inspect every PASS by hand via the local-branch ref. See
   `RESUME.md §The bench-as-truth pattern`.
4. **`oMLX` is on `localhost:8000`**; the API key lives in the login Keychain
   (service `OMLX_API_KEY`) and `~/.luxe/secrets.env` — never in this repo.
5. **Read `RESUME.md` first** for current project state and active tasks.
6. **Read `lessons.md`** for postmortems of every historical surprise.
7. **Git: rebase, never merge.** `origin/main` enforces linear history (no merge
   commits, no force-push — admin-bypass only). Integrate remote changes with
   `git fetch` + rebase; never create a merge commit. A committed PreToolUse hook
   (`.claude/hooks/precommit-pull.sh`, wired in `.claude/settings.json`) plus
   repo-local `pull.rebase`/`rebase.autoStash` auto-rebase before each commit.
   Hardened 2026-08-12: stranded-autostash guard (a stash-apply conflict exits
   0 from git and used to silently eat uncommitted work — now popped, or the
   commit is BLOCKED with the work intact in stash) + hunk-preserving
   re-staging (saved cached patch, not whole-file re-add). See
   `lessons.md` 2026-05-25 and 2026-08-12 + memory `feedback_git_linear_history`.
8. **Never `Path.rglob`/`glob` a user-chosen root.** pathlib swallows only
   `PermissionError`, so an unreachable network dir (`OSError(ETIMEDOUT)` from
   a NAS mount or `~/Library/CloudStorage`) crashes the caller — that killed
   the chat TUI on 2026-07-29. Use `luxe.fswalk.iter_files` (os.walk-based,
   prunes vendor dirs, logs skips). `luxe.sdd` Must-not.

## When the user asks for new work

Default to the established patterns:
- New tools land with regression tests in `tests/test_tools.py`
- New prompt variants land with tests in `tests/test_prompts.py`
- New fixtures land with a `requirements:` block (SpecDD Lever 1 schema)
- New `.sdd` files follow `<dir>/<dir>.sdd` placement
- New maintain_suite fixtures that need write-time create-only restrictions
  use `forbids_create: [glob, ...]` in fixtures.yaml; the bench harness's
  `_inject_forbids_create_sdd` (run.py) writes a synthetic `<repo>.sdd`
  at the cloned-repo root and adds it to `.git/info/exclude` so the
  contract doesn't pollute fixture diffs

When in doubt, look at how the most recent shipped feature did it and
match the shape.
