# Plan: luxe operability cycle — anti-fumble · project briefs + working notes · tool-call hardening

**Executor:** Opus 5, running autonomously on m5 in `~/Downloads/luxe`.
**Verifier:** a separate Claude session will independently re-test everything against §7 after you finish — write the report it expects (§8).
**User decisions already made (do not re-ask):**
- Theme of the cycle: after a real outage, the biggest cost was **operating luxe from memory under pressure** (command/flag recall) and **setup drift** ("setup wasn't ready"). Fix: luxe must recite its own operation at point of need.
- Three workstreams ship: **A** anti-fumble layer, **B** per-repo brief + session working notes, **C** evidence-first tool-call hardening.
- **Explicitly OUT of scope:** scheduled/cron smoke, launchd jobs, alerting, email, any "constant monitoring." Do not build it, do not add TODOs for it. `luxe ready` is point-in-time, invoked by the user.
- Benchmark/maintain path stays byte-identical except where §5C explicitly allows an evidence-backed, flagged change.

---

## 0. Ground truth (verified 2026-08-04 by code exploration — trust these anchors, re-verify cheaply)

- CLI is **click**, root group `AliasedGroup` at `cli.py:46`, commands registered as `@main.command()` + `<name>_cmd`. Model a new command on `smoke` (`cli.py:1433-1464`): lazy imports, `load_config(config_path or _default_chat_config())` (`_default_chat_config` at `cli.py:983`), `sys.exit(1 if failed else 0)`.
- **`/doctor` is already session-independent in practice.** `run_doctor(session, slots, repo_path) -> Doctor` (`chat/inspection.py:339`) reads only duck-typed attrs (`session.write_enabled`, `.web_enabled`, `.project_kind`, `.index_head`, `.unrestricted_bash`; `slots.cfg/.backend/.backend_name/.model_for/.available_models/.degraded_*`). `tests/test_chat_inspection.py:215` (`doctor_ctx`) already constructs stand-ins outside a REPL. Rendering (glyphs + `→ fix` lines) lives in `_doctor` at `chat/commands.py:886`. Every check is exception-guarded; the only networked check is `update` (`buildinfo.fetch_origin(timeout_s=4)`, quiet-OK offline).
- `luxe check` (`cli.py:2058`) is a thin oMLX+models check; `luxe smoke` (`chat/smoke.py:389 run_smoke`) is the generation-level drill. **No `luxe ready`, no `luxe init`, no OUTAGE doc exist anywhere** (grepped).
- Chat commands: handlers dict from `_build_handlers()` (`chat/commands.py:141`), help table `_HELP_ROWS` (`commands.py:57-99`). **These are two independent lists — nothing enforces parity today.**
- Self-explaining-gate precedents to match: `make_bash_fn(restricted_hint=True)` prefix (`tools/shell.py:205,231-233`, markers at `:184`); `READ_ONLY_CHAT_HINT` / `NO_PROJECT_CHAT_HINT` (`agents/prompts.py:378,390`) injected via `ChatSession.build_extra_context` (`chat/session.py:164-179`); `/web` availability reporting (`commands.py:1201-1247`, `inspection.py:310`); `/tools` gated-tool listing (`commands.py:600-687`).
- Memory subsystem (`src/luxe/memory/`): `.luxe/memory.md` read by `load_memory` (`memory/project.py:116`), injected curated-first via `render_block(max_chars=4000)` (`project.py:169`) as `<project_memory>` from `chat/session.py:147-149`. Auto facts (`confidence="auto"`) are stored but **never injected** until promoted (`project.py:81,146`). **Nothing writes memory.md programmatically today** — only `/memory edit` opens $EDITOR (`commands.py:1541`). `memory.sdd` Must-nots: never read `~/.claude/` or repo `CLAUDE.md`; never modify `agents/prompts.py`/`agents/loop.py` *from the memory package*.
- gitkit survey = ready-made brief generator: deep-mode Stage 0 at `gitkit/deep.py:1301-1319` builds `survey_ctx` (`gather_repo_health` + `build_repo_summary().render()` + `_framing_block(framing_files(target))`, `framing_files` at `deep.py:359`) and runs one read-only mono pass with `GIT_SURVEY_HINT` (`agents/prompts.py:528`). Cached survey notes: `load_map(target, head)` (`deep.py:766`) → `~/.luxe/reports/<hash>/map/survey_notes.md`, zero model calls. `gitkit.sdd:164` forbids inline prompts — new prompts go in `agents/prompts.py`. gitkit already owns the sanctioned repo-`.luxe/` write precedent (`store.mirror_to_repo`, `gitkit.sdd:44-49`).
- Session artifacts for mining: `~/.luxe/runs/<run_id>/events.jsonl` (`run_state.py:122 append_event`; `tool_call` events at `loop.py:1458/:1555` carry `phase, step, name, key_hash, duplicate, cached, bytes_out`; also `spec_*`, `respond_*`, retry-adjacent kinds); `~/.luxe/sessions/<id>/transcript.jsonl` (`memory/session.py:102 append_turn`; assistant records carry `run_id = f"{session_id}-{turn_idx}"` — the join key, `repl.py:650` — plus `steps`, `tool_calls`, `backend`; `error` records at `repl.py:375,396`, `tui.py:587,615`); `debug.log` plain-text (`chat/debuglog.py`, format at `:24`; backend retry reasons are enumerated strings in `backend.py:169-365`, e.g. `transient-<Exc>`, `5xx-transient-<marker>`).
- Tool-call handling today: text-fallback parser `_parse_text_tool_calls` (`loop.py:95`) **silently drops** candidates whose name isn't known; name `.strip()` at `loop.py:1403`; schema validation `validate_args` (`tools/base.py:117`) checks required keys + primitive types only (**no enum/nested checks**) and — asymmetry — runs **only when the name is in `tool_def_map`** (`loop.py:1406-1418`; on reject: `schema_rejects += 1`, error message back, tool not executed); unknown name at dispatch → `"Unknown tool: <name>"` error string (`tools/base.py:76-91`). No tool-level retries — the model self-corrects from the error message, bounded by step budget + dedup (`loop.py:1429-1470`).
- Test harnesses to reuse: `tests/test_chat_commands.py:18-70` (FakeBackend + `ctx` fixture + `_text()`), `tests/test_chat_inspection.py` (`doctor_ctx`), `tests/test_memory.py` (`isolated_home`), `tests/test_gitkit.py:306-421` (monkeypatched `run_single` driving `run_git_report`).
- House rules (CLAUDE.md — read it first): prompts only via `agents/prompts.py`; every touched dir's `<dir>.sdd` walked and updated; no `Path.rglob` on user roots (use `luxe.fswalk`); rebase-only git; suite green before push.

