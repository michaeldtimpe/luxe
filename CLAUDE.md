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
`scripts/capacity_drills.py` to re-run. Do not extend fan-out beyond these.

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

luxe is the living center of the fleet's model/agent work; the sibling repos
are satellites and their m5 clones go stale (that bit twice — see micro-mind's
lessons.md 2026-08-03 "stale clone" entry). Current reality, recorded here so
a luxe session doesn't have to rediscover it:

- **neo (A18 Pro, 8 GB) runs luxe** as of 2026-08-13. **micro-mind is retired**
  — superseded for deployment, the same way neo-llm-bench was; its checkout,
  harness, REPL and bench fixtures stay on neo as the canonical historical
  record for small-model guard design, and its `lessons.md` is the valuable
  part. luxe is now the one agent codebase fleet-wide.
- **neo's ENGINE is unchanged and is not retired**: llama.cpp `llama-server`
  in router mode (`--models-preset ~/dotfiles/luxe/neo-models.ini`, port 8080,
  LaunchAgent `com.micromind.llama-server` — the label keeps its lineage on
  purpose). Champion since the 2026-08-03 neo bake-offs:
  **`Qwen3-4B-Instruct-2507-Q4_K_M`** (GGUF, ctx 16384, q8 KV, single-model —
  no fallback pair). It replaced `qwen25-1.5b-instruct`: the 1.5B's 0% BFCL
  multi-turn floor is a size artifact (lifts at 3B, closes at 4B), and the 4B
  is the smallest model that passes luxe's real code drill on that box. Coder
  variants lost to instruct at every size; 14B is Metal-unrunnable on 8 GB.
  **MLX/oMLX is not an option there** — probed 2026-08-13 and refuted at
  production context (`acceptance/mlx_neo_probe_2026_08/REPORT.md`).
- **neo's luxe config is OUT of this repo**: `~/dotfiles/luxe/neo.yaml`
  (a `hosts.neo` manifest + a `backends.local` entry at 127.0.0.1:8080 with
  `engine: llama-server`), passed with `--config`. It lives in dotfiles
  because an earlier deploy edited `configs/chat.yaml` in place and hid it
  with `git update-index --skip-worktree` — 78 commits of drift. So **neo
  still has no `hosts:` entry in `configs/chat.yaml`, by design**; the
  statement in chat.sdd is about this repo's file, not about neo lacking a
  manifest. Commands other than `luxe chat`/`luxe code` (which the dotfiles
  wrappers cover) find it via **`$LUXE_CONFIG`**.
