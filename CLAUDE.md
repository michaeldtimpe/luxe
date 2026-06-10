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

**Sanctioned exception — `luxe chat` model slots.** The interactive REPL
(`src/luxe/chat/`, shipped 2026-06-01) exposes opt-in `chat`/`plan`/`code` model
slots via `configs/chat.yaml` `slots:`. This is the ONLY sanctioned per-work-type
model selection (carve-out noted in `src/luxe/luxe.sdd`). It defaults to
champion-everywhere (no fan-out; byte-identical model selection), and is scoped
to the interactive front-end — never the benchmark/maintain path. Do not extend
fan-out beyond this.

## Interactive front-end (`luxe chat` / `luxe compare`)

Added 2026-06-01 (additive; benchmark path byte-identical). See `RESUME.md`
2026-06-01 handoff + memory `project_luxe_chat_interactive_overhaul.md`.

- **`luxe chat`** — REPL. Each turn = one `run_single` call; conversation +
  project memory inject ONLY via the new `run_single(extra_context="")` seam
  (default `""` = byte-identical). Read-only tools by default (`/write` toggles).
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
4. **`oMLX` is on `localhost:8000`** with key `OMLX_API_KEY=omlx-sdb25582k3mq8pf9`.
5. **Read `RESUME.md` first** for current project state and active tasks.
6. **Read `lessons.md`** for postmortems of every historical surprise.
7. **Git: rebase, never merge.** `origin/main` enforces linear history (no merge
   commits, no force-push — admin-bypass only). Integrate remote changes with
   `git fetch` + rebase; never create a merge commit. A committed PreToolUse hook
   (`.claude/hooks/precommit-pull.sh`, wired in `.claude/settings.json`) plus
   repo-local `pull.rebase`/`rebase.autoStash` auto-rebase before each commit. See
   `lessons.md` 2026-05-25 + memory `feedback_git_linear_history`.

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
