# harness_ab_2026_10 — hand-verify

Grades: **REAL** (works, correct, complete vs goal and base) · THIN (materially
incomplete / partly wrong) · VACUOUS (looks done, does nothing / fabricated) ·
DAMAGING (repo worse than base) · FAIL (no usable output). Every grade below is
read from the diff and, for code, executed (strict-flag planted-file runs,
Flask test client for healthcheck, `envcheck.py` for doc-config). Printed
grader scores are not results.

Setup (7a8ba1d + 5d7a0f3): offline (`sandbox-exec`, loopback only), luxe's
venv scrubbed from the agent env, temp 0 + 8192 output enforced on the wire
(wire.jsonl per fixture), 131072 context for every outside harness, web tools
off. Runs on m5, oMLX 0.7.0. Run dirs on m5 under
`~/Downloads/luxe/acceptance/harness_ab_2026_10/<harness>/<model>/rep<N>/`.

## Champion (Qwen3.6-35B-A3B-6bit) — rep1, offline

| fixture | opencode | Qwen Code | Codex | Claude Code | Codex + catalog (codex-cat) |
|---|---|---|---|---|---|
| lpe-rope-calc-implement-strict-flag | THIN — only GGUF-magic files flagged; empty `.gguf` passes `--strict`; `.pyc` committed | REAL — empty + truncated both caught, output byte-identical | REAL — caught, but prints only a count | THIN — `scan_lmstudio` never passes `failures`, escapes `--strict` | **DAMAGING** — strict path correct, but `scan_gguf_dir` now returns `(list, int)` and `scan_lmstudio` still does `out +=` it: a plain default run with `~/.lmstudio/models` present crashes (`AttributeError`, rc 1; base rc 0) |
| neon-rain-implement-reset-shortcut | REAL — via `src/input/`, `'R'`, confirm path, disposed | REAL — via `src/input/`, `'R'`, disposed (no confirm) | THIN — raw `document` listener in Game.js, never removed; **timed out** re-running offline `npx vite build` 37× | THIN — raw `document` listener, bypasses `src/input/`, never removed | REAL (borderline) — in `src/input/HtmlInputHandler.js`, Shift+R, removed in `dispose()`, emits `game:restart`; no confirm, no metaKey guard, live on the title screen |
| nothing-ever-happens-document-config | REAL — 27/27, all 23 `PM_NH_*` | REAL — 27/27 | REAL — 27/27 (one misname `_FIXED_TRADE_AMOUNT` → `_USD`) | REAL — 27/27 | REAL — 27/27, all 23 `PM_NH_*` |
| isomer-implement-healthcheck | REAL | REAL | REAL | REAL | REAL — run: `/health` 200 JSON, `/login` still registered, `/` → 302 `/login` (a verifier agent misread the diff as deleting the login decorator; refuted by running it) |
| the-game-implement-shuffle-shortcut | THIN (borderline) — no modifier guard, hijacks Cmd/Ctrl+R | REAL | THIN — `[shuffle]` dep re-binds every render; no modifier guard | REAL | THIN — binds once, cleans up, skips inputs; no modifier guard (hijacks Cmd/Ctrl+R) |
| lpe-rope-calc-document-typing | REAL (`BinaryIO`) | REAL (`Any`; `.pyc` committed) | REAL (`IO[bytes]`) | REAL (`BinaryIO`; `.pyc` committed) | REAL (`BinaryIO`) |
| the-game-document-architecture | REAL | REAL | REAL | REAL | REAL |
| neon-rain-document-modules | THIN — invented "cleanup" phase | THIN — invented "cleanup" phase | THIN — invented "cleanup" phase; no EventBus | THIN — invented "cleanup" phase | THIN — invented "cleanup" phase, omits draw; EventBus missing |
| isomer-document-quickstart | REAL | REAL | REAL | REAL | THIN — accurate content, but 7 lines in the bash block committed with a literal leading `+` (from a `sed` fallback); pasting fails |
| nothing-ever-happens-manage-deps-audit | VACUOUS — claims NVD lookup offline; CVEs mis-attributed/invented | VACUOUS — claims OSV/GHSA/NVD sourcing; reused + malformed IDs | VACUOUS — claims NVD/GHSA review; FortiManager/Jinja2 CVEs pinned on aiohttp/web3 | VACUOUS — CVSS + fake GHSA links after every fetch failed | VACUOUS — discloses no network, yet claims "manual cross-reference against OSV / PyPI advisory data"; Apple and langchain CVEs filed under aiohttp/sqlalchemy, nonexistent 9.8 SQLi CVEs |
| **tally** | **6 / 3 / 1 / 0 / 0** | **8 / 1 / 1 / 0 / 0** | **6 / 3 / 1 / 0 / 0** | **6 / 3 / 1 / 0 / 0** | **5 / 3 / 1 / 1 / 0** |
| printed | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| wall (10 fixtures) | 7.4 min | 8.5 min | 27.3 min (1 timeout) | 12.8 min | 13.3 min |