- **`luxe smoke` on neo now PASSES locally** — the old pin ("bare
  `luxe smoke` fails there; neo's smoke is the agentic drill against m5") is
  RETIRED. Measured 2026-08-13: `luxe ready` exit 0, `luxe smoke` READY 3s,
  `luxe smoke --chat --code` READY 38s (code drill 6 steps / 9 tool calls,
  pytest green, exactly `calc.py` changed). Drilling m5 from neo still works
  and is still useful; it is no longer the only option.
- **`engine:` on a `backends:` entry** (`omlx` default | `llama-server`)
  switches only luxe's oMLX-SPECIFIC diagnostics — endpoint label + fixes,
  the API-key check, the "weights location unreported" line, the stale-Cellar
  probe, and `luxe pull`'s refusal. It never changes the request: every
  supported engine is OpenAI-compatible and the bodies are byte-identical.
- **neo's boot persistence is a KNOWN OPEN GAP** (2026-08-13): the router's
  LaunchAgent is blocked from loading at login by macOS **Background Task
  Management** (`disposition = 9` — enabled but *not allowed*), not by
  launchd or the plist. It needs a one-time GUI approval in System Settings →
  General → Login Items & Extensions. Until then, after any reboot:
  `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.micromind.llama-server.plist`.
  Full diagnosis: `~/dotfiles/luxe/README-neo-router.md` and
  `acceptance/luxe_neo_unification_2026_08/REPORT.md`. **Do not add a
  cron/launchd watchdog** — same standing decision as `luxe ready`'s.
- **neo-llm-bench** is also marked *superseded for deployment* — canonical
  historical record for the sub-2B era; its methodology stays reusable.
- When a question touches micro-mind/neo state, trust the checkouts **on
  neo** (`ssh neo`, `~/Downloads/{micro-mind,neo-llm-bench,luxe}`) or a
  freshly fetched origin/main — never an unfetched m5 clone.

## Interactive front-end (`luxe chat` / `luxe compare`)

Added 2026-06-01 (additive; benchmark path byte-identical). See `RESUME.md`
2026-06-01 handoff + memory `project_luxe_chat_interactive_overhaul.md`.

- **`luxe chat`** — REPL. Each turn = one `run_single` call; conversation +
  project memory inject ONLY via the new `run_single(extra_context="")` seam
  (default `""` = byte-identical). Read-only tools by default (`/write` toggles).
  - **Chat is a CONVERSATION by default (2026-07-29 fix).** Every freeform
    turn gets the `chat_conversational` persona (registry variant; task
    overlay cleared); the baseline maintenance persona applies only to
    `/plan` drafting, `/goal` rounds, and `/use <slot>`-pinned turns. Do NOT
    re-key the persona on the routed slot — slot routing comes from the
    `_infer_task_type` keyword heuristic and misclassifies ordinary messages
    ("explain…", "add…", "fix…") as coding tasks; that was the "chats become
    coding sessions" bug. Slots still pick the MODEL only.
  - **Multi-backend (chat-only carve-out, luxe.sdd):** `configs/chat.yaml`
    `backends:` maps names → BackendEntry(base_url, api_key_env, timeout_s,
    stall_timeout_s, decode_stall_timeout_s, default). `/backend` lists (health ✓/✗, active), `/backend <name|n>`
    switches (health-checked; drops unresolvable `/model` overrides; never
    unloads the OLD server), `--backend <name>` picks at startup. Keys come
    from env vars only (m5 → OMLX_API_KEY_M5) — never YAML. Absent block ⇒ a
    synthesized "local" entry from `omlx_base_url`; benchmark/maintain read
    `omlx_base_url` only. m5 entry carries `timeout_s: 2400` (dense turns
    over Tailscale) — the old hardcoded Backend timeout hack is retired —
    plus `stall_timeout_s: 2400` (see "Progress deadlines" below).
    SessionMeta records backend_name/base_url; assistant transcript records
    are stamped `"backend"`.
  - **Progress deadlines — a request that stalls must not hang (B6, 2026-07-31).**
    `httpx.Timeout` is a PER-READ deadline and oMLX emits keepalives on both
    response paths (`"model":"keepalive"` SSE chunks when streaming; a bare
    `b' '` every ~10s under chunked encoding when not), so every keepalive
    resets it and no finite `timeout_s` can bound a request whose generation
    has stopped — one hung 23 min against a 600s timeout with no error and no
    log line. `Backend` therefore keeps a SECOND clock on *progress* (content
    delta / tool-call fragment / usage / finish_reason; keepalives never
    count): `stall_timeout_s` (1800s) before the first token, where a long
    prefill is legitimate, and `decode_stall_timeout_s` (120s) once tokens
    flow, where a gap is unambiguous. A stall raises `httpx.ReadTimeout` so
    the retry classifier treats it as transient. Overridable per endpoint via
    `BackendEntry`; unset = inherit Backend's default (the numbers live in
    `backend.py` alone). **Raising `timeout_s` is not a fix** — it only moves
    the symptom. This one is NOT chat-only: the non-stream path is the
    benchmark/maintain path, which could previously wedge on one fixture
    forever.
  - **`/attach <path> [...]`** stages file contents ONE-SHOT for the next
    turn: 48KB/file + 128KB/turn caps, binary refused (null-byte sniff),
    injected as `<attached_files>` just below `<system_constraints>`, cleared
    on consumption; kind="attachment" transcript records.
  - **TUI paste + resume:** multi-line pastes become a `[pasted N lines]`
    chip expanded at submit (stock Textual Input kept only the first line);
    `--resume`/`/resume` now work inside the Textual TUI (transcript replays
    into the RichLog on mount). The `[chat]` extra installs via
    `uv sync --extra chat` — without it chat falls back to the line REPL.
  - **Model roster + tool capability** (2026-07-30). `configs/chat.yaml`
    `visible_models:` is the working set `/model` offers (5 ids on m1/m5);
    everything else the server holds is hidden. Local weights carry NO glyph
    now — only ☁ network / ⇅ remote. **`chat/modelcaps.py` detects tool
    support from the chat template**: gemma-3 has none (system/user/assistant
    only + an alternation guard) and oMLX *silently drops* the `tools` array
    for it, so luxe withholds the whole tool surface and tells the model not to
    fake it. Gemma is therefore selectable (`/model chat gemma-3-27b-it-4bit`)
    but NOT the default — a default has to be able to read a file.
  - **Start a session anywhere** (2026-07-30; `chat/project.py`). The subject
    resolves to **git** (walks UP to the git root — a subdir session gets the
    whole repo), **dir** (pyproject.toml/package.json/… marker), or **none**.
    `$HOME` and anything above it never count as the project. `--repo` resolves
    upward too (the `luxe-chat` wrapper always passes `--repo "$PWD"`).
    No-project mode: **no index, no repo lock**, read tools still work,
    `bm25_search`/`find_symbol` withheld from the tool list, prompt carries
    `NO_PROJECT_CHAT_HINT`, status bar shows `no project`. `/project [path]`
    attaches or switches mid-session (moves the repo lock, acquiring the new one
    first); `/index [path]` builds the index where you are. Startup from `$HOME`
    is now **0.5s**.
  - **Startup indexing is bounded and single-pass** (2026-07-30). It used to
    walk the tree THREE times (BM25, symbols, language detection) with no cap:
    `luxe chat --repo ~` cost **210s of indexing + ~18s of language walking**.
    Now `cli._build_chat_indexes` runs `fswalk.scan_source_files` once — git
    `ls-files` when the root is a repo, else a breadth-first walk that prunes
    `HOME_NOISE_DIRS` (`Library`, …) at depth 1 — and feeds the list to both
    builders plus `_languages_from_paths`. Caps: `LUXE_INDEX_MAX_FILES` (8000),
    `LUXE_INDEX_MAX_MB` (96), `LUXE_INDEX_NO_GIT=1` to force the walk.
    Measured after: **~1s in a repo, ~16s from `$HOME`**. Truncation prints
    what the model can't see + how to lift it. Benchmark/maintain keep the
    unbounded walk (builders called without `files=`).
  - **`/pull` — get model weights** (2026-07-29; `src/luxe/modelstore.py`, CLI
    `luxe pull`). Mounted volume first (kappa/alpha over SMB — same bytes at LAN
    speed), else HuggingFace **through oMLX's own downloader**
    (`/admin/api/hf/*`; cookie session via `/admin/api/login`, Bearer alone is
    rejected). Never write a second `snapshot_download` — it would race the
    server over the HF cache. Mount imports resolve symlinks AND Synology
    `XSym` stubs (1067-byte regular files on SMB — a naive copy imports stubs
    instead of weights); dangling links abort the copy. Copies stage in
    `.<name>.partial` and rename, so an interrupt never leaves a half-model.
    In chat, `/pull <ref>` previews and `/pull <ref> --yes` transfers.
  - **Session commands added in the 2026-07-29/30 `/help` audit**: `/theme`
    (live palette switch), `/tools` (real tool surface + what read-only gates),
    `/status` (session dump incl. model origin), `/unload` (free RAM without
    quitting), `/retry` (re-run the last message via `CommandResult.submit`),
    plus `/export`, `/diff`, `/doctor` — logic in `chat/inspection.py`, all
    read-only. `/diff` defaults to the files THIS session wrote (ledger),
    diffs against HEAD, reports untracked as new; `/export` renders the
    PERSISTED transcript (survives `/resume`) to `<session dir>/transcript.md`;
    `/doctor` preflights endpoint/key/model/weights/disk/index/git/mode/TUI and
    prints the fix for every warning.
  - **MCP tools attach at STARTUP via `--mcp <name>`** (chat-only, repeatable;
    servers from `--mcp-config <path>`, default `configs/mcp.yaml`). There is no
    way to attach a server mid-session — a plain `luxe chat` has no MCP surface
    no matter what the user tells the model. The mage-hands home-lab relays are
    wired this way through the private dotfiles repo (`~/dotfiles/luxe/relays.yaml`
    + the `luxe-alpha`/`luxe-kappa`/`luxe-router`/`luxe-all` wrappers); no
    hostname or token belongs in THIS repo. Tools are namespaced
    `mcp__<server>__<tool>`; the server's `gate_tools` patterns follow the
    `/write` gate, `--mcp-read-only` drops them entirely. **Servers are isolated
    per connection** (2026-07-31): one task + one `AsyncExitStack` each, so a
    dead server can neither cancel a healthy one nor be reported "up" with no
    session. Don't reintroduce a shared exit stack, and catch `BaseException`
    (not `Exception`) at any anyio connect boundary — see `lessons.md`
    2026-07-31 and the chat.sdd MCP bullet.
  - **Web tools are `/web`-gated, default OFF** (2026-07-31; `src/luxe/web/`,
    walk `web/web.sdd` first). `web_fetch` (bounded GET → stdlib HTML→markdown;
    `render=true` = headless Chromium via the optional `[web]` extra),
    `web_search` (Brave/Tavily key through luxe.secrets; withheld when no key
    resolves), and `web_answer` (2026-08-03; Brave Answers — a SEPARATE
    product/subscription from search: one server-side grounded answer via
    the OpenAI-compatible /res/v1/chat/completions endpoint, keyed by
    `BRAVE_ANSWERS_API_KEY`, withheld independently). Gated independently of `/write` — reading a page mutates nothing
    locally. Chat-only via the extra-tool seam: **never add these to
    `TOOL_FNS`**, since a benchmark that can reach the live internet is no
    longer reproducible. The egress guard refuses non-public hosts on every
    redirect hop; note `ipaddress.is_private` is NOT enough — the tailnet is
    100.64.0.0/10 (RFC 6598) and reports as non-private, so `is_global` carries
    the check. `LUXE_WEB_ALLOWLIST` adds an optional fnmatch host allowlist on
    top (unset = any public host; set = deny-by-default for hosts).
    **`src/luxe/web/` is the ONLY web/browser stack** — it absorbed
    `src/luxe/browser.py` (`browse_navigate`/`browse_read`, `[browser]` extra)
    on 2026-08-03 after the two landed in parallel. Don't add a second stack,
    browser dependency, or gating model.
  - **`--ephemeral` / `/ephemeral [on|off]` leaves nothing behind**
    (2026-08-11; `luxe.ephemeral`). Suppresses every luxe write site:
    `~/.luxe/sessions/<id>/` (transcript, fold, meta, **ledger**, debug.log),
    `~/.luxe/runs/<id>/events.jsonl`, `<repo>/.luxe/memory.md` +
    `facts.jsonl`, `~/.luxe/reports/` + **`<repo>/.luxe/gitkit/`**, gitkit
    deep's `map/` cache, `~/.luxe/cve_cache/`, `~/.luxe/mcp_audit.jsonl`.
    Keep that list complete — `update_ledger` is exposed on every chat turn,
    so an unguarded writer silently recreates the session dir.
    `--ephemeral --resume` is an error. It does **not** touch the write tools
    (`/write` still gates those, and the agent still edits your repo — this is
    about what luxe RECORDS); reads are untouched; the repo lock is still
    taken and is the one surviving artifact. `/ephemeral` ON mid-session
    purges this session's own dirs and prints what it removed, but never
    `.luxe/memory.md` (curated text lives there). Status bar shows `eph on`
    at priority 1 when active. Implemented as per-site suppression, NOT by
    redirecting `luxe_home()` (luxe.sdd forbids it, and it would capture the
    read paths).
  - **Read-only default ≠ missing capability.** luxe has the full mutation
    surface — `write_file` (creates parent dirs + files, i.e. scaffolds trees),
    `edit_file`, `bash` — but `make_read_only_role` (`mcp/server.py`) strips
    `{write_file, edit_file, bash}` until `/write` flips `session.write_enabled`.
    A chat agent in read-only mode will *honestly report it has no file-creation
    tool*; that's the gate, not a gap. The read-only `<session_mode>` hint now
    tells it to point the user at `/write`. See `lessons.md` 2026-06-01 + memory
    `feedback_luxe_dev_platform_write_mode`.
  - **Context window is `/ctx <small|medium|large|xlarge>`** (chat-only),
    clamped to the role's `num_ctx_max` (`configs/chat.yaml`; `0` = no
    expansion). NOT dynamic/auto — high pressure only *suggests* the next tier.
    Benchmark/maintain ignore `num_ctx_max`.
  - **`/bash` toggles unrestricted shell** (chat-only dev mode; default OFF =
    hardened allowlist). When ON + write mode, the turn swaps in
    `make_bash_fn(unrestricted=True)` via `run_single`'s extra-tool seam — any
    command, chains/pipes/redirects, cwd=repo root but NOT sandboxed. The default
    `TOOL_FNS["bash"]` and the benchmark path stay allowlisted (`tools.sdd`).
  - The REPL shows a randomized rainbow banner + per-render color-shifting prompt
    arrows; the footer carries `tok/s` and start/end timestamps + elapsed
    (`chat/render.py`).
  - **Status bar** (`chat/status.py`): order `path · git · ctx · cache · start ·
    last · write · bash · web · [eph] · slot · model` (`eph on` appears only
    when `/ephemeral` is active; `ctx N% <size>` e.g. 128K; `cache`=resident
    prompt size — no cross-turn cache; `write`/`bash`/`web` on/off, all three
    always visible; slot+model last).
    The model name carries a PROVENANCE glyph (`chat/origin.py`, 2026-07-29):
    `⌂` weights on local disk · `☁` network volume / cloud-sync tree · `⇅`
    remote endpoint (warn-coloured for the last two; no glyph when unknown).
    Same fact is stated at startup, in `/model`'s listing, and on a weight
    swap. One cached `/v1/models/status` probe per endpoint, resolved off the
    render path; failures degrade to `unknown`.
    Palette: path blue (fixed hex), slot purple, model yellow, state on=green/off=red,
    ctx/write/bash labels in default fg, grey else; git keeps the theme's role
    colours. Startup banner minimal (bar shows repo/slot/model/mode). `fields()`
    (→ `Segment` list with drop-`priority`) is the single source; `fit()` is
    responsive (drop low-value first → middle-ellipsis path; git/ctx/model
    protected). Live during a turn via `rich.Live` + `LiveActivity` when
    `is_terminal` (tool log scrolls above a ticking bar); falls back to line
    streaming otherwise. Colours follow the user's ACTIVE Claude statusline theme,
    resolved LIVE by `chat/theme.py` (reads `~/.claude/statusline-theme`, imports
    the user's yet-another-statusline `themes` module via the `statusline_command.py`
    symlink, converts each role's ANSI escape → ptk/Rich; ANSI 0-15 stay named so
    they track the terminal profile, 16-255 fixed). Built-in llmtop fallback if
    the repo is absent. Theming reads only the name file — NOT the memory
    subsystem (the `~/.claude` prohibition is scoped to context/memory). **`luxe chat --dev`** starts write+bash ON. Hidden exit
    aliases: `/exit`, `/q` (both = `/quit`).
  - **Flag-state failures self-explain.** Defaults are safe (read-only +
    allowlisted bash) and shown in the banner + chips; in write mode a restricted
    bash rejection front-loads "enable unrestricted dev mode with /bash" onto the
    error (`make_bash_fn(restricted_hint=True)`), so the model surfaces the toggle
    instead of retrying. Genuine errors aren't augmented. Chat-only — benchmark
    bash untouched.
