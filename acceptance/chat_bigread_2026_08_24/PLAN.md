# Plan: chat survives an oversized read (2026-08-24)

Driver: `EVIDENCE.md` in this directory — four turns lost across two backends
in ~30 minutes, all the same shape. Sequenced by risk, not by severity: every
phase ships and is verifiable on its own, and nothing in Phase 1 touches a
benched default.

**Ordering rule.** Phase 1 lands first even though Phase 2 is the actual fix.
Phase 1's diagnostics are what make Phase 2's and Phase 3's bench arms
readable — today a failed turn names the wrong engine and prescribes the wrong
remedy, so a bench arm's failure log cannot be trusted to say what failed.

---

## Phase 1 — Diagnostics tell the truth (contained; no benched default)

Seven changes, none touching the loop, the prompt registry, or any default the
maintain_suite has measured. Unit tests only; ship as one commit each or one
batch, reviewer's choice.

### 1.1 Name the engine that actually failed
- **Where**: `src/luxe/backend.py:850` and `:858` — `"oMLX stream failed: …"`,
  `"oMLX stream retries exhausted …"`.
- **Change**: add `engine_label: str = "oMLX"` to `Backend.__init__` and
  interpolate it into both messages. Default preserves today's string, so the
  benchmark path is byte-identical by construction. Wire the real value at the
  one chat construction site from `BackendEntry.engine_label()`
  (`config.py:164`), which already returns `oMLX` / `OpenRouter` / the raw
  engine name.
- **Test**: `tests/test_chat_backends.py` — a `Backend` built with
  `engine_label="OpenRouter"` raises `BackendError` whose text contains
  `OpenRouter` and not `oMLX`; a default-constructed one still says `oMLX`.
- **Risk**: none. No test currently pins either string (verified).

### 1.2 Stop prescribing `/backend local` for a failure that is not reachability
- **Where**: `src/luxe/chat/slots.py:416` `unreachable_hint()`; callers
  `repl.py:402`, `repl.py:884`, `tui.py:682`.
- **Change**: the hint currently fires on any backend error whenever ≥2
  backends are configured, and never probes. Gate it on `backend.health()`
  (already exists, `backend.py:862`, guarded and cheap). Healthy endpoint →
  return `None` and say something true instead: the endpoint answered, this
  request did not. Keep the existing hint verbatim for the genuinely
  unreachable case so the outage path is unchanged.
- **Test**: `tests/test_chat_backends.py` — stub `health()` True → no hint;
  False → today's hint. Assert the single-backend case still returns `None`.
- **Risk**: low. Adds one HTTP GET on the failure path only.

### 1.3 Make the aborted-turn footer coherent
- **Where**: `src/luxe/chat/repl.py:1070-1082`, `tui.py:743-751`.
- **Change**: when the turn aborted, `last_prompt_tokens` describes the last
  *accepted* step and `peak_context_pressure` the step that failed. Label them
  as such rather than printing `ctx: 2% of 128K` beside `context pressure
  103%`. Proposed: `last accepted 3.1k/128K · attempted ~72k est`.
- **Test**: `tests/test_chat_status.py` (or nearest) — render an aborted
  `AgentResult` and assert both numbers carry their qualifier.
- **Risk**: none; display only.

### 1.4 Gate the `/ctx` suggestion on it being able to help
- **Where**: `repl.py:1084-1090`, `tui.py:752-756`, `CTX_SUGGEST_PRESSURE=0.85`
  (`session.py:47`).
- **Change**: suppress the suggestion when the turn aborted for a reason that a
  larger window does not address, and when a single tool result is what
  crossed the threshold (that is a budget problem, not a window problem — point
  at `/ctx` only when growth was cumulative). Note in passing:
  `CTX_TIER_MIN_RAM_GB` gates `huge` on **host** RAM, which is meaningless for
  a cloud backend — harmless on a 128 GB box, wrong reasoning to leave in place.