---

## 1. Workstream A1 — `luxe ready` (host-level "can I work right now?")

New top-level command `luxe ready`, registered next to `smoke`.

1. **Refactor, don't fork:** extract the doctor rendering from `chat/commands.py:886 _doctor` into a shared helper (suggest `inspection.render_doctor(doc, console)`) so `/doctor` and `luxe ready` print identically. `/doctor` behavior must not change (existing tests in `test_chat_inspection.py` + `test_chat_commands.py` stay green unmodified except imports).
2. `ready_cmd` builds the standalone context the way `doctor_ctx` does in tests: `load_config(...)`, construct `Slots` for this host's manifest, resolve the project from cwd via `chat/project.py` (no index build — `ready` must be fast; the index check may report "no index" with the fix line, that's correct behavior), a minimal session stand-in (write/bash/web all off — but suppress or mark the `mode` line as N/A outside a session rather than implying the user's session is read-only; small render tweak, your call, note it in the report).
3. Output = the same glyph table + `→ <fix>` lines, then a verdict: `READY` / `READY (warnings)` / `NOT READY`, and one closing line: `full generation drill: luxe smoke` (and `luxe smoke --chat --code` for the agentic drill). Exit 0 on ok/warn, 1 on any FAIL (matches smoke).
4. Every FAIL/WARN `fix` string must be a **runnable command**, not a description (audit existing `Check.fix` strings while you're in there; tighten any that aren't copy-pasteable).
5. Offline discipline: unchanged from doctor — the `update` check stays the only networked line and stays quiet-OK offline. `ready` must produce a verdict with the network fully down.
6. Options: `--config` (as smoke), `--backend <name>` (reuse smoke's drill-backend resolution if cheap, else skip and note). Alias `luxe doctor` → `ready` via `apply_aliases` (`cli.py:66`).
7. Tests: new `tests/test_cli_ready.py` (or extend `test_chat_inspection.py`) — READY on the healthy fake, NOT READY + fix lines when the fake backend is down / model missing from catalog / key absent; exit codes; render parity with `/doctor` (same `Doctor` in → same lines out).

## 2. Workstream A2 — the outage card: `OUTAGE.md` + `luxe outage`

1. New repo-root **`OUTAGE.md`** — the ≤120-line offline emergency card. Content (terse, command-first, tables over prose):
   - "Anthropic is down. Do this:" — `luxe ready` → fix lines → `luxe chat` / `luxe code` (and the `luxe-chat`/`luxe-code` wrappers), `--repo`, `--backend`, `--dev`.
   - Gates table: `/write`, `/bash`, `/web`, `/ctx`, what each unlocks, what the default is.
   - Per-host cheat sheet: m1 / m4 / m5 interactive main+fallback, the m5 capacity opt-in incantation (`/model all GLM-4.5-Air-4bit`), `--backend m5` from a weak host.
   - Recovery: `luxe pull` (mount-first), `luxe update`, `luxe unload`, `luxe smoke [--chat --code] [--backend m5]`, dangling-weights signature.
   - Forensics: `~/.luxe/sessions/<id>/debug.log`, `transcript.jsonl`, headless REPL piping (`printf 'msg\n/quit\n' | luxe chat --repo <dir>`).
   - Source the facts from CLAUDE.md/README — **do not invent flags; verify each one against `--help` output while writing.**
2. New `luxe outage` command: prints OUTAGE.md to the terminal (Rich markdown render when tty, plain text otherwise). Zero network, zero model, works with oMLX down — it must import nothing that phones home. Also add chat command `/outage` (same content into the RichLog/console) — cheap since the file read is shared.
3. Wire discoverability: `luxe ready`'s NOT READY verdict line mentions `luxe outage`; README gets a 3-line section; `luxe --help` epilog mentions it if click makes that cheap.
4. Tests: `luxe outage` exits 0 and output contains sentinel strings; `/outage` via the `ctx` harness; a test that OUTAGE.md exists and every `luxe <sub>` command it names is a real registered command (parse the group's command list — this keeps the card from rotting).

## 3. Workstream A3 — gate-hint completeness + command-surface parity

1. **Parity test (the drift killer):** new test asserting `_build_handlers()` keys ≡ `_HELP_ROWS` names ∪ documented-hidden-aliases (`/exit`, `/q`, gitkit short aliases…). Keep an explicit allowlist constant next to `_HELP_ROWS` so intentional hiding is declared, not accidental.
2. **Unknown-command suggestions:** `dispatch` (`commands.py:130`) on unknown `/foo` suggests the closest match (`difflib.get_close_matches` over handler names) before "Try /help".
3. **Gate-hint audit:** walk every user-visible refusal/gated path and confirm the failure text names its unlock. Known-good (leave alone): restricted bash, read-only writes, `/web` off, no-project, gitkit-no-repo, model-can't-tool. Audit and fix if silent: `/attach` over-cap refusals, `/pull` without `--yes`, `/model` naming a hidden-but-served model (should say it's hidden by `visible_models`, not "unknown"), `/backend` health-fail, MCP tool called when no `--mcp` was passed at startup (if the surface allows the model to even try, the refusal should say "MCP servers attach at startup: restart with --mcp <name>"). For each gap fixed, a test in the matching test file. **Do not touch benchmark-path error strings** — chat-only surfaces only (the `restricted_hint` seam pattern).

---

## 4. Workstream B — `luxe init` brief + session working notes

### B1. `luxe init` — draft `.luxe/memory.md` per repo

1. **Placement: gitkit** (`src/luxe/gitkit/brief.py`) — it owns the survey machinery, the read-only role pattern (`runner.py:275-290`), and the sanctioned repo-`.luxe/` write precedent. The memory package stays read-path-only (its `.sdd` is untouched except a cross-reference note).
2. New prompt registry entries in `agents/prompts.py`: `GIT_BRIEF_HINT` — instructs a **≤50-line project brief**: what this is, stack, layout (top modules + what each owns), how to run/tests, invariants & gotchas, current-state pointers. Grounded in the same `survey_ctx` inputs as `GIT_SURVEY_HINT`; explicitly "no findings, no risk audit — orientation only." Register in `tests/test_prompts.py` alongside the other gitkit hints (`:630` pattern). Update `gitkit.sdd`'s prompt index (`gitkit.sdd:164-173`).
3. Flow of `luxe init [path]` (and chat `/init`): resolve repo root (reuse `chat/project.py` resolution; refuse `$HOME` and no-project — same rule as chat) → if a fresh deep-map cache exists (`load_map` head-match), offer its survey notes as grounding context for free; else build `survey_ctx` exactly as `deep.py:1301-1319` does → **one** read-only `run_single` pass with `GIT_BRIEF_HINT` → write result into `.luxe/memory.md` inside fenced markers:
   ```
   <!-- luxe:brief begin (auto-drafted 2026-08-04, `luxe init` — edit freely above/below; re-init replaces only this block) -->
   ...
   <!-- luxe:brief end -->
   ```
4. **Write semantics (the contract):** user-curated text is preserved byte-for-byte; the fenced block is replaced in place if present, else appended at **end of file** (curated-first ordering in both the file and `render_block`). Brief hard-capped at 2,000 chars on write (deterministic truncation with a `…` marker — don't trust the model to count). If memory.md doesn't exist, create it with a one-line header comment + the block. Never touch `facts.jsonl`.
5. Re-run is idempotent: `luxe init` on an already-briefed repo replaces the block (stamp the date); `--dry-run` prints instead of writing.
6. Budget note: `render_block`'s `max_chars=4000` stays as-is; 2,000-char brief + curated text fits the common case, truncation favors the top (curated) which is correct.
7. Tests (`tests/test_gitkit_brief.py` or extend `test_gitkit.py` with the monkeypatched-`run_single` pattern): block written, curated text preserved on re-init, idempotent replace, cap enforced, no-project refused, CLAUDE.md never read (assert on the built context — memory.sdd discipline extends here), `/init` via the chat harness.

### B2. Session working notes — luxe remembers what it did

1. New module `src/luxe/chat/notes.py`. On session end (`/quit` and its aliases, both REPL and TUI `finally` paths) and on-demand via `/note`:
   - Skip silently if: no project attached, or < 2 assistant turns, or config disables it.
   - One `backend.chat` call (not `run_single` — no tools needed) over a compact digest of the session: reuse the fold/summarize machinery (`chat/summarize.py` + `fold.jsonl`) for input, never the raw full transcript. Output: 3–6 bullets — what was done/changed, what was tried and failed, open threads. Cap 900 chars on write.
   - Append into a second fenced block in `.luxe/memory.md`: `<!-- luxe:notes begin -->` … entries newest-first, each stamped `### 2026-08-04 <session-id-prefix>` … `<!-- luxe:notes end -->`. **Rolling window: keep the newest 5 entries / 1,500 chars total, drop oldest on write.** Same preserve-curated-text discipline as B1.
   - Print one line when written: `session notes → .luxe/memory.md (disable: notes: false in chat.yaml)`. On backend failure or user Ctrl-C during the call: skip silently, never block exit, never retry.
2. Config: `notes: true|false` in `configs/chat.yaml` (default **true**), threaded like other chat config knobs. `/note` works regardless of the toggle (explicit invocation = consent).
3. Read-only-mode nuance: writing `.luxe/memory.md` from a read-only session is sanctioned (it's luxe's own state file, precedent `gitkit.sdd:44-49`) — but say so in `chat.sdd` explicitly so it isn't "fixed" later as a gate bypass.
4. `.sdd` updates: `chat.sdd` gains the notes contract (triggers, caps, rolling window, failure = silent skip, never blocks exit, benchmark path untouched); `memory.sdd` gains a note that memory.md now has two machine-managed fenced blocks and their markers are load-bearing.
5. Tests (`tests/test_chat_notes.py`): distillation written on quit (FakeBackend), rolling window eviction, caps, curated + brief blocks preserved, skip-paths (no project, short session, config off, backend down), `/note` on demand, TUI path smoke via existing tui test harness.

---

## 5. Workstream C — tool-call hardening, evidence first

**Order is mandatory: C1 report before any C2 code. If C1 shows an empty tail, C2 is "no changes warranted" and that's a successful outcome — say so in the report.**

### C1. Mining script + taxonomy report

1. `scripts/toolcall_taxonomy.py` (stdlib only, read-only over `~/.luxe/`): joins `runs/*/events.jsonl` + `sessions/*/transcript.jsonl` (join key `run_id = f"{session_id}-{turn_idx}"`) + regex-parse of `sessions/*/debug.log`. Window: `--days 45` default.
2. Buckets to count (with per-tool and per-model breakdown where the records allow): `schema_rejects` (from loop events / result counters), unknown-tool dispatches (`"Unknown tool:"` in tool results), duplicate-call storms (`duplicate=True` density per run), text-fallback parses that dropped candidates (needs a proxy: assistant text containing `<tool_call>` with no matching `tool_call` event), backend retry reasons (enumerated strings from debug.log), `error` transcript records by exception type, empty-response turns. Note which buckets are **unmeasurable** with today's records rather than guessing.
3. Output: `acceptance/toolcall_taxonomy_2026_08/REPORT.md` — counts, top examples (session id + turn), and a ranked "worth fixing?" table with an explicit evidence bar: **a class needs ≥5 occurrences across ≥2 distinct sessions in the window to justify code.**

### C2. Fixes (only for classes that cleared the bar — cap at the top 3)

Pre-analyzed candidates, in expected-value order (confirm against C1 before building any):
1. **Silent text-fallback drops** (`loop.py:95`): when `_parse_text_tool_calls` finds a well-formed call whose name is unknown, return the fact instead of dropping, so the loop can append a `role:tool` message "Unknown tool <name> — available: <list>". Today the model gets *nothing* and may believe the call happened.
2. **`validate_args` depth** (`tools/base.py:117`): add enum membership + one level of nested-object required-key checks. Reject message must quote the offending key and the allowed values.
3. **Unknown-name validation asymmetry** (`loop.py:1406-1418`): names not in `tool_def_map` skip validation and fall through to dispatch — unify so the model always gets one consistent, informative rejection shape.

**Bench discipline for C2 (hard rule):** all three touch the benchmark path. Message-text-only improvements (clearer error strings) may ship default-on, un-benched, citing the write_file-description precedent — note them as un-benched in the report. Anything that changes **control flow** (new rejection, new message where none existed — that includes candidate 1) ships behind an env flag **default-OFF** (`LUXE_TOOLCALL_STRICT=1` style), documented in CLAUDE.md's opt-in-modes section, with the recommendation that the user flips it on for chat via config and a future bench decides promotion. Do not run a maintain_suite bench yourself — that's a user decision. Every fix: regression tests in `tests/test_tools.py` / the loop test files per `tools.sdd`.

---

## 6. Safety rails (violating any of these is a failed run)

- **No monitoring:** no cron/launchd/schedule/alert code or docs, per user decision.
- Benchmark/maintain path byte-identical except §5C's explicitly flagged items. Verify with a scoped diff review before pushing: changes under `agents/loop.py`, `tools/base.py`, `tools/fs.py`, `single.py`, `backend.py` need line-by-line justification in the report.
- Prompts only via `agents/prompts.py`; never inline in chat/gitkit/cli.
- Every touched directory's `.sdd` updated in the same commit as the behavior (`chat.sdd`, `gitkit.sdd`, `memory.sdd` cross-ref, `tools.sdd` if C2 ships anything).
- Never read `~/.claude/` or repo `CLAUDE.md` from any new context-building code path (B1/B2 especially).
- No `Path.rglob`/`glob` on user-chosen roots — `luxe.fswalk` only.
- `.luxe/memory.md` writes must never destroy user text: every write path re-reads, splices fenced blocks only, and has a test proving curated bytes survive.
- OUTAGE.md contains no secrets, no tailnet hostnames beyond what CLAUDE.md/README already publish (m1/m4/m5 short names are fine — they're already in the repo).
- Git: feature branch per workstream or one series, rebase-only, suite green (`uv run pytest`) before every push, commits in house style.

---

## 7. Acceptance criteria (what the verifier will independently re-test)

1. `luxe ready` on this healthy m5: exits 0, prints the check table + `READY`, runs in seconds, and never touches the network except the ≤4s update fetch. With `OMLX_API_KEY` env masked and a config pointing at a dead port: exits 1, every red line has a copy-pasteable fix command.
2. `luxe outage` and `/outage` print the card with oMLX stopped and network off; every `luxe` subcommand named in OUTAGE.md exists in `luxe --help`; the card-vs-CLI consistency test exists and passes.
3. Handler/help parity test exists; `dispatch("/wrte")`-style typo suggests `/write`.
4. `luxe init` on a scratch repo produces a fenced ≤2,000-char brief in `.luxe/memory.md`; hand-added curated text above the block survives a re-init byte-for-byte; `luxe init` on the luxe repo itself produces a brief a human judges accurate (verifier will read it); no-project dir is refused with a clear message.
5. A scripted session (`printf 'do X\nfollow-up\n/quit\n' | luxe chat --repo <scratch>`) writes a notes block; a second session's `<project_memory>` injection contains it (assert via `render_block` or debug.log); rolling window evicts at entry 6; `notes: false` suppresses; Ctrl-C during distillation exits cleanly.
6. `scripts/toolcall_taxonomy.py --days 45` runs against the real `~/.luxe` on m5 and its REPORT.md counts spot-check against raw jsonl (verifier samples 2 buckets by hand). Every C2 change shipped maps to a cleared evidence bar in that report; control-flow changes are default-OFF flagged; `LUXE_TIERED_COMPACT`-style docs added for any new flag.
7. Full `uv run pytest` green; `luxe smoke` still exits 0 on m5; `git log` linear; scoped-diff review of benchmark-path files matches the report's justification list.
8. README + CLAUDE.md updated for: `ready`, `outage`, `init`, `/note`/notes config, any new env flag. OUTAGE.md is discoverable from `luxe ready`'s failure output.
9. Report file (§8) exists and matches reality.

## 8. Handoff report (required)

Write `~/Downloads/luxe-operability-REPORT.md` containing: per-workstream outcomes; commit SHAs pushed (branch names); the C1 taxonomy summary table and which candidates cleared/failed the evidence bar; every benchmark-path line changed with justification; deviations from this plan with reasons; open WARNs (e.g. "candidate 2 shipped message-only, control-flow half deferred"); and the exact commands the verifier should start with. The verifier session starts from that file.