- **`luxe compare run/review`** — side-by-side single-task comparison (3 modes,
  incl. luxe-vs-bare substrate ablation), blind + vote.
- **`src/luxe/memory/`** — `~/.luxe/sessions/` transcripts + curated-first project
  memory (repo `.luxe/memory.md`); must NOT read `~/.claude/` or repo `CLAUDE.md`.
  - **`.luxe/memory.md` now carries two MACHINE-MANAGED fenced blocks** whose
    markers are load-bearing (2026-08-04): `luxe:brief` (written by `luxe init`
    / `/init` — one read-only gitkit pass with `GIT_BRIEF_HINT`, orientation
    only, capped at 2,000 chars in Python) and `luxe:notes` (written by
    `chat/notes.py` at session end and by `/note` — one non-agentic
    `backend.chat` over the deterministic fold, 900 chars/entry, rolling
    window of 5 entries / 1,500 chars). `memory.project.splice_block` is the
    ONLY writer: it re-reads, replaces just its own block, appends at EOF when
    absent, and preserves every other byte — **user-curated text must survive,
    and tests prove it**. `facts.jsonl` is never touched. Notes failure is a
    SILENT skip with no retry and must never block exit; writing this file
    from a read-only session is sanctioned (luxe's own state file,
    orchestrator-side Python, same precedent as `store.mirror_to_repo`) — do
    not "fix" it as a gate bypass. Config: `notes: true|false` in
    `configs/chat.yaml` (default true; `/note` ignores it).