- **Test**: aborted result with a single >25%-of-window tool result → no
  suggestion; cumulative growth at 0.9 → suggestion unchanged.
- **Risk**: low.

### 1.5 Annotate files that are big enough to hurt, not just big enough to refuse
- **Where**: `src/luxe/tools/fs.py:428` `_oversize_note`.
- **Change**: the threshold is `read_limit()` (the refusal cap). A 257,988 B
  file at 0.98× the cap listed as a bare name. Annotate at a second, lower
  ctx-relevant threshold with distinct wording — refused files keep today's
  text; merely large ones get a size and a nudge toward `limit=`/`grep`.
- **Test**: extend `tests/test_tool_budget.py` — a tree with nothing large
  stays byte-identical (this is the pinned property, keep it); a 200 KB file
  under a 256 KB cap gains a note.
- **Risk**: low, but it changes `list_dir`/`glob` output on the benchmark path
  for trees containing large-but-readable files. **Check `tests/test_golden_request.py`
  is unaffected before landing.** If it is affected, make the second threshold
  chat-only via the same extra-tool seam `make_prose_aware_write_fns` uses.

### 1.6 Session notes must survive a non-UTF-8 memory file
- **Where**: `src/luxe/chat/notes.py:188`; same pattern at
  `memory/project.py:126,234` and `:100`.
- **Change**: `read_text(encoding="utf-8", errors="replace")`, or catch
  `UnicodeDecodeError` and treat as empty-with-a-warning. Silent-skip is the
  documented contract for notes failure and stays; losing the notes to a
  decode error is not the failure that contract was written for.
- **Test**: `tests/test_chat_notes.py` — a gzip-magic `.luxe/memory.md` still writes
  the block and does not raise.
- **Risk**: none. Note `splice_block` remains the only writer.

### 1.7 Do not eat a slash command over a stray leading character
- **Where**: `src/luxe/chat/commands.py:128`.
- **Change**: strip leading whitespace and backticks before the `startswith("/")`
  test. A user pasting `` `/ctx xlarge `` from documentation is trying to run a
  command, not converse.
- **Test**: `tests/test_chat_commands.py` — `` `/ctx xlarge ``, `` `/ctx
  xlarge` ``, and ` /quit` all dispatch.
