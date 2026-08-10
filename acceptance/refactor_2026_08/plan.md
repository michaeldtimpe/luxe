# Plan: luxe consolidation refactor — dedup · extraction · layering

**Executor:** Opus 5, running autonomously on m5 in `~/Downloads/luxe`.
**Verifier:** a separate Claude session will independently re-check everything against §7 after you finish — write the report it expects (§8).
**User decisions already made (do not re-ask):**

- This is a **consolidation pass, not a redesign**. Every item is a move, a merge of proven-identical text, a delete of unreferenced code, or a helper extracted from verbatim copies. If an item tempts you to "improve" logic, message text, flag values, or ordering while moving it — don't. Byte-identical behavior is the goal; the diff should be explainable as relocation + deletion.
- **`run_agent` stays one function.** It is 1,207 lines of one algorithm; do not split its body. Only its *preamble* (constants, env parsing) is in scope.
- **Benchmark-path items (Phase 3) are in scope but individually gated.** Each is its own commit with `tests/test_golden_request.py` run before and after. No full maintain-suite bench is required for this cycle — golden-request byte-identity + the full test suite is the gate. If any Phase 3 item turns out riskier than described, **skip it and record why** in the report; a skipped Phase 3 item is an acceptable outcome, a broken golden is not.
- Git discipline: rebase-only linear history (CLAUDE.md rule 7), **one commit per numbered item** (small items inside one workstream may share a commit if the report says so). Suite green before every commit.
- Prompts stay in `agents/prompts.py`; walk and update every touched dir's `<dir>.sdd`; no `Path.rglob` on user roots (CLAUDE.md rules).

---

## 0. Ground truth (verified 2026-08-04 by AST + grep survey — trust these anchors, re-verify cheaply)

Sizes: `src/luxe` ≈ 32.8k lines; `cli.py` 2,234; `chat/commands.py` 1,792; `agents/loop.py` 1,785. Tests: 107 files, ~2,126 test functions, flat under `tests/`; `pyproject.toml` `addopts` deselects `live_model`/`live_backend`. Run everything with `uv run pytest` (plain `python3` has no pytest; if collection errors mention missing extras, `uv sync --extra chat --extra dev` first — note `uv sync` can prune `mpmath` needed by `test_miss_func_49`, reinstall if that one goes red).

- **Guardrails duplication (the headline):** `agents/guardrails.py` (766 lines, lifted from loop.py in commit `4581d38`, 2026-05-26) was a **copy, not a move**. 27 module-level constants in `loop.py:156–575` are byte-identical (AST-literal-verified) to constants in `guardrails.py`: `_WRITE_PRESSURE_{MESSAGE,MIN_TOOLS,MIN_TOKENS,MIN_STEP,MAX_TOOLS_BEFORE_FIRE}`, `_PROSE_BURST_{MESSAGE,MAX_STEP,MIN_DELTA}`, `_ACTION_DENSITY_GATE_{MESSAGE,MIN_STEP,MIN_TOKENS,MAX_TOOLS,MIN_TURNS_AFTER_BAIL}`, `_EARLY_BAIL_MESSAGE{,_NO_ABSTAIN,_SOFT_ANCHOR,_COMMIT_IMPERATIVE,_BREADTH_PROBE}`, `_EARLY_BAIL_{MIN_STEP,MIN_READS}`, `_HABITUATION_EXIT_{MIN_STEP,MIN_KINDS}`, `_POST_WRITE_IDLE_MAX`, `_MAX_CONSECUTIVE_REPEAT_STEPS`, `_BREADTH_PROBE_ESCALATION_COUNT`, `_CONVERGENCE_{LOW,HIGH}_THRESHOLD`. Also `_v1105_synthesis_looping_signature` at `loop.py:514–524` ≡ `guardrails.py:467–477`. Several loop.py copies are unreferenced within loop.py and survive only because tests import them **from `luxe.agents.loop`** (`test_loop_write_pressure.py`, `test_loop_spec_gate.py`, `test_loop_respond_terminal.py`, `test_loop_adaptive_policy.py`). The running guards already consume guardrails' copies.
- **cli.py responsibility map:** maintain pipeline inline at `:108–556` (~449 ln, spawned by `benchmarks/maintain_suite/run.py:642` and `benchmarks/swebench/adapter.py:226` via `python -m luxe.cli maintain`); chat/code launch at `:558–1008` (~450 ln, self-documented chat-only at `:562`); gitkit glue `:1078–1238`; pull surface `:1240–1363` + helpers `:1769–1969` (~300 ln total); language detection `:2125–2193` (`_LANG_BY_EXT`, `_languages_from_paths`, `_detect_languages_for_repo`).
- **commands.py dispatch:** `_HELP_ROWS` (`:56–102`) → `_HIDDEN_COMMANDS` (`:110`) → `_build_handlers()` dict (`:166–219`) → `dispatch()` (`:148`). `tests/test_chat_commands.py:1025–1046` pins `handlers ≡ _HELP_ROWS ∪ _HIDDEN_COMMANDS` in both directions — a free verifier for any split. `return CommandResult(handled=True)` appears 112×; 6 pure toggle commands share an 8-line shape; 13 hand-written `Usage:` strings duplicate `_HELP_ROWS`.
- **Layering cycles (all verified):**
  - **C1** `modelstore.py:34` → `from luxe.chat.origin import network_mounts` — the only unconditional low-level→chat import in the repo (`network_mounts` is a `/sbin/mount` parse at `chat/origin.py:135`).
  - **C2** gitkit ↔ chat: `gitkit/runner.py:153,244,449`, `gitkit/brief.py:89,139`, `gitkit/deep.py:1157` do function-local imports of `chat.render` (`ChatCancelled`, `raise_if_cancelled`, `truncate_for_display`; defined at `chat/render.py:76–90,148–157,355–385`).
  - **C3** gitkit → cli: `gitkit/runner.py:245`, `gitkit/apply.py:180`, `gitkit/brief.py:140` import private `cli._detect_languages_for_repo`; combined with `cli.py:1092,1121,1628 → gitkit` this is a true cycle.
  - Healthy and to be preserved: `chat/ → agents/` one-directional; `agents/` never imports `chat/`.