- **`backend.py` streaming** is gated (`stream`/`on_token`). As of 2026-06-01 the
  loop wires it CHAT-ONLY: `run_single`/`run_agent` take an `on_token` that, when
  set (interactive chat live tail), makes `backend.chat` stream. Benchmark/maintain
  pass `on_token=None` → `stream=False` → byte-identical request, deterministic
  path untouched. Do NOT pass `on_token` from the benchmark/maintain path.
- New work here walks `src/luxe/{chat,compare,memory}/<dir>.sdd` first.

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
`phase_thresholds=(0.50, 0.85, 0.95)`. Validated at n=75 across 2 reps:
resolves equivalent to baseline within substrate noise band (±2.8); 42-56%
wall reduction; 2 protected wrong_target instances healed; zero new damages.

- **Disable for ablation**: `LUXE_TIERED_COMPACT=0`. **If a workload behaves
  unexpectedly, try this first.** Compaction default-ON is the largest
  behavior change shipped in 2026-05.
- **Retune**: `LUXE_TIERED_COMPACT_PHASE_THRESHOLDS="p1,p2,p3"` or
  `LUXE_TIERED_COMPACT_THRESHOLD=<f>` (single-knob, sets all 3 phases).
- See `src/luxe/agents/agents.sdd` § "forge-hybrid Phase 2 (A) compaction
  invariants" for the pinned tuning rationale + counter-discipline rules.