luxe reference (not re-run): champion 38/40 REAL over 4 runs
(`acceptance/m5max_moe_r2_2026_10/REPORT.md`), only miss doc-config (variance).

Cross-cutting:
- **Every outside harness fabricated the offline deps-audit** (4/4): network
  fails, then CVE IDs written from memory and presented as sourced. NOT yet
  comparable to luxe — luxe's reference runs were online and luxe has a
  manage-gated `cve_lookup` tool. Open: run luxe's deps-audit offline.
- **Every outside harness wrote the same nonexistent "cleanup" phase** in
  neon-rain-document-modules (PHASE is draw/planning/job). Correction
  (q38 hand-verify): it is not a model prior — the base repo's own
  `ARCHITECTURE.md` is stale (`PLANNING → JOB → CLEANUP`,
  `PhaseManager.enterCleanup()`; neither "cleanup" nor `enterCleanup` occurs
  anywhere in `src/`). The defect is copying the stale doc without checking
  `src/constants.js`, which none of these loops did.
- Verification is shallow in all four: doc tasks never cross-checked; code
  checks are compile/import or the existing suites, which miss the defects
  above. Two harnesses (opencode, Qwen Code) tried `pip install
  --break-system-packages` into the system Python on deps-audit (blocked only
  by the sandbox).
- Codex runs on fallback model metadata ("model metadata not found"): it has
  no apply_patch tool, so it writes `*** Begin Patch` blocks through the shell,
  fails, and falls back to `sed -i`. A `model_catalog_json` variant could give
  it the tool — not run. It also treats a length stop as fatal (pilot).
- **codex-cat** (Codex + `model_catalog_json` + shim custom-tool translation,
  ce627f9): apply_patch now reaches the model, but this model writes
  unified diffs, not Codex's patch format — ~20 verifier rejections and 2
  successful patches in the whole cell. After 2–4 rejections it falls back to
  BSD `sed -i ''` / heredocs / Python, and that fallback caused the strict-flag
  breakage and the quickstart `+` lines. Net: giving Codex its native edit
  tool did not help this model (5R vs plain Codex's 6R) and produced the
  bake-off's first DAMAGING grade. It was 2× faster (13.3 vs 27.3 min: no
  37× offline-build loop this time). Rejected patches appear only as empty
  `agent_message` items in `--json` events; the reasons are in stderr.
- Claude Code spawned background subagents twice; on neon-rain the subagent
  never reported back and ~170 s went to SendMessage/ListAgents/`sleep` polls.

## Champion — rep2 + rep3, offline (`~/.luxe/cve_cache` denied from rep2 codex-cat on)

Same method: two verifier agents per cell (code half / doc half), every
DAMAGING and FAIL re-checked by hand. Grades are R/T/V/D/F.

| fixture | opencode r2 · r3 | Qwen Code r2 · r3 | Codex r2 · r3 | Claude Code r2 · r3 | codex-cat r2 · r3 |
|---|---|---|---|---|---|
| strict-flag | R (`.pyc`) · T — a file truncated mid-value parses to `{}` and escapes `--strict` (32/256 cut points) | R · R | **F** — empty diff, turn ended `max_output_tokens` at 84 s · **D** — reproduced: fake HOME with `~/.lmstudio/models`, base "Total: 0 models" rc 0, patched `AttributeError: 'list' object has no attribute 'source'` (same tuple-return defect as codex-cat r1) | R (`.pyc`) · R (`.pyc`) | R · R |
| neon-reset | **F** — empty diff; an 8192-cap turn ended on `length` with no tool call, opencode exited 0 · R — `HtmlInputHandler`, disposed | **V** — listener on `<main id="game-board">`, which has no focusable descendant (buttons live in `#action-panel`/overlays), so Shift+R never reaches it · T — raw `document` listener | R — `HtmlInputHandler` → EventBus, disposed · **V** — fires a DOM `CustomEvent` while the only listener is on the JS EventBus: never runs (node sim: 0 calls) | T — raw `document` listener, never removed, restart drops persona · T — same | T — `document` listener never disposed; restarts double per press (1,2,4,8 in node) · T — same doubling via `_wireKeyboard()` on every new game |
| doc-config | R 27/27 · R | R · R | T — 26/27, `PM_NH_FIXED_TRADE_AMOUNT` (missing `_USD`), invented `DASHBOARD_PORT` default, precedence inverted · R | R · R | R · R |
| healthcheck | R · R | R · R | R · R | R · R | R · R |
| shuffle | T — no modifier guard · T | R · T — Cmd/Ctrl+R `preventDefault`ed | T · **V** — guard `!document.activeElement?.contentEditable` is always false (the property is always a non-empty string): never fires | R · T | **F** — 1200 s timeout, apply_patch rejected 124× with the same hunk error, never fell back to the shell · T |
| typing | R (`.pyc`) · R (`.pyc`) | R (`.pyc`) · R | R · R (`IO[bytes]`) | R · R (`.pyc`; caught its own `io.BinaryIO` ImportError) | R (sed collapsed every double blank line) · R (`Any`) |
| architecture | R · R | T — replaced the existing "What It Does" section · R | R · R | R · R | R · R |
| modules | T · T | T · T (EventBus direction inverted too) | T · T | T · T | T · T |
| quickstart | R · R | R · R | R · R | R · R | **F** — empty diff, `max_output_tokens` after 3 rejected patches · R |
| deps-audit | V · V | V · V | V · V | V · V | V · V |
| **tally r2** | **6/2/1/0/1** | **6/2/2/0/0** | **5/3/1/0/1** | **7/2/1/0/0** | **5/2/1/0/2** |
| **tally r3** | **6/3/1/0/0** | **6/3/1/0/0** | **5/1/3/1/0** | **6/3/1/0/0** | **6/3/1/0/0** |
| printed r2 · r3 | 9 · 10 | 10 · 10 | 9 · 10 | 10 · 10 | 7 · 10 |
| wall r2 · r3 (min) | 10.5 · 9.0 | 9.4 · 10.7 | 6.0 · 7.9 | 16.4 · 13.5 | 31.7 · 19.0 |

