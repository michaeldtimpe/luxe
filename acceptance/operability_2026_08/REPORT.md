# luxe operability cycle — execution report

**Executor:** Opus 5, autonomous, on m5 in `~/Downloads/luxe`.
**Date:** 2026-08-04.
**Plan:** `~/Downloads/luxe-operability-plan.md` (the contract).
**Branch:** `feat/operability`, pushed to `origin`. Linear, no merge commits.

| | |
|---|---|
| Suite | `uv run pytest` → **2218 passed, 1 skipped, 4 deselected** (was 2141 at branch point; +77 tests) |
| `luxe ready` (m5) | **exit 0**, READY, ~1s |
| `luxe ready --config <dead port>` | **exit 1**, NOT READY, every ✗/! carries a runnable fix |
| `luxe outage` / `luxe doctor` | **exit 0** |
| `luxe smoke` (m5) | **exit 0**, READY (9s), all 10 steps ✓ |
| Benchmark path | **byte-identical** — `git diff origin/main -- agents/loop.py tools/base.py tools/fs.py agents/single.py backend.py` is EMPTY |
| Monitoring/cron/launchd | **none added**, per §6 |

## Commits (oldest → newest)

| SHA | Workstream | Subject |
|---|---|---|
| `ad23d08` | A | anti-fumble layer — `luxe ready`, the offline card, gate hints |
| `c69cecf` | B | per-repo brief + session working notes in `.luxe/memory.md` |
| `f4d4336` | C | mine `~/.luxe` for tool-call failure classes — no hardening warranted |
| `e4f9ecf` | B fix | the distillation read the wrong field, and both narrated |
| `4c9b40e` | B fix | no bullets recovered ⇒ write nothing, and give the answer room |

The last two are live-drill fixes, described in "Two bugs the live drills
caught" below. They are worth reading before anything else in this report.

---

## Workstream A — anti-fumble layer (`ad23d08`)

### A1 · `luxe ready`

- **Refactored, not forked.** `/doctor`'s rendering moved out of
  `chat/commands.py:_doctor` into `inspection.render_doctor(doc, console,
  title=…)`; `/doctor` now calls it. A test drives the same `Doctor` through
  both call sites and asserts the output lines are identical below the title.
- `cli.build_ready_doctor(cfg, repo_path)` builds the host-level `Doctor`
  against a stand-in `ChatSession` + `SlotManager`, then
  `inspection.hostwide_view(doc)` restates the three session-scoped lines
  (`mode`, `web`, `search index`) as "n/a outside a session" and clears their
  fixes so a stand-in's state can never colour the verdict. **This is the §1.2
  render tweak the plan left to my judgement** — I neutralised all three
  rather than just `mode`, because "search index: not built" would otherwise
  be a permanent WARN on every `ready` run (it never indexes, by design).
- Verdict + exit codes: `READY` / `READY (warnings)` → 0, `NOT READY` → 1,
  unknown `--backend` → 2 (matches `smoke`). Closing line names
  `luxe smoke` and `luxe smoke --chat --code`; the NOT READY line names
  `luxe outage`.
- Options: `--config`, `--backend <name>`, plus `--repo <path>` (added — the
  project checks need a subject and `ready` must not depend on cwd).
  `luxe doctor` is an alias via `apply_aliases`.
- **Offline discipline intact:** the ≤4s `update` fetch is still the only
  networked line; verified by running with a dead-port config.

**§1.4 `Check.fix` audit — every string tightened to a runnable command:**

| check | before | after |
|---|---|---|
| API key | `set OMLX_API_KEY (see ~/.luxe/secrets.env)` | `` `echo 'OMLX_API_KEY=<key>' >> ~/.luxe/secrets.env` `` |
| weights (network) | "loading streams them over the network…" | `` `luxe pull <model>` to copy them to local disk `` |
| weights (remote) | "prompts and weights cross the network" | `` `luxe pull <model>` here, then `--backend local` `` |
| disk | "a weight swap wants ~40 GB of headroom" | `` `luxe pull --list` then `luxe pull <m> --remove` `` |
| search index | "restart chat in the repo you want indexed" | `` `/index` in chat, or `luxe chat --repo <path>` `` |
| index freshness | "restart chat to reindex" | `` `/index` to reindex (or restart chat) `` |
| git | "`/diff` and git tools won't work here" (a consequence, not a fix) | `` `git -C <repo> init` if it should be one `` |
| chat model (endpoint down) | *(no fix at all)* | "fix the endpoint above first, then re-run" |