## Default-ON: server-truth context calibration

`LUXE_CTX_SERVER_TRUTH` defaults to **ON** as of 2026-08-11. `estimate_tokens`
is `len(text) // 4` — fine for prose, **2-3.7× low** on the code and JSON tool
payloads an agent loop actually carries (1.84×/1.92×/2.24× across three live
turns; 2.38-3.69× through a 13-step log-analysis run where `est=19.8%` against
a true `47.3%`). Every compaction threshold divides by that estimate, so
phases pinned at 0.50/0.85/0.95 were firing near 1.0-1.9 of the real window —
phases 2 and 3 could not fire before the server rejected the prompt. The
loop now recalibrates each step from the response's `usage.prompt_tokens` and
folds the ratio into the denominator (`calibrated_ctx_limit`), so every
consumer inherits it and **the pinned thresholds keep their values** — what
changes is what the fraction means.

- **Disable for ablation**: `LUXE_CTX_SERVER_TRUTH=0` (only the exact string
  "0") restores the pre-2026-08-11 reading exactly.
- **UNBENCHED** — compaction now fires earlier on the benchmark path (at the
  context it always claimed). That's the intended correction, but it changes a
  default-ON lever validated at n=75 under the old reading. Run maintain_suite
  before treating it as settled. See `agents.sdd` § "Server-truth context
  calibration".

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
- **The read cap can scale with ctx** — `budget_for_ctx()` / `set_read_budget()`,
  `LUXE_TOOL_BUDGET_CTX=1`, **OFF by default**. The fixed 256 KB predates the
  `/ctx` tiers: in real tokens one max-size read is 480% of the DEFAULT 32K
  window and 60% of the largest window luxe can open, so scaling with ctx
  means scaling DOWN (~13 KB at 32768). Enabling it is a benchmark-path change
  and needs a maintain_suite run.

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
  string "0"). **If a workload behaves unexpectedly, try this alongside
  `LUXE_TIERED_COMPACT=0`.**