- **Duplication inventory:**
  - **D1** identical `os.walk` prune predicate — including its precedence quirk `(d not in excludes and not d.startswith(".")) or d == ".github"` — at `search.py:84`, `symbols.py:229`, `repo_index.py:164`, `gitkit/deep.py:277`, `gitkit/deep.py:365`. `search.py:75–95` ≡ `symbols.py:220–241` structurally (only the per-file filter differs). `fswalk.py` is the canonical home.
  - **D2** `_DEFAULT_EXCLUDES` byte-identical ×3 (`symbols.py:81`, `repo_index.py:39`, `search.py:31`). The two `_LANGUAGE_EXTENSIONS` tables (`symbols.py:30`, `repo_index.py:20`) **deliberately differ** — do NOT merge them; add a comment at each stating why.
  - **D3** eight git-subprocess wrappers: `gitkit/apply.py:33`, `gitkit/health.py:33`, `chat/inspection.py:126`, `chat/status.py:132`, `tools/git.py:12`, `buildinfo.py:21`, `cli.py:1396`, `chat/project.py:61`. `inspection.py:126` and `health.py:33` share the exact `(ok, out)` contract. **Do not touch** raw `subprocess.run(["git",…])` sites in `pr.py`, `citations.py`, `spec_validator.py` — benchmark path, exact flags matter.
  - **D4** `human_bytes` ladders: `modelstore.py:53` (public), `planeproxy.py:286`, `chat/render.py:121`. (`chat/status.py:69` `_human` is a different unit system — leave it.)
  - **D5** `_tilde` home-relative helper ×3 (`cli.py:608`, `chat/origin.py:192`, inlined `chat/status.py:277`); `Path.home()/".luxe"` spelled out at 12 sites.
  - **D6** CLI↔chat mirrors: `pull` implemented twice (`cli.py:1268–1363,1769–1969` vs `commands.py:436–592`); backend-name validation copied verbatim 3× in cli.py (`:755`, `:1483`, `:1585`); ✓/✗ glyph render loop ×4 (`cli.py:1703,1764`, `commands.py:1139,1168`). The **doctor/ready sharing via `inspection.run_doctor`/`render_doctor` is the model to follow**.
  - **D7** `Backend(...).unload_all_loaded()` in `try/except: pass` ×6 (`cli.py:536,1065,1112,1135,1535,1639`); `load_config(config_path or _default_chat_config())` ×10.