The last row was found by a test I wrote to assert the property
(`test_every_warn_or_fail_carries_a_fix`), not by reading — it is now enforced.

### A2 · `OUTAGE.md` + `luxe outage` + `/outage`

- `OUTAGE.md` at the repo root, **105 lines** (cap 120). Six sections: can I
  work right now → gates table → per-host cheat sheet → recovery → forensics →
  if luxe itself is broken. Every flag was checked against live `--help`
  output while writing; no invented flags. No secrets, no tailnet hostnames
  beyond the m1/m4/m5 short names already in CLAUDE.md.
- `src/luxe/outage.py` is the single reader (`load_card`), importing only
  `re`/`pathlib`. `luxe outage [--plain]` renders Rich markdown on a tty and
  plain text otherwise; `/outage` prints the same bytes into a session.
  `load_card` never raises — a damaged checkout still yields four actionable
  lines.
- **Anti-rot test:** `outage.referenced_commands()` extracts every
  `luxe <sub>` the card presents as *runnable* (fenced blocks + inline
  backtick spans only — prose like "if luxe itself is broken" is excluded by
  construction) and the test asserts each is a registered command or alias.

### A3 · Parity + gate hints

- **Parity test** (`tests/test_chat_commands.py::TestCommandSurfaceParity`):
  `_build_handlers()` keys ≡ `_HELP_ROWS` names ∪ `_HIDDEN_COMMANDS`, a new
  declared frozenset next to `_HELP_ROWS`. Three assertions: no undocumented
  handler, no help row without a handler, no stale hidden entry.
- **Typo suggestions:** `dispatch` runs `difflib.get_close_matches` over
  handler names before falling back to "Try /help". `/wrte` → "Did you mean
  /write?".
- **Gate-hint audit.** Already good, left alone: restricted bash, read-only
  writes, `/web` off, no-project, gitkit-no-repo, no-tool-support model,
  `/pull` preview→`--yes`, `/pull` existing→`--force`. Fixed:

| surface | gap | now |
|---|---|---|
| `/attach` binary | "looks binary — refused" | + "ask the model to read it instead — it has read_file/bash" |
| `/attach` turn cap | "128KB total cap reached" | + "attachments are one-shot: send this turn, then `/attach` on the next" |
| `/model <slot> <explicit-id>` | accepted silently, failed at swap time a turn later | `_warn_model_not_offered` distinguishes **hidden by `visible_models:`** (supported — how the m5 capacity model is selected) from **not in this catalog** (`/pull …`) |
| `/backend` health-fail | "unreachable (staying on X)" | + "`/net` to diagnose; a remote entry needs $OMLX_API_KEY_M5 set (and `/planeproxy` up)" |
| `/tools` with no MCP | **printed nothing at all** | "MCP tools (none) — MCP servers attach at STARTUP only — restart with `luxe chat --mcp <name>`" |

All are chat-only console strings. No benchmark-path error text changed.

---

## Workstream B — brief + working notes (`c69cecf`, `e4f9ecf`, `4c9b40e`)

### B1 · `luxe init` / `/init`

- `src/luxe/gitkit/brief.py`, prompt `GIT_BRIEF_HINT` in `agents/prompts.py`
  (registered + tested in `tests/test_prompts.py`; `gitkit.sdd`'s prompt index
  updated). One read-only `run_single` pass with the gitkit read-only role.
- Grounding: a **FRESH deep-map cache** for this HEAD is reused for free when
  present (`load_map` → `survey_notes`), else `gather_repo_health` +
  `<repo_map>` + `_framing_block(framing_files())` exactly as `deep.py:1301`.
- Writes the `luxe:brief` fenced block via `memory.project.splice_block`.
  Capped at **2,000 chars deterministically** (`cap_brief`, line-boundary
  aware, explicit `…[luxe: brief truncated…]` marker). `--dry-run` prints.
  Subject resolution reuses `chat/project.py` (no-project and `$HOME` refused
  with the fix named). `facts.jsonl` never touched.

### B2 · Session working notes

- `src/luxe/chat/notes.py`, prompt `SESSION_NOTES_HINT`. One non-agentic
  `backend.chat` over the **deterministic fold** (`chat/summarize.py`), never
  the raw transcript. Splices the `luxe:notes` block: newest-first, dated +
  session-stamped entries, **900 chars/entry, 5 entries / 1,500 chars total**,
  all enforced in Python.