- **Bound the cost**: `LUXE_TRUNCATED_TURN_MAX_RETRIES=<n>` (default 2, the
  benched value; malformed degrades silently). Each retry is a full capped
  generation — ~2.5 min at 8,192 tokens — so a chat turn that rambles into the
  cap can spend ~8 min before ending. `=0` never fires but leaves the
  mechanism ON, which keeps `terminal_turn_truncated`'s `retry_enabled=True`
  and so stays distinguishable from an ablation in the corpus. Bench/maintain
  must keep the unset default: 30/30 was measured at 2.
- **It says so out loud now** (2026-08-11): `run_agent`/`run_single` take a
  display-only `on_notice` callback that chat wires to the transcript, so a
  retry is visible while it happens instead of only in `events.jsonl`. Default
  `None` = benchmark path unchanged.
- Validated at 3 reps × 10 fixtures × 2 arms: maintain_suite 27/30 → **30/30**,
  score 111 → 120, zero regressions, +22.5% tokens / +7.8% wall, and 3/3
  firings on genuine cap hits with none spurious.
- **The evidence is narrow**: it fired 3 times, all on one fixture — the suite
  holds exactly one capped-turn `implement` trajectory. No-harm is broad,
  it-helps is n=1. See `agents.sdd` § "Truncated-turn retry" and
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