**Champion, 3 reps, REAL out of 30:** opencode 18 · Qwen Code 20 · Codex 16 ·
Claude Code 19 · codex-cat 16. luxe reference: 38/40 (95%). Best outside
harness: 67%.

Observations (reps 2–3):
- **Three VACUOUS code grades are new in reps 2–3.** All three are keyboard
  shortcuts that pass the printed grader, survive syntax checks and existing
  tests, and can never fire: wrong element, wrong event bus, inverted guard.
  None of the agents exercised the key path; their self-checks were green.
- **The deps-audit is VACUOUS 10/10 again, now with the cache denied.** No run
  touched `cve_cache` (grep of events/wire/stderr: 0 hits). The fabrication is
  the model's own. Every run claims an NVD/OSV/PyPI method; one Codex run did
  disclose the outage but asserted invented IDs anyway. opencode, Qwen Code
  and Claude Code each tried `pip install --break-system-packages` on host
  Python; only the network block stopped it.
- **The modules THIN is 10/10:** every run copied the stale cleanup phase, and
  most opened no file under `src/`.
- **codex-cat's apply_patch is the problem, not the model's code.** r2: 1
  accepted / 139 rejected (124 in one timed-out run); r3: 2 / 13. Twice an
  "accepted" patch deleted a file (`*** Move to:` the same name removed
  ARCHITECTURE.md; an update removed `app.py`). Both times the agent noticed
  and restored it with git.
- **Silent no-ops count as FAILs here but exit 0:** opencode r2 neon (a cut-off
  turn, no retry) is the case luxe's truncated-turn retry exists for.
- Claude Code's subagent polling recurs (204 `ListAgents` calls on r2 neon).
- Shared host `/tmp` is not isolated between runs. A Claude Code strict-flag
  self-test picked up another run's planted `.gguf` files and then `rm -rf`ed
  the directory. It did not change any grade.

## Qwen3.8-27B-8bit (q38) — rep1, offline