- Triggers: both front-ends' session-end `finally` (repl.py and tui.py),
  **before** the model unload so the backend is still usable; and `/note` on
  demand, which bypasses the config toggle and the 2-turn floor.
- Skips: no project, <2 answered turns, `notes: false` in `configs/chat.yaml`
  (new field, default `true`).
- **Never blocks exit:** the guard catches `BaseException` (Ctrl-C mid-call is
  a `KeyboardInterrupt`; anyio cancels are worse) and re-raises only
  `SystemExit`. No retry. Silent on failure except a `logger.info` to
  debug.log.

### The `.luxe/memory.md` write contract

`memory.project.splice_block` is the **single writer** for both blocks:
re-read → replace only between this block's own markers → append at EOF when
absent (so curated text stays first, matching `render_block`'s truncation
priority) → temp-file + rename. `read_block`/`block_markers` round it out.
Proven by tests: curated bytes survive a re-init **byte-for-byte** (including
non-ASCII), text below the block survives, the two blocks coexist
independently, `facts.jsonl` is untouched, and no new context builder reads
`CLAUDE.md`.

Writing this file from a read-only session is **sanctioned** and now says so
explicitly in `chat.sdd` so it isn't "fixed" later as a `/write` bypass.

### Two bugs the live drills caught (and what they cost)

Both features shipped with green unit tests and **did nothing useful live**.
This is the most important finding in the report.

1. **`distil` read `resp.content`; `backend.ChatResponse` exposes `.text`.**
   Session notes wrote *nothing* on every real session — silently, no error,
   no log, indistinguishable from the feature being off. The test's
   hand-rolled `_Resp` stub was written from the same wrong assumption, so 23
   tests *confirmed* the bug. Fixed; the fakes now build the real
   `ChatResponse`, and a test asserts it has no `.content` so the contract
   can't drift back.
2. **The champion narrated instead of complying.** Notes wrote "Here's a
   thinking process: 1. **Analyze User Input** …" into permanent project
   memory; the brief opened with "Now I have enough information to write the
   brief. Let me compile it." This is the *already-documented*
   conclude-discipline finding (memory `project_gitaudit_conclude_experiment`:
   prevention REFUTED, deterministic recovery is the fix) — it had simply
   never been generalised past gitkit. Added `notes.extract_bullets` (last
   contiguous run of **column-0** bullets: the trace nests its bullets under
   numbered headers, so column 0 separates answer from thinking) and
   `brief.strip_preamble` (slice from the first requested section heading).
3. **The recovery's own fallback was the next trap.** `extract_bullets` first
   fell back to the raw text when it found no bullets — which is exactly what
   an all-narration reply looks like when `max_tokens` cuts it off mid-trace.
   So it wrote the trace anyway. Now: no bullets ⇒ write **nothing** (logged),
   and `max_tokens` 512 → 2048 so the answer survives the narration.

All three are written up in `lessons.md` (2026-08-04). The generalised rules:
*a test double must be the real type*; *any new feature asking this champion
for a shape needs Python recovery from day one*; *when a recovery step guards
a durable auto-injected artefact, its fallback must be "write nothing"*.

**Live verification, final state** (scratch repo, two consecutive sessions):
clean bullets both times, newest-first append, hand-written text above the
block preserved byte-for-byte. `luxe init --dry-run` on the luxe repo now
produces a clean brief reaching "Invariants & gotchas" (it previously died
inside "Layout" — `GIT_BRIEF_HINT` now states the 2,000-char budget and
demands terse coverage of every section over depth in one).

---

## Workstream C — tool-call hardening (`f4d4336`)

### C1 · `scripts/toolcall_taxonomy.py`

Read-only over `~/.luxe/`, joins `runs/*/events.jsonl` +
`sessions/*/transcript.jsonl` + `debug.log` on
`run_id = f"{session_id}-{turn_idx}"`. Eight buckets, evidence bar **≥5
occurrences across ≥2 distinct sessions in the window**. Artefacts:

- `acceptance/toolcall_taxonomy_2026_08/REPORT.md` — machine-generated
- `acceptance/toolcall_taxonomy_2026_08/C2-VERDICTS.md` — hand-written
  per-candidate reasoning
- `tests/test_toolcall_taxonomy.py` — 22 tests pinning the counting rules

**45-day contract window** (39 runs, 87 tool calls, 39 assistant turns):