- **Benched**: 3 reps × 10 fixtures on shipped defaults = **30/30 · 120/150**
  (identical to the four pre-change references) at **12% less wall and 24%
  fewer tokens**; compaction fires more often and reaches phase 3 (21 vs 6
  firings) yet drops fewer than half the tool results — early-and-small
  replaced late-and-huge. Reproduced same day on a settled tree. See
  `acceptance/ctx_server_truth_2026_08_12/REPORT.md`.
- **Disable for ablation**: `LUXE_CTX_SERVER_TRUTH=0` restores the
  pre-2026-08-11 estimate reading exactly. If a workload behaves
  unexpectedly, this joins `LUXE_TIERED_COMPACT=0` and
  `LUXE_TRUNCATED_TURN_RETRY=0` in the first-things-to-try list.

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
- **Respond terminal tool** (`LUXE_RESPOND_TERMINAL=1`) — exposes a
  `respond(message=...)` tool with 4 watchdog gates (early-respond,
  no-writes-late, passive-surrender, compaction-phantom). Forge-hybrid
  Phase 3 (B) infrastructure; champion does not adopt the lever at any
  tested promotion (n=14 smoke 2026-05-28: 0/14 adoption with or without
  prompt guidance). Default-OFF; refute documented in `lessons.md`.
- **Trajectory-shape early_bail suppression** (`LUXE_EARLY_BAIL_TRAJECTORY_SHAPE=1`)
  — selectively suppresses `early_bail` when the model is in deep
  localized reading with stable convergence. Forge-hybrid Phase 4 (D)
  infrastructure; locked predicate fired 0/14 at n=14 smoke (too narrow
  for this champion at num_ctx=32768). Implicit dependency on
  `LUXE_ADAPTIVE_POLICY=1` for `score_log` population. Default-OFF.

- **Post-write idle repeat counting** (`LUXE_POST_WRITE_IDLE_REPEATS=1`) — makes
  a post-write REPEAT call (same tool + args as an earlier call this run) count
  toward the `post_write_idle` streak instead of resetting it. Closes a blind
  spot the guard's own docstring claimed to cover: `read_file` is
  `_DEDUP_EXEMPT`, so a repeated read returns content and resets the streak.
  **REFUTED at n=2 A/Bs (2026-08-10)** — with opportunity present it changed
  no score, no firing count and no wall; keep default OFF and do not re-bench
  on maintain_suite.
  Demonstrated on m1 (a post-edit read of an already-read key, `dup=False`,
  78 bytes, which reset the streak). It is a LATENT gap, not the cause of a
  known abort — m1's code-drill failure was a step-budget problem, fixed
  separately. **UNBENCHED — do not flip the default without a maintain_suite
  run.** See `agents.sdd` § "Post-write idle repeat counting".

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