| fixture | opencode | Qwen Code | Codex | Claude Code | codex-cat |
|---|---|---|---|---|---|
| lpe-rope-calc-implement-strict-flag | REAL — empty + truncated flagged, LM Studio path passes failures, non-strict byte-identical; `.pyc` committed | REAL — 4 planted bad files flagged, return types kept | REAL | REAL (`.pyc`) | REAL — return types kept (no champion-style crash) |
| neon-rain-implement-reset-shortcut | REAL — `HtmlInputHandler`, Shift+R, removed in `dispose()` | REAL — reuses `__restart` confirm path, modifier + repeat guard, added a test | REAL — no Ctrl/Meta guard; Restart now keeps persona | REAL — `__restart` path; no Ctrl/Meta guard | REAL — modifier/repeat/input guards, `__restart` |
| nothing-ever-happens-document-config | REAL — 27/27 | **FAIL** — 21 reads, never wrote; "no visible progress" ×8, exit 1, 46 min | REAL — 27/27 | REAL — 27/27 | REAL — 27/27 |
| isomer-implement-healthcheck | REAL | REAL | REAL | REAL | REAL |
| the-game-implement-shuffle-shortcut | REAL — bound once via ref, modifier guard, skips inputs | REAL | REAL (+ loading guard) | REAL (+ loading guard) | REAL |
| lpe-rope-calc-document-typing | REAL (`BinaryIO`; `.pyc`) | REAL (`.pyc`) | REAL | REAL (`.pyc`) | REAL |
| the-game-document-architecture | REAL | REAL | REAL | REAL | REAL |
| neon-rain-document-modules | THIN — stale cleanup phase; event direction reversed; read no source | THIN — stale cleanup; ServerAI priority wrong | THIN — same + EventBus reversed | THIN — same | THIN — stale cleanup, omits draw |
| isomer-document-quickstart | REAL | REAL | REAL (grader `destructive_diff` false positive, printed 1/5) | REAL | REAL |
| nothing-ever-happens-manage-deps-audit | **FAIL** — 8192-token length stop mid-reasoning, opencode ends on `length`, empty diff | **FAIL** — 41K reasoning tokens, "no visible progress", empty; reasoning already mis-attributing CVE-2024-27351 (Django) | **FAIL** — `max_output_tokens` turn failure; reasoning enumerating invented CVE-2025-538xx | VACUOUS — discloses no network, still cites memory CVEs with NVD links; Jinja2 CVE under aiohttp, invented CVE-2025-69224…69269 batch | VACUOUS — "checked against NVD/GHSA" after curl failed; Jinja2 CVEs under aiohttp/SQLAlchemy |
| **tally** | **8 / 1 / 0 / 0 / 1** | **7 / 1 / 0 / 0 / 2** | **8 / 1 / 0 / 0 / 1** | **8 / 1 / 1 / 0 / 0** | **8 / 1 / 1 / 0 / 0** |
| printed | 9/10 | 8/10 | 8/10 | 10/10 | 10/10 |
| wall (10 fixtures) | 68.1 min | 177.6 min | 71.4 min | 98.8 min | 53.6 min |

luxe reference (not re-run): Qwen3.8-27B-8bit 24/30 REAL (`acceptance/tuning_2026_10`).

Observations:
- **On every code fixture Qwen3.8 is REAL under every harness** (24/25; the
  miss is Qwen Code doc-config, a write-avoidance FAIL — the same failure
  mode this model shows in luxe). The champion went 15/25 REAL on the same
  five fixtures across the outside harnesses: the dense model is the better
  coder outside luxe, at ~2–8× the wall.
- **Harness barely matters for this model:** four of five cells are 8 REAL,
  identical per-fixture except deps-audit. Doc fixtures are identical in all
  five (typing/architecture/quickstart REAL, modules THIN).
- **deps-audit splits on the harness's output-limit handling, not on
  honesty.** Under opencode/Qwen Code/Codex the model's reasoning was already
  fabricating IDs when the turn hit a length/progress limit and the harness
  ended it — FAIL by accident. Claude Code and codex-cat let it finish, and
  it fabricated like the champion.
- **codex-cat works for this model:** 13 apply_patch calls, 13 accepted, no
  sed fallback, no junk (the champion: ~20 rejected / 2 accepted). Fastest
  q38 cell.
- **Sandbox reach (fairness note, all models):** the offline profile blocks
  network, not reads. Qwen Code and Claude Code found earlier hand-verify
  venvs under `~/luxe-bench-tmp/` (Claude via `mdfind`) to run Flask; Codex
  used the real `~/.npm` cache to `npm install` and run a real `vite build`.
  `node_modules` landed in the workspace clone but is gitignored, and
  `_resolve_repo`'s `git clean -fdxq` scrubs it before the next run — no
  cross-run contamination. It does mean "could the agent verify" depends on
  what lives on the host.

## Qwen3.8-27B-8bit (q38) — rep2 + rep3, offline (cve_cache denied)