| class | occ | sessions | verdict |
|---|---:|---:|---|
| `schema_reject` | 0 | 0 | no occurrences |
| `unknown_tool_name` | 0 | 0 | no occurrences |
| `textfallback_drop` | 0 | 0 | no occurrences |
| `duplicate_storm` | 0 | 0 | no occurrences |
| `empty_response` | 1 | 1 | below bar |
| `aborted_run` | 0 | 0 | no occurrences |
| `turn_error` | 0 | 0 | no occurrences |
| `backend_retry` | — | — | **UNMEASURABLE** |

**400-day context scan** (1634 runs, 45148 tool calls; context only, never
counted against the bar — a quiet six weeks is not evidence of absence):
`schema_reject` 191/54 · `unknown_tool_name` 21/13 · `textfallback_drop`
**0/0** · `duplicate_storm` 78/52 · `empty_response` 51/16 · `aborted_run`
163/163 · `turn_error` 0/0.

### C2 · **No code shipped.** Per-candidate verdicts

| # | Candidate | Window | Wide | Verdict |
|---|---|---:|---|---|
| 1 | Silent text-fallback drops (`loop.py:95`) | 0 | **0 / 0** | **REFUTED — do not build.** Zero in both windows. This was the plan's highest-expected-value candidate. |
| 2 | `validate_args` enum + nested depth (`tools/base.py:117`) | 0 | 191 / 54 | **NOT ACTIONABLE.** `single_mode_done.schema_rejects` is a per-run TOTAL; the offending tool, key and reason are never persisted. Nothing says whether an enum or nested check would have caught even one. |
| 3 | Unknown-name validation asymmetry (`loop.py:1406`) | 0 | 21 / 13 | **BELOW BAR.** Top values are `final_report` ×9 / `tool` ×6 (May-era hallucinations) and one degenerate repetition loop; the whitespace-suffixed names (`read_file\n` ×110) are **already fixed** by `tc.name.strip()` at `loop.py:1403` — a closed class, not an open one. |

`agents/loop.py`, `tools/base.py`, `tools/fs.py`, `agents/single.py` and
`backend.py` are byte-identical to `origin/main`. **There is therefore no
benchmark-path line to justify**, and nothing needed an env flag.

### The actionable finding is measurement, not hardening

Three gaps, **not built** (each touches `events.jsonl`, which
`maintain_suite/run.py` parses, so each is bench-visible and a user decision):

1. Tool RESULTS are never persisted — `"Unknown tool: X"`, `"Schema error:
   …"` exist only in the in-flight message list. A `tool_reject` event
   carrying `{name, reason, offending_key}` would make candidates 2 and 3
   directly countable at the cost of one `append_event` call, no control flow.
2. `_parse_text_tool_calls` drops with no log line and no event, so a run
   where it fired is indistinguishable from one where the model wrote prose —
   which is *why* candidate 1 reads as 0. Making the drop observable is the
   prerequisite for ever deciding on it.
3. Backend retry reasons go to `on_retry`, which neither front-end routes to a
   logger. During an outage — the exact scenario this cycle exists for — the
   retry history is the most diagnostic thing there is and it is not written
   down.

### Two counting bugs found before the numbers were trusted

Written up in `lessons.md` (2026-08-04):

- A hand-written `KNOWN_TOOLS` list omitted the real `cve_lookup`, `git_show`
  and `deps_audit` while inventing `run_tests`/`git_status`/`apply_patch`.
  First run: **342 "unknown tool" dispatches across 68 sessions** —
  comfortably over the bar and enough to justify shipping loop.py changes.
  True figure: **21**. The script now reads the **live registry**
  (`_build_full_tool_surface` over every task type), prints its provenance in
  the corpus table, and a test asserts the static fallback can't go stale.
- Bench-apparatus runs polluted the corpus: the chunk-conclude A/B replay
  harness (`ccab-*`) dispatches synthetic names (`foo` ×66) by construction.
  Apparatus prefixes (`ccab-`, `smoke-`, `test-`, `capdrill-`) are now
  excluded by default; `--include-apparatus` restores them.

Also: one mined tool name was `list_dir` followed by ~400 newlines, which
rendered as a page of blank lines and silently broke the markdown table.
`_display()` now `repr`s and bounds every mined key.

---

## `.sdd` and docs updated (same commits as the behaviour)