- **Dead code (AST sweep, ~900 defs, only these):** `citations.py:57,61` (`Citation.is_ambiguous`/`.is_cleared`), `mcp/client.py:171,175` (`ServerDown`/`HardCapExceeded`, never raised or caught), `outage.py:17` (`card_path()`, superseded by `CARD_PATH` at `:14` which `tests/test_cli_ready.py:241` monkeypatches), `staleproc.py:224` (`cellar_glob`, self-declared debug helper). **Not dead** (verified, leave alone): `tools/respond.py`, `planeproxy.py`, all `*_cmd` Click entry points, Textual/HTMLParser framework hooks.
- **Structure-pinning tests you'll lean on:** `tests/test_golden_request.py` (exact oMLX HTTP body from a real `run_single`; regen only via `LUXE_UPDATE_GOLDEN=1` — **this cycle must not regenerate goldens**); `test_chat_commands.py:1025` (handler/help parity); `test_cli_ready.py:238` (every command OUTAGE.md names must exist); `test_sdd.py` (contract shape); `test_prompts.py` (registry completeness); `test_gitkit.py:174` (CLI alias table); `test_turn_core.py` (REPL/TUI seam).
- **Tests/scripts importing privates that must keep working via re-exports:** `test_chat_theme.py:90,98,125,175`, `test_chat_project.py:242`, `test_spec_resolver.py:602` (cli privates); `test_pr_flow.py:443`, `test_turn_core.py:140` (`cli._infer_task_type`); `scripts/chunk_conclude_ab.py:109` (`cli._detect_languages_for_repo`).

---

## 1. Phase 1 — proven-identical dedup + dead code (no benchmark exposure)

### 1.1 Guardrails constants: delete loop.py's copies, re-export for tests