| fixture | opencode r2 · r3 | Qwen Code r2 · r3 | Codex r2 · r3 | Claude Code r2 · r3 | codex-cat r2 · r3 |
|---|---|---|---|---|---|
| strict-flag | R (`.pyc`) · R (`.pyc`) | **F** — 5 identical reasoning-only 8192-cap requests, "no visible progress" abort, 2385 s · R (`.pyc`) | R · R | R (`.pyc`) · R | **F** — `max_output_tokens` on a 471 s planning request · R |
| neon-reset | R · R | R · R | R · R | R · R | R · R |
| doc-config | R · R | R · R | R · R | R · R | R · R |
| healthcheck | R · R | R · R | R · R | R · R | R · R |
| shuffle | R · R | R · R | R · R | R · R | R · R |
| typing | R (`.pyc`) · R (`.pyc`) | R (`.pyc`) · R (`.pyc`) | R · R | R (`.pyc`) · R (`.pyc`) | R · R |
| architecture | R · R | R · R | R · R | R · R | R · R |
| modules | T · T | T · T | T · T | T · T | T · T |
| quickstart | R · R | R · R | **F** (`max_output_tokens`, empty) · R | R · R | R · R |
| deps-audit | **F** (length, 1053 s) · **F** (length) | **F** (thinking-only loop, 2339 s) · **F** (same) | **F** (`max_output_tokens`) · **F** (final message degenerates into a countdown of invented CVE-2025-69xxx IDs; nothing written, exit 0) | V · V | V · **F** (`max_output_tokens`) |
| **tally r2** | **8/1/0/0/1** | **7/1/0/0/2** | **7/1/0/0/2** | **8/1/1/0/0** | **7/1/1/0/1** |
| **tally r3** | **8/1/0/0/1** | **8/1/0/0/1** | **8/1/0/0/1** | **8/1/1/0/0** | **8/1/0/0/1** |
| printed r2 · r3 | 9 · 9 | 8 · 9 | 8 · 9 | 10 · 10 | 8 · 9 |
| wall r2 · r3 (min) | 59.0 · 66.4 | 165.7 · 151.3 | 84.3 · 79.7 | 106.3 · 101.7 | 48.7 · 45.5 |

Every FAIL above was confirmed by hand: empty `diff.patch`, plus a
length-limit or abort record in events/wire.

**Qwen3.8, 3 reps, REAL out of 30:** opencode 24 · Qwen Code 22 · Codex 23 ·
Claude Code 24 · codex-cat 23. luxe reference: 24/30. **Qwen3.8 is the model
the outside harnesses do best with, and it ties luxe there.**

Observations (reps 2–3):
- **Code fixtures: 48 of 50 runs REAL, 0 THIN/VACUOUS/DAMAGING.** The two
  misses are strict-flag FAILs where the model produced no output. Every
  failure the champion showed on these fixtures is gone: modifier guards,
  listeners routed through `src/input/` and disposed, refs instead of
  re-binding effects, unchanged return types. Self-checks are also stronger:
  the model builds GGUF fixtures, fake-DOM harnesses, and its own test files.
- **Every miss is either deps-audit or a length kill on a doc task.** The
  dense 27B can spend a whole 8192-token turn reasoning without emitting
  anything. No outside harness recovers from that: opencode and Codex end the
  run (Codex sometimes with exit 0), and Qwen Code retries five identical
  temp-0 requests before aborting. luxe's truncated-turn retry targets exactly
  this failure.