| file | what |
|---|---|
| `src/luxe/luxe.sdd` | `ready`/`outage` contract: shared renderer, exit codes, offline purity, runnable fixes, **no scheduled counterpart** |
| `src/luxe/chat/chat.sdd` | notes contract (triggers/caps/window/silent-skip/bullet recovery/sanctioned memory.md write), `/init`, `/outage`, parity + `_HIDDEN_COMMANDS`, gate-hint audit, `render_doctor`/`hostwide_view` |
| `src/luxe/gitkit/gitkit.sdd` | `luxe init` as the third read-only surface + the single sanctioned write; `GIT_BRIEF_HINT` added to the prompt index |
| `src/luxe/memory/memory.sdd` | the two machine-managed fenced blocks, load-bearing markers, `splice_block` as the only writer |
| `src/luxe/tools/tools.sdd` | **untouched** — C2 shipped nothing |
| `README.md` | `luxe ready`/`outage` section, `luxe init`, project-memory + notes paragraph, `luxe ready` as step 0 of the escalation ladder |
| `CLAUDE.md` | `ready`/`outage` under the fallback kit; the two memory.md blocks under `src/luxe/memory/`; a new tool-call-taxonomy section |
| `configs/chat.yaml` | `notes: true` with a comment |
| `lessons.md` | two entries (the taxonomy denominator; the two silent live failures) |

---

## Deviations from the plan

1. **§1.2 render tweak (left to my judgement).** `hostwide_view` neutralises
   `mode`, `web` **and** `search index`, not just `mode` — otherwise every
   `ready` run reports a permanent WARN for an index it never builds.
2. **`luxe ready --repo <path>` added** (not in §1.6). The project checks need
   a subject, and depending on cwd would make the command untestable.
3. **§5.1 "stdlib only" relaxed for one guarded import.** The script tries
   `luxe.agents.single._build_full_tool_surface` inside `try/except` and falls
   back to a labelled static snapshot. A hand-list demonstrably produced a
   16×-wrong count; a wrong denominator that decides whether code ships is
   worse than an optional import. The script still runs under bare `python3`
   and says which source it used.
4. **`--context-days` added** (not in §5.1). The 45-day window turned out to
   hold 87 tool calls; reporting "0 occurrences" without showing that the
   window was quiet would have been misleading. The context scan is labelled
   as never counting against the bar.
5. **Apparatus-run exclusion** (not in §5.1) — methodology choice, documented
   in `C2-VERDICTS.md`, reversible with `--include-apparatus`.
6. **§4.B2 "reuse `fold.jsonl` for input"** — I call `summarize.fold_history`
   on the live session turns rather than reading `fold.jsonl` off disk. Same
   deterministic function, same output, no I/O and no ordering dependency on
   when the fold was persisted.
7. **`luxe init` has no `/init` TUI-worker plumbing.** It runs on the command
   worker like `/pull` transfers do. In the Textual TUI a long brief pass will
   block that worker; the CLI (`luxe init`) is the recommended entry point.

Nothing in §6 was violated: no monitoring/cron/launchd/alerting code or TODOs;
benchmark path byte-identical; all prompts in `agents/prompts.py`; no
`Path.rglob` on user roots (the taxonomy script uses `os.scandir` one level);
memory.md writes splice fenced blocks only with tests proving curated bytes
survive; every touched dir's `.sdd` updated in the same commit.

## Open WARNs

1. **`luxe init`'s brief still truncates at 2,000 chars on the luxe repo.**
   It now reaches "Invariants & gotchas" and loses only "Where things stand".
   Truncation is deterministic and marked in the file. Raising `MAX_BRIEF_CHARS`
   is a one-line change if the user prefers a longer brief, but 2,000 was the
   plan's number and it leaves room for curated text under `render_block`'s
   4,000-char budget.
2. **Notes cost one extra model call per session** (~5s on m5). Disable with
   `notes: false` in `configs/chat.yaml`.
3. **The notes distillation depends on the champion emitting column-0
   bullets.** Recovery is deterministic and the failure mode is safe (write
   nothing, log it), but a model that answers in unbulleted prose produces no
   notes. Observed rate on m5 after the fix: 0 failures in 2 consecutive live
   sessions; before the `max_tokens` bump it failed roughly half the time.
4. **`luxe smoke --chat --code` was not run** (only plain `luxe smoke`, exit
   0). Nothing in this cycle touches the agentic drill path, but the verifier
   may want it for completeness.
5. **My drills added ~6 sessions to `~/.luxe/sessions/`.** A verifier re-running
   `toolcall_taxonomy.py` will see slightly different 45-day counts than
   `REPORT.md` (a handful more assistant turns). The verdicts do not change.