- **Risk**: low. Guard against over-stripping — a message that legitimately
  starts with a fenced code block (```` ```/… ````) must not be swallowed.

**Phase 1 gate**: `uv run pytest tests/ -q` green. No maintain_suite run needed.

---

## Phase 2 — Chat gets the read budget it already ships (the actual fix)

`tools/tools.sdd` § "`LUXE_TOOL_BUDGET_CTX` wiring" states the exact bar:
maintain is default-ON on `acceptance/toolbudget_ab_2026_08_12/REPORT.md`;
chat stays opt-in because *"chat UX around large files is a different question,
and there is NO chat-side evidence"*. `EVIDENCE.md` in this directory is that
evidence. This phase clears the bar the way the .sdd asks, not by asserting it.

### 2.1 Produce the chat-side arm
- Drill: `luxe smoke --chat --code` plus a purpose-built repeat of the failure
  in a planted scratch repo containing one ~250 KB prose file and one ~70 KB
  source file, run headless
  (`printf 'msg\n/quit\n' | luxe chat --repo <dir>`) at 32K and 128K, both arms
  (`LUXE_TOOL_BUDGET_CTX` unset vs `=1`).
- Capture per arm: turn outcome, `read_budget_applied` events, peak pressure,
  wall, and whether the model recovered via the `offset=` resume the clipped
  read hands it. **The recovery path is the thing to watch** — a budget that
  turns one fatal turn into three timid ones is not a win.
- Land as `REPORT.md` beside this plan.

### 2.2 Flip the chat default
- **Where**: `src/luxe/chat/repl.py:717`.
- **Change**: `os.environ.get("LUXE_TOOL_BUDGET_CTX") == "1"` →
  `os.environ.get("LUXE_TOOL_BUDGET_CTX", "1") != "0"`, matching the
  maintain/`LUXE_TRUNCATED_TURN_RETRY` opt-out grammar exactly.
- **Do this only if 2.1 supports it.** The .sdd explicitly forbids "aligning"
  the two grammars without chat-side evidence; producing the evidence is what
  makes the alignment legal, and a bad result means the flag stays opt-in and
  we solve it in Phase 3 instead.
- **Docs**: update `tools/tools.sdd` § wiring (the asymmetry paragraph and the
  "do not align" sentence both become wrong on landing), `chat/chat.sdd`, the
  `repl.py:705-719` comment block, and `CLAUDE.md` § "Tool limits are
  announced" — which currently says the budget is "OFF by default" without
  distinguishing the two paths, and is already stale re: maintain.
- **Test**: `tests/test_tool_budget.py:282` asserts the literal chat source
  string `'os.environ.get("LUXE_TOOL_BUDGET_CTX") == "1"'`. It will fail by
  design — update it to the new grammar and add the unset→ON, `"0"`→OFF,
  `"true"`→ON cases the maintain side already has.

**Phase 2 gate**: the drill in 2.1 shows the failing turn now completing, with
the model actually using the resume offset; full pytest green.

---

## Phase 3 — Stop dispatching and re-dispatching a request that cannot succeed

Loop- and backend-level. Touches benched behavior → maintain_suite required.
Note `chat/chat.sdd` Must-not forbids the *chat* subtree from modifying
`agents/loop.py`'s `backend.chat` call site; these changes live in `agents/`
and `backend.py` and are governed by `agents/agents.sdd`.

### 3.1 Classify payload-shaped failures instead of retrying them blind
- **Where**: `src/luxe/backend.py:219-273` `classify_failure`.
- **Problem**: `RemoteProtocolError` → `transient-*` → three identical
  dispatches of the same bytes, 81.5s and 76s of pure waste, billed.
- **Change**: pass the request's estimated size into `classify_failure`. When a
  transport-level failure follows a prompt that grew sharply over the previous
  accepted request (proposed: >2×, and above some absolute floor), classify it
  `payload-suspect`: at most one retry, and only after the caller has had a
  chance to shrink. Keep every existing classification for every other case —
  this must not touch the outage retry behaviour the fleet depends on.
- **Test**: `tests/test_backend_retry.py` — growth-ratio table; assert existing
  transient/4xx/5xx decisions are unchanged.
- **Risk**: medium. This is the fleet's outage path. The conservative version
  (log the classification, keep retrying) is worth shipping first if the bench
  arm is at all ambiguous.

### 3.2 Bound a tool result at insertion, not at compaction
- **Where**: `src/luxe/agents/loop.py`, tool-result append site.
- **Rationale**: this is the structural answer to the phase-3-dropped-nothing
  finding, and it is deliberately **not** a change to `TieredCompact`.
  `agents.sdd` pins `messages[0]`/`messages[1]` and the last `keep_recent`
  assistant iterations as never-eligible; that invariant is load-bearing and
  should not be relaxed to solve a problem that belongs one layer down. A
  result that alone exceeds a fraction of the window should be clipped when it
  is created, with the same `offset=` resume text `read_file` already emits.
- **Interaction**: Phase 2 makes this mostly redundant for `read_file`. It
  still matters for `bash`, `grep`, and MCP tools, which have no budget at all.
- **Gate**: `python -m benchmarks.maintain_suite.run --variants <yaml>`,
  3 reps × 10, plus hand-reading every PASS via the local-branch ref
  (bench-as-truth; a printed pass is not a result).

### 3.3 Make the no-op visible
- **Where**: `src/luxe/context.py:295-300`.
- **Change**: `compaction_phase_reached` with `tokens_before == tokens_after`
  and `tool_results_dropped == 0` should record that it achieved nothing —
  `effective=False`, or a distinct event. Today the telemetry reads as a
  response to pressure when nothing happened, which is how this went unnoticed.
- **Test**: `tests/test_context.py` (or nearest compaction test) — 2-assistant-message trajectory
  over threshold asserts `phase_reached=3, effective=False`.
- **Risk**: none if additive. Check `scripts/toolcall_taxonomy.py` and any
  events consumer for a field-shape assumption first.

---

## Phase 4 — Context pressure stops over-reporting under composition shift

Benched default (`LUXE_CTX_SERVER_TRUTH`, shipped `967124d`) → maintain_suite
required. Lowest priority: it made the failure *legible* wrong, it did not
cause it. Do not start this before Phase 2 lands.

- **Problem**: the ratio is measured on the previous response. At step 1 that
  prompt is system-prompt-and-tool-JSON, and the ratio (observed 1.79–2.64x) is
  then applied to a prompt that is 96% prose, where the same sessions measured
  1.21–1.27x. Result: 102.5% reported on a request that was likely ~65% of the
  window.
- **Change**: damp the extrapolation when composition shifts — when `est_sent`
  grows by a large multiple over the calibration sample, decay the ratio toward
  1.0 in proportion, or carry two ratios (overhead vs body). `agents.sdd`
  already documents the drift and pins that the ratio is re-measured, never
  latched; this narrows *how far* a sample may be extrapolated and changes no
  threshold.
- **Constraint**: the pinned `(0.50, 0.85, 0.95)` thresholds keep their values.
  The clamp `[0.5, 8.0]` stays.
- **Gate**: 3 reps × 10 maintain_suite against the shipped default, plus a
  replay of `168f1825a1fd`'s message sizes asserting the reported pressure
  lands near the prose-true figure.

---

## Phase 5 — Repair, governance, and the record

### 5.1 Fix the help text `cbfef4f` shipped to main
`src/luxe/chat/launch.py` now says *"External backends (e.g. openrouter)
default to moonshotai/kimi-k3 when no model is specified"* — that is one
`default_model:` key on one `configs/chat.yaml` entry, generalised into a rule
about all external backends. And *"Enables browser-enhanced mode for web
interactions"* is invented terminology; the real capability is `web_page` +
Chromium. Rewrite both accurately. Rebase, never merge.

### 5.2 Decide on agent commit attribution
`cbfef4f` is authored `Michael Timpe <michaeldtimpe@gmail.com>` with no trailer
marking it agent-written, and the turn that produced it claimed *"Pushed —
bypassed the protected ref rule on main"*, which nothing in the log supports.
luxe's chat bash path commits under the user's identity with no marker. Either
add a `Co-Authored-By:`/`X-Luxe-Session:` trailer at the chat-bash commit seam,
or record the decision not to. **User's call — do not implement either
unilaterally.**

### 5.3 Write the record
- `lessons.md` 2026-08-24: the compaction no-op (phase 3 that dropped zero),
  the read budget that was on for bench and off for chat, and the diagnosis
  that named the wrong engine and prescribed the wrong endpoint.
- Memory: a `project_` entry pointing at this directory; update
  `project_openrouter_backend.md` with the failure mode.
- `RESUME.md`: current state + which phases remain.

---

## Sequencing

| Phase | Ship gate | Blocks |
|---|---|---|
| 1 | pytest | nothing |
| 2 | drill `REPORT.md` + pytest | needs 1 (readable failures) |
| 3 | maintain_suite 3×10, hand-read | needs 2 (else measures the wrong thing) |
| 4 | maintain_suite 3×10 + replay | needs 2 |
| 5.1/5.3 | pytest | nothing |
| 5.2 | user decision | — |

Phase 1 + 5.1 + 5.3 are a single sitting. Phase 2 is the one that would have
saved the session. Phases 3 and 4 are the ones that make the *next* unfamiliar
failure legible, and both want a bench run you should kick off yourself rather
than have an agent run unattended.