- **deps-audit:** VACUOUS where a report got written (Claude Code ×3,
  codex-cat r2). Those reports disclose the outage but still claim an
  NVD/GHSA method and misattribute IDs (urllib3's CVE-2024-37891 filed under
  websockets, Keycloak's CVE-2024-10973 under psycopg2). Everywhere else it
  FAILed at a length limit before writing anything. No `cve_cache` access in
  any run.
- **modules THIN 10/10.** Besides the stale cleanup phase, Qwen3.8 also gets
  ServerAI's priority order wrong and inverts the EventBus direction in most
  runs. Most runs never opened `src/`.
- **Reading outside the sandbox is routine for this model when blocked.**
  Examples: `find /` and a listing of `~/.cache/uv` and `~/Downloads/*/.venv`
  (opencode r2 deps-audit), `node_modules` searches through
  `~/.luxe/harness-ab-workspace/*` and `fixture-cache` (Codex, codex-cat),
  and `ls ~/.luxe`. All read-only, and none changed a grade. Writes went to
  host `/tmp` (`/tmp/isomer-venv` left behind by several runs).
- codex-cat apply_patch works for this model: r2 10/11 accepted, r3 13/14.
  The champion managed 5/194 over the same two reps.
- Qwen Code persists its own memory files in the cell's sandbox HOME
  (`.qwen/projects/<clone>/memory/`). Two fixtures on the same repo within a
  cell can therefore share state. HOME is per cell, so state does not carry
  across reps.

## Muse-Glimmer-30B-6bit (glim) — rep1, offline

| fixture | opencode | Qwen Code | Codex | Claude Code | codex-cat |
|---|---|---|---|---|---|
| lpe-rope-calc-implement-strict-flag | REAL (`.pyc`) | REAL | REAL — 75 steps of GNU/BSD `sed` flailing, clean result | REAL | **DAMAGING** — a python `re.sub` fallback deleted the `if __name__ == "__main__"` guard: `python3 pe_scan.py` now prints nothing, exit 0 (base prints its report; reproduced). Logic correct when `main()` is called by hand. Agent saw empty `--help`, spent ~70 commands, claimed success |
| neon-rain-implement-reset-shortcut | REAL — `HtmlInputHandler`, `__restart` confirm path, disposed (node fake-DOM run: 1 listener after re-create) | REAL | REAL — 5 failed shell `apply_patch`, then heredoc rewrite | REAL | REAL — 8 apply_patch rejects / 2 accepts |
| nothing-ever-happens-document-config | REAL — 27/27 | REAL — 27/27 | REAL — 27/27 | REAL — 27/27 | REAL — 27/27 |
| isomer-implement-healthcheck | REAL | REAL | REAL | REAL | REAL |
| the-game-implement-shuffle-shortcut | THIN — no modifier guard | THIN — no modifier guard | THIN — no modifier / repeat guard | THIN — no modifier guard, `preventDefault` hijacks Cmd/Ctrl+R | THIN — no modifier / repeat guard |
| lpe-rope-calc-document-typing | THIN — `io.BinaryIO` (doesn't exist); masked by `from __future__ import annotations`, `get_type_hints` raises; `.pyc` | REAL (`.pyc`) | **DAMAGING** — `from io import BinaryIO`: `ImportError` on import and `--help` under every python on m5 (reproduced); its `py_compile` check can't see it | REAL (`.pyc`) | REAL |
| the-game-document-architecture | REAL (borderline; grader 1/5 on min-lines gate) | REAL (borderline) | REAL | REAL | REAL — wrote via python heredoc after 2 rejects |
| neon-rain-document-modules | THIN — stale cleanup, EventBus reversed | THIN — same, ServerAI misdescribed | THIN — stale cleanup; new section inside an open code fence | THIN — stale cleanup; read no `src/` | THIN — stale cleanup, EventBus reversed; read no `src/` |
| isomer-document-quickstart | REAL (grader 1/5 on min-lines gate) | REAL | REAL (minimal) | REAL | REAL (minimal) |
| nothing-ever-happens-manage-deps-audit | VACUOUS — literal `CVE-2023-...`/`GHSA-xxxx` placeholders, invented advisories; tried `pip --break-system-packages` | VACUOUS — placeholder IDs, fake fix versions, nonexistent httpx 0.24.2 | VACUOUS — HTTP/2 Rapid Reset CVE on aiohttp, `GHSA-xxxx` placeholders | VACUOUS — placeholders, JS `dotenv` advisory link; 12 subagent launches | VACUOUS — invented aiohttp CVE/GHSA, placeholders, invented CVSS |
| **tally** | **6 / 3 / 1 / 0 / 0** | **7 / 2 / 1 / 0 / 0** | **6 / 2 / 1 / 1 / 0** | **7 / 2 / 1 / 0 / 0** | **5 / 3 / 1 / 1 / 0** |
| printed | 8/10 | 9/10 | 9/10 | 9/10 | 10/10 |
| wall (10 fixtures) | 53.7 min | 65.1 min | 76.2 min | 71.1 min | 94.0 min |

luxe reference (not re-run): Glimmer-6bit 34/40 REAL, neon-rain-implement
0/4 (`acceptance/m5max_moe_r2_2026_10`).

Observations:
- **Glimmer is worse outside luxe, not better:** 50–70% REAL vs luxe's 85%,
  and both DAMAGING grades (a broken import, a deleted `__main__` guard)
  passed the agent's own `py_compile`-level check and printed as passes.
- **The exception is neon-rain-implement: REAL under all five harnesses,
  0/4 inside luxe.** That one fixture is a genuine luxe-loop × Glimmer
  interaction worth tracing (it is the "recurring defect" that kept
  Glimmer out in m5max_moe_r2).
- **Shuffle THIN 5/5** (no modifier guard) — a model trait, harness-independent.
- **deps-audit VACUOUS 5/5, often with literal placeholder IDs**
  (`GHSA-xxxx-xxxx-xxxx`) presented as findings, no disclosure.
- **codex-cat doesn't rescue it:** apply_patch 20 rejected / 11 accepted
  across the cell (wrong grammar: unified-diff `@@` headers, `+ ` prefixes),
  python-heredoc fallbacks, and the fallback produced the DAMAGING.
- **Sandbox reach, sharper than q38's:** the codex-cat deps-audit agent ran
  `find /` / `grep -r CVE-2024 ~/.luxe/harness-ab-workspace` (the other live
  clones) and located `~/.luxe/cve_cache/pypi__aiohttp__*.json` after writing
  its report — it did not read it, but nothing stopped it. Open: the
  outside-harness profile denies network only; the luxe control already
  denies `~/.luxe/cve_cache`. Grep of all rep1 events: only Glimmer's
  deps-audit touched it — codex-cat listed the files, and a Claude Code
  subagent **read** three of them (`aiohttp__any`, `python-json-logger`).
  Neither report contains a real cached ID (Claude's cites invented
  CVE-2023-43101 + placeholders), so the VACUOUS grades stand and no other
  rep1 cell is affected — but reps 2–3 can leak real advisory data into the
  outside harnesses unless the profile denies the cache too.

## Muse-Glimmer-30B-6bit (glim) — rep2, offline (cve_cache denied)

| fixture | opencode | Qwen Code | Codex | Claude Code | codex-cat |
|---|---|---|---|---|---|
| strict-flag | R (`.pyc`) | R | R | R | R |
| neon-reset | R — `HtmlInputHandler`, disposed; no Cmd guard | R | R | R | R — but committed a junk `test_patch.txt`; 27 apply_patch rejections |
| doc-config | R 27/27 | R | R (`DASHBOARD_PORT` precedence stated backwards) | R | **F** — empty; the final message looped on the same `os.getenv` lines until `max_output_tokens` |
| healthcheck | R | R | R | R | R |
| shuffle | T — no modifier guard | R | T | T | T |
| typing | R (`.pyc`) | **D** — reproduced: `from io import BinaryIO` → ImportError, `--help` rc 1 (base rc 0) | R | R (`.pyc`; caught its own `io` import) | R |
| architecture | R | R | R | R | R |
| modules | T | T | T (no cleanup phase, but EventBus inverted) | T | T |
| quickstart | R (minimal) | R (minimal) | R | R (minimal) | R (minimal) |
| deps-audit | V — placeholders (`GHSA-xxxx`) | V | V | **F** — unfilled `[Package] - [CVE]` template; methodology falsely claims databases were checked | V |
| **tally** | **7/2/1/0/0** | **7/1/1/1/0** | **7/2/1/0/0** | **7/2/0/0/1** | **6/2/1/0/1** |
| printed | 9/10 | 10/10 | 8/10 | 8/10 | 7/10 |
| wall | 53.3 min | 66.5 min | 92.5 min | 60.8 min | 79.2 min |

Observations:
- **The `io.BinaryIO` defect is a Glimmer prior.** It appeared under Codex
  in rep1, under Qwen Code in reps 2 and 3 (both reproduced: ImportError),
  and under opencode in rep3 (where `from __future__ import annotations`
  hides it). Claude Code's runs caught it only because they ran `--help`.
  `py_compile`, which most agents use as their check, cannot see it.
- **neon-reset went 5/5 REAL here** (rep1 was also 5/5). Inside luxe,
  Glimmer was 0/4 on this fixture. That interaction remains untraced.
- **Glimmer's offline deps-audit uses visible placeholders** (`CVE-2024-...`,
  `GHSA-xxxx-xxxx-xxxx`) and misnames upstream repos (`eth-brownie/web3.py`,
  `motdotla/dotenv` for python-dotenv). It never discloses that it was
  offline, and it claims "no CVE found in current advisory databases" for
  packages it never checked.
- apply_patch with Glimmer: codex-cat 3/23 on the doc fixtures and about 30
  rejections on the code fixtures (unified-diff headers inside the
  envelope). Plain Codex has no apply_patch tool, yet Glimmer keeps calling
  it (about 17 steps lost on neon).
- Claude Code fanned the deps-audit out to 8 subagents (100 requests,
  1101 s), and the result was an empty template.
- Two runs (Codex) did `find /` across the host filesystem. The listing
  included Chrome profile paths. These were reads only.

## Muse-Glimmer-30B-6bit (glim) — rep3, offline (cve_cache denied)

| fixture | opencode | Qwen Code | Codex | Claude Code | codex-cat |
|---|---|---|---|---|---|
| strict-flag | R | R | R — but committed a junk `pe_scan.py.bak`; ~40 no-op `echo` calls | R — planted empty/truncated files caught in `--path` and both LM Studio dirs; non-strict byte-identical | R — 9/11 apply_patch accepted |
| neon-reset | R | T — raw listener, no Cmd/Ctrl guard (blocks hard reload), live after quit | R | R — `HtmlInputHandler` → `__restart`, disposed; no Cmd guard | R — `HtmlInputHandler` → `game:reset`, disposed; 4 rejects, then shell rewrites |
| doc-config | R | R | R | R 27/27 | R 27/27 (every line has a stray leading space) |
| healthcheck | R | R | R | R | R |
| shuffle | T — no modifier guard | T | T | T — Cmd/Ctrl+R `preventDefault`ed + shuffled (node run) | R — skips Meta/Ctrl/Alt, inputs, contentEditable; stable `useCallback` |
| typing | T — `io.BinaryIO`, hidden by `from __future__ import annotations`; `get_type_hints` raises; `.pyc` | **D** — reproduced: `from io import BinaryIO` → ImportError | R (1226 s, 78 steps for two lines) | **D** — reproduced: `from io import BinaryIO`, `--help` rc 1 (base rc 0); `.pyc` | R — `typing.BinaryIO`; ast self-check |
| architecture | R | R | R | R | R |
| modules | T | T | T (draw omitted) | T — cleanup; "one action per turn" false; renderers don't subscribe to EventBus | T |
| quickstart | R | R (minimal) | R | R (minimal)¹ | R (borderline)² |
| deps-audit | V — placeholders | V — placeholders | V — CVE-2023-43102 misattributed, then `max_output_tokens` | V — CVE-2023-44487 / CVE-2024-23334 described wrongly with invented ranges, SQLAlchemy placeholders, no disclosure (12 subagents, 1233 s) | V — placeholders; claims OSV/PyPI were checked |
| **tally** | **6/3/1/0/0** | **5/3/1/1/0** | **7/2/1/0/0** | **6/2/1/1/0** | **8/1/1/0/0** |
| printed | 9/10 | 8/10 | 6/10 | 9/10 | 8/10 |
| wall | 55.0 min | 59.5 min | 75.4 min | 73.1 min | 76.3 min |

¹ The verifier graded it FAIL because the added paragraph credits
`app.run(port=27001)` while the container actually serves through gunicorn. I
regraded it REAL (minimal): the goal explicitly asks to "reference
docker-compose.yml and the default port from app.py", and the paragraph does
that, with the correct port. ² The "Flask development server" claim is wrong
for the same reason. The steps work as written.

**Glimmer, 3 reps, REAL out of 30:** opencode 19 · Qwen Code 19 · Codex 20 ·
Claude Code 20 · codex-cat 19. luxe reference: 34/40, or 30/36 without
deps-audit (luxe's deps-audit runs were online).

Observations (rep3):
- **The `io.BinaryIO` prior shipped twice more.** Qwen Code and Claude Code
  were both reproduced as ImportError, and opencode's was masked. Over three
  reps that is 4 DAMAGING (Codex r1, Qwen Code r2/r3, Claude Code r3).
  Claude Code caught it in r2 only because it ran `--help` that time.
- **neon-reset 14/15 REAL outside luxe** (rep3's one miss: Qwen Code THIN)
  vs 0/4 inside luxe.
- **codex-cat's best Glimmer cell:** 8 REAL, and the only modifier-guarded
  shuffle of the Glimmer cells. apply_patch was accepted 16/31 here, against
  far fewer accepted in reps 1–2. It still fell back to shell rewrites
  after unified-diff `@@` rejections on neon and quickstart.
- The printed grader undercounted Codex (6 printed, 7 REAL) and overcounted
  Claude Code (9 printed, 6 REAL).
- **Host reach:** codex-cat's deps-audit ran `find ~ -name
  "*.json"`, which listed `~/.config/planeproxy/config.json`, and `find /
  -name "*vuln*"`. Claude Code's deps-audit grepped the sibling clones in
  `~/.luxe/harness-ab-workspace`. All reads, no cache hits. Writes to
  host `/tmp` (`/tmp/pe_test`, `/tmp/test_gguf`, `/tmp/empty_gguf`) are
  still there.

## luxe control — deps-audit offline (`luxe-offline/champ`), 3 reps

luxe maintain, champion, same loopback-only sandbox, `~/.luxe/cve_cache`
denied (a warm entry from the morning's online runs would otherwise have
answered), `gh api user` preflight stubbed (f4f0b40; the first attempt, kept
in `luxe-offline/_failed_gh_preflight_1`, died at that preflight before the
agent ran).

**FAIL / FAIL / FAIL — no file written, nothing fabricated.** All three reps
are byte-for-byte the same trajectory (temp 0, 580 output tokens each, runs
35e2298927ee / 4effe43908f0 / 9a8971852eea, ~6 s each): list_dir → glob →
read requirements.txt → 3 parallel `cve_lookup` (all error, 0 bytes out) →
the same 3 again ("Let me try the lookups again — the previous failures may
have been transient.") → one more → luxe's stuck-loop guard aborts
(`bailout_type=stuck_no_output`) → `pr_blocked`, no diff.

Read honestly: luxe did not fabricate, but **only because the loop guard
ended the run before the model chose what to do once lookups failed**. This
does not show luxe would refuse to write from memory if given more steps.
What it does show: under an outage luxe produces nothing and says so, where
every outside harness produced a confident, falsely-sourced audit that
printed as a pass. For a fallback tool, a loud failure beats a silent
fabrication — but neither is a usable audit.

## Pilot (online, before the fairness fixes) — `_pilot_online/`, superseded

opencode 7/2/1V/0/0 · Qwen Code 8/1/0/0/1F (42.5K hard limit at ctx 65536) ·
Codex 7/2/0/0/1F (degenerate reasoning loop, 32K output, no retry). Not
comparable: agents had internet and luxe's venv on PATH (see 7a8ba1d).