6. **`/doctor`'s dead-endpoint fix line says `` `/backend <other>` ``**, a chat
   command, which reads slightly oddly from `luxe ready`. The first clause
   (`brew services restart omlx`) is the runnable one. Left shared rather than
   forking the string.

---

## Commands for the verifier to start with

```bash
cd ~/Downloads/luxe && git fetch && git checkout feat/operability
uv sync --extra chat --extra dev --extra analyzers

# 1 · suite + linear history + the benchmark-path rail
uv run pytest -q                                    # expect 2218 passed, 1 skipped
git log --merges origin/main..HEAD                  # expect EMPTY
git diff --stat origin/main -- src/luxe/agents/loop.py src/luxe/tools/base.py \
    src/luxe/tools/fs.py src/luxe/agents/single.py src/luxe/backend.py
                                                    # expect EMPTY (byte-identical)

# 2 · A1 — ready, healthy and broken
uv run luxe ready; echo "exit=$?"                   # expect READY, 0
uv run luxe doctor >/dev/null; echo "alias exit=$?" # expect 0
cat > /tmp/dead.yaml <<'EOF'
omlx_base_url: "http://127.0.0.1:59999"
models: {monolith: "Qwen3.6-35B-A3B-6bit"}
roles: {monolith: {model_key: monolith}}
EOF
uv run luxe ready --config /tmp/dead.yaml; echo "exit=$?"   # expect NOT READY, 1
uv run luxe ready --backend nope; echo "exit=$?"            # expect 2

# 3 · A2 — the card, with oMLX stopped if you like
uv run luxe outage --plain | head -30; echo "exit=$?"
uv run pytest tests/test_cli_ready.py -q            # incl. the card-vs-CLI anti-rot test

# 4 · A3 — parity + typo suggestion
uv run pytest tests/test_chat_commands.py -q -k "Parity or Suggestion or GateHints"

# 5 · B1 — brief on a scratch repo AND on luxe itself (judge the prose)
uv run luxe init . --dry-run                        # reads well? starts at "## What this is"?
mkdir -p /tmp/vrepo && cd /tmp/vrepo && git init -q . && \
  printf "[project]\nname='v'\n" > pyproject.toml && echo "x=1" > m.py && \
  git add -A && git -c user.email=t@t -c user.name=t commit -qm i
cd ~/Downloads/luxe && uv run luxe init /tmp/vrepo
printf '# MY CURATED NOTE\nkeep me\n\n%s' "$(cat /tmp/vrepo/.luxe/memory.md)" \
  > /tmp/vrepo/.luxe/memory.md
uv run luxe init /tmp/vrepo                          # re-init
head -3 /tmp/vrepo/.luxe/memory.md                   # expect the curated note, intact
uv run luxe init /tmp                                # expect refusal: "no project"

# 6 · B2 — notes end to end, then the injection
printf 'how many lines in m.py?\nis there a test file?\n/quit\n' | \
  uv run luxe chat --repo /tmp/vrepo
cat /tmp/vrepo/.luxe/memory.md                       # expect a luxe:notes block, BULLETS
                                                     # (a "thinking process" here = regression)
printf 'what is in m.py?\nanything else?\n/quit\n' | uv run luxe chat --repo /tmp/vrepo
cat /tmp/vrepo/.luxe/memory.md                       # expect 2 entries, newest FIRST,
                                                     # curated note still at the top
uv run python -c "from luxe.memory import project as p; \
  print(p.render_block(p.load_memory('/tmp/vrepo')))"   # the <project_memory> injection

# 7 · C — re-mine and spot-check two buckets by hand
uv run python scripts/toolcall_taxonomy.py --days 45 --context-days 400
uv run pytest tests/test_toolcall_taxonomy.py -q
#   then read acceptance/toolcall_taxonomy_2026_08/C2-VERDICTS.md
#   spot-check e.g.:
grep -c '"duplicate": true' ~/.luxe/runs/*/events.jsonl | grep -v ':0' | head
grep -h '"name": "final_report"' ~/.luxe/runs/*/events.jsonl | wc -l   # expect 9

# 8 · the drill
uv run luxe smoke; echo "exit=$?"                    # expect READY, 0
```

**Where to look if something disagrees with this report:** the two
`lessons.md` entries dated 2026-08-04 explain every non-obvious decision, and
`acceptance/toolcall_taxonomy_2026_08/C2-VERDICTS.md` carries the full
reasoning for shipping no hardening code.