Replace the duplicated definitions in `loop.py:156–575` with a single import block from `luxe.agents.guardrails` (`# noqa: F401` re-exports so the four test files' `from luxe.agents.loop import _X` imports keep resolving). Delete the verbatim `_v1105_synthesis_looping_signature` copy and import guardrails'. Do **not** move the `_RESPOND_*` nudge strings (`loop.py:221–275`) in this item — they exist only in loop.py and are covered by `test_loop_respond_terminal.py`; leave them where they are.

Add `tests/test_guardrails_identity.py`: for every re-exported name, `assert getattr(loop, name) is getattr(guardrails, name)` — pins that the dedup is a re-export, not a third copy.

Expected: −100…130 lines; `test_loop_write_pressure.py`, `test_loop_spec_gate.py`, `test_loop_respond_terminal.py`, `test_loop_adaptive_policy.py` pass **unmodified**.

### 1.2 Delete the six dead symbols

`Citation.is_ambiguous`, `Citation.is_cleared`, `ServerDown`, `HardCapExceeded`, `outage.card_path()`, `staleproc.cellar_glob()`. After deletion, `grep -rn <name>` over `src tests benchmarks scripts` must return nothing (definition included). ~−35 lines.

---

## 2. Phase 2 — structural moves (no benchmark exposure)

### 2.1 Split `chat/commands.py` into group modules behind `_build_handlers()`

Move handler bodies into six sibling modules; `commands.py` keeps `CommandResult`/`CommandContext`, `_HELP_ROWS`, `_HIDDEN_COMMANDS`, `is_command`, `dispatch`, `_build_handlers` (now importing the group modules), `_help`, `_retry`, `_clear`, `_quit` (~250-line residual):

| Module | Commands | ~Lines |
|---|---|---|
| `chat/cmd_models.py` | `/model` `/backend` `/pull` `/unload` + `_pull_*` helpers | 380 |
| `chat/cmd_diag.py` | `/status` `/tools` `/doctor` `/outage` `/net` `/planeproxy` | 235 |
| `chat/cmd_transcript.py` | `/diff` `/export` `/full` `/copy` + `_print_patch`, `_esc` | 160 |
| `chat/cmd_agentic.py` | `/goal` `/plan` `/attach` `/sys` | 180 |
| `chat/cmd_project.py` | `/project` `/index` `/init` `/note` `/memory` `/gitaudit` `/gitchange` `/compare` | 260 |
| `chat/cmd_toggles.py` | `/theme` `/write` `/bash` `/web` `/verbose` `/reasoning` `/debug` `/terse` `/compact` `/ctx` `/use` | 290 |

While splitting `cmd_toggles.py`, collapse the six identical bool-toggle handlers into one `_toggle(attr, label, hint)` factory (~90→30 lines) and add a `_usage(ctx, "/cmd")` helper that reads the usage string from `_HELP_ROWS` instead of the 13 hand-written copies. **Output strings must not change** — `tests/test_chat_commands.py` asserts on them.

Update `chat/chat.sdd` (Owns list). The parity test at `test_chat_commands.py:1025` is the drift-killer here; it must pass unmodified.

### 2.2 Lift the chat/code launch block out of cli.py → `chat/launch.py`

Move `cli.py:558–1008` (`_build_chat_indexes`, `_resolve_theme_name`, `_shared_chat_options`, `_run_interactive`, `_apply_slot_overrides`, `_tilde`, …). Click commands stay in cli.py as thin shells. Keep module-level re-exports in `cli` for every private that `test_chat_theme.py`, `test_chat_project.py`, `test_spec_resolver.py` import.

### 2.3 Break the cycles (C1, C2, C3)

- **C1:** move `network_mounts` (+ its mount-parse helpers) from `chat/origin.py:135` to a neutral home — suggest `src/luxe/mounts.py` or into `modelstore.py` itself if it has no other consumers; `chat/origin.py` imports it back. `modelstore` must no longer import `chat.*` at module level.
- **C2:** move `CancelToken`/`ChatCancelled`/`raise_if_cancelled` and `truncate_for_display` from `chat/render.py` into neutral `src/luxe/cancel.py` and `src/luxe/textfmt.py`; re-export from `chat/render.py` unchanged. Convert gitkit's six function-local imports to top-level imports of the neutral modules.
- **C3:** move `_LANG_BY_EXT`, `_languages_from_paths`, `_detect_languages_for_repo` (`cli.py:2125–2193`) into `repo_index.py` (de-facto home — `gitkit/diffscope.py:107` already imports repo_index privates). Re-export from `cli` (tests + `scripts/chunk_conclude_ab.py` import them there). Convert gitkit's three function-local cli imports to top-level repo_index imports. **Note:** `_detect_languages_for_repo` feeds `maintain`'s `run_single(languages=…)` — this is a pure move with re-export, but run the golden test before/after anyway and say so in the report.

Add `tests/test_layering.py`: an AST-based check asserting (a) no module outside `chat/` imports `chat.*` at module level except via the sanctioned re-export list, (b) `agents/` never imports `chat/`, (c) `gitkit/` has no function-local imports of `chat.render` or `luxe.cli` left. This makes the layering durable instead of a one-time cleanup.

### 2.4 One git runner

New `src/luxe/gitcmd.py`: `run(repo, *args, timeout=…) -> CompletedProcess` plus a `run_ok(repo, *args, timeout) -> tuple[bool, str]` adapter. Migrate, in order: `chat/inspection.py:126` + `gitkit/health.py:33` (identical contract — straight merge), then `chat/status.py:132`, `gitkit/apply.py:33`, `buildinfo.py:21`, `tools/git.py:12`, `cli.py:1396`. Preserve each site's timeout value exactly. **Leave `pr.py`, `citations.py`, `spec_validator.py`, `chat/smoke.py`, `gitclone.py`, `repo_index.py` raw call sites alone.**

### 2.5 Unify `pull` behind modelstore

One `modelstore` function owning resolve-sources → preview → mount-copy-or-HF-download, taking an `on_progress` callback; `cli.py` passes the Rich-progress renderer, `chat/cmd_models.py` passes the 10%-tick transcript renderer. Both surfaces must resolve the same source list for the same ref (add that assertion to `tests/test_modelstore.py`). Keep the consent shape (`/pull <ref>` previews, `--yes`/`--yes`-equivalent transfers) byte-identical. Expected ~−120 lines.

### 2.6 cli idiom helpers + small-fry dedup

- `_unload_unless(...)` for the 6 teardown copies (**skip the `:536` site inside `maintain` — that's Phase 3 territory; leave it**).
- `_chat_cfg(config_path)` for the 10 `load_config(config_path or _default_chat_config())` sites.
- One backend-name-validation helper for the 3 verbatim copies.
- `luxe_home() -> Path` in a neutral module for the 12 `Path.home()/".luxe"` sites (pure refactor; do not add configurability).
- `human_bytes`: consolidate `planeproxy.py:286` and `chat/render.py:121` onto `modelstore.human_bytes` (add `sep=""` param if needed to keep output byte-identical). Leave `chat/status.py:69`.
- `_tilde` ×3 → one home (wherever `launch.py`/`textfmt.py` makes natural).
- ✓/✗ glyph render loop ×4 → one `render_ok_lines(...)` helper.
- `_DEFAULT_EXCLUDES` ×3 → one definition (this constant is shared data, not the walk itself — the walk unification is Phase 3). Add the "deliberately different" comments to the two `_LANGUAGE_EXTENSIONS` tables.

---

## 3. Phase 3 — benchmark-path items (each = own commit, golden-gated, skippable)

Run `uv run pytest tests/test_golden_request.py` immediately before and after each of these commits. Goldens in `tests/golden/` must be byte-identical to `origin/main`'s throughout the cycle.

### 3.1 One pruned-walk helper

`fswalk.iter_pruned(root, excludes, keep=(".github",))` (or equivalent) reproducing the shared predicate **exactly, including the `(A and B) or C` precedence quirk** — do not "fix" the quirk in this cycle; note it in the report as a candidate behavior change for a future evidence-gated pass. Route `search.py:84`, `symbols.py:229`, `repo_index.py:164`, `gitkit/deep.py:277,365` through it; merge `search._candidates`/`symbols._symbol_candidates` onto one parameterized walker. Add an equivalence test: fixture tree containing dotdirs, an excluded dir, `.github`, an oversize file → `sorted(new(...))` equals the hand-computed legacy result.

### 3.2 Lift the `maintain` body → `src/luxe/maintain.py`

`cli.py:137–556` moves; the Click command becomes a thin shell calling `maintain_pipeline(...)`. `python -m luxe.cli maintain` must keep working (both benchmark adapters spawn it). Byte-diff `luxe maintain --help` before/after. This lands cli.py near ~900 lines.

### 3.3 `RunFlags.from_env()` for run_agent's env preamble

Extract the ~20 `LUXE_*` reads at `loop.py:~620–790` into a frozen dataclass in `agents/flags.py`, constructed once at the same point in `run_agent`. Same env vars, same defaults, same malformed-value fallbacks, same read order. Parametrized test asserting field-by-field behavior under monkeypatched env, including malformed values. Update `agents/agents.sdd` if it names loop.py as the home of these flags.

---

## 4. Rules that apply to every item

1. **No behavior change.** User-visible strings, exit codes, timeouts, env-var semantics, consent flows: byte-identical. The one sanctioned "new behavior" is *new tests*.
2. **No golden regeneration.** If a golden test fails, the refactor is wrong — revert the item, don't regenerate.
3. **Re-exports over test edits.** Prefer keeping old import paths alive via re-export; only touch a test file to *add* tests or update an import the plan explicitly lists. Never weaken an assertion.
4. **`.sdd` hygiene:** every touched directory's `.sdd` gets its Owns/Must lists updated; new neutral modules (`gitcmd.py`, `cancel.py`, `textfmt.py`, `maintain.py`, `flags.py`, `launch.py`, `cmd_*.py`) are listed in the owning dir's `.sdd`. `tests/test_sdd.py` stays green.
5. **Commit messages:** `refactor(<area>): <what moved/merged> [P<phase>.<item>]`, with the standard trailer lines from the repo's convention.
6. **Suite cadence:** full `uv run pytest` before each commit; if wall time forces it, targeted files during development but the FULL suite at each commit boundary.
7. **Stop-and-record:** any item where reality contradicts §0's anchors (line numbers drifted, a claimed-identical constant differs, an "unused" symbol has a consumer) — stop that item, record the discrepancy in the report, move on. Do not improvise a bigger change.

---

## 5. Expected end state (targets, not hard gates)

- Net deletion ≈ **−600 lines**; ~2,400 lines relocated out of the three big files.
- `cli.py` ≈ 900 lines of thin command shells; `chat/commands.py` residual ≈ 250–350; `loop.py` ≈ 1,480–1,650.
- Zero module-level imports of `chat.*` from root-tier modules; zero function-local cycle-dodging imports in `gitkit/`.
- New durable tests: `test_guardrails_identity.py`, `test_layering.py`, walk-equivalence, `RunFlags` env matrix, pull source-resolution parity.

---

## 6. Explicitly OUT of scope

- Splitting `run_agent`'s body; any change to guard *logic* or thresholds.
- Fixing the `.github` precedence quirk (record it; future evidence-gated change).
- Merging the two `_LANGUAGE_EXTENSIONS` tables.
- `pr.py` / `citations.py` / `spec_validator.py` git call sites.
- Regenerating goldens; any prompt text change; any `configs/` change.
- Performance work beyond what dedup naturally yields; no new dependencies; no new env vars or flags (except none — `RunFlags` reads existing ones).
- Renaming user-facing commands, flags, or output.

---

## 7. Verification (what the verifying session will do — make all of this pass)

1. **Clean tree, linear history:** `git status` clean; `git log --oneline origin/main..HEAD` shows one commit per completed item, no merge commits; commit messages follow §4.5.
2. **Full suite:** `uv run pytest` green (excluding the default-deselected `live_*` markers). Count of collected tests ≥ pre-cycle count (2,126 functions) — refactors may add tests, never lose them.
3. **Goldens untouched:** `git diff origin/main -- tests/golden/` is empty. `uv run pytest tests/test_golden_request.py` passes.
4. **Guardrails identity:** `tests/test_guardrails_identity.py` passes; `grep -c "_WRITE_PRESSURE_MESSAGE\s*=" src/luxe/agents/loop.py` returns 0 (no local definition); the four loop test files show no diff vs origin/main (`git diff origin/main -- tests/test_loop_write_pressure.py tests/test_loop_spec_gate.py tests/test_loop_respond_terminal.py tests/test_loop_adaptive_policy.py` empty).
5. **Dead code gone:** for each §1.2 name, `grep -rn <name> src tests benchmarks scripts` returns nothing.
6. **Layering:** `tests/test_layering.py` passes; spot-check `grep -n "from luxe.chat" src/luxe/modelstore.py` empty; `grep -rn "from luxe.cli import\|from luxe.chat.render import" src/luxe/gitkit/` shows only top-level imports of the new neutral modules (i.e., nothing matching).
7. **Size targets:** `wc -l src/luxe/cli.py src/luxe/chat/commands.py src/luxe/agents/loop.py` within ±15% of §5's targets for completed items (skipped Phase 3 items relax the cli.py target by their stated size).
8. **Help-surface byte-parity:** for every top-level command, `luxe <cmd> --help` output identical to a fresh `origin/main` worktree's output (the verifier will diff programmatically; `COLUMNS=200` pinned on both sides). Same for `luxe --help` command list.
9. **Parity + structure tests unmodified and green:** `test_chat_commands.py` handler/help parity, `test_cli_ready.py` OUTAGE.md command check, `test_sdd.py`, `test_prompts.py`, `test_gitkit.py` alias test — all pass, none weakened (diff vs origin/main shows only plan-sanctioned edits).
10. **Behavioral spot-checks (cheap, offline):** `luxe outage` prints the card, exit 0; `printf '/help\n/quit\n' | uv run luxe chat --repo <scratch>` works; `uv run luxe ready` produces a verdict (endpoint may be down — NOT READY is fine, a traceback is not).
11. **Report exists and is honest:** every §1–3 item has a row (done/skipped + evidence); every skip has a recorded reason; every §0-anchor discrepancy encountered is listed.

---

## 8. Report (write this last)

Write `~/Downloads/luxe-refactor-REPORT.md`:

- Per-item table: item → status (done / skipped / partial) → commit sha → lines moved/deleted → tests added → verification evidence (which §7 checks you ran yourself and their results).
- Any §0 anchor that was wrong and what you did about it (§4.7).
- Golden test runs: when you ran `test_golden_request.py` for each Phase 3 item and the result.
- Final `wc -l` for the three big files and total `src/luxe`; final `uv run pytest` tail (pass/fail/skip counts, wall time).
- Deferred-candidates section: the `.github` precedence quirk, `run_agent` body, `_LANGUAGE_EXTENSIONS` merge, anything you noticed but correctly didn't do.
