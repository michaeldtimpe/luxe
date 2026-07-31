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
before (champion everywhere). Do not extend fan-out beyond these.

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
  fetch → show incoming → rebase onto origin/main → `uv sync --extra chat`;
  no-op when current. `/doctor`'s `update` check is the only networked doctor
  line (≤4s fetch; offline = quiet OK, never a warning — doctor runs during
  outages). Startup banners stay offline-pure (local refs only).
- gemma is out of the roster (no tool support); the bench apparatus is cold
  storage — capability re-benching only on explicit request.

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
    last · write · bash · slot · model` (`ctx N% <size>` e.g. 128K; `cache`=resident
    prompt size — no cross-turn cache; `write`/`bash` on/off; slot+model last).
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

## Opt-in modes (default off, byte-identical when disabled)

Five subsystems are gated by env vars and default to **off**. Each has
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
   repo-local `pull.rebase`/`rebase.autoStash` auto-rebase before each commit. See
   `lessons.md` 2026-05-25 + memory `feedback_git_linear_history`.
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
