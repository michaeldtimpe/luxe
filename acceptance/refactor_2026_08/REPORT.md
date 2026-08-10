# luxe consolidation refactor — execution report

**Executor:** Opus 5, `~/Downloads/luxe`, 2026-08-04.
**Branch:** `main`, 11 commits ahead of `origin/main`, **committed locally only — nothing pushed.**
**Suite:** 2,353 passed · 2 skipped · 4 deselected · 66s. Green before every commit.
**Goldens:** never regenerated; `git diff origin/main -- tests/golden/` is empty.

All 11 numbered items are **done**. Nothing was skipped at the item level. Four
sub-parts *inside* items were skipped on §4.1 grounds (behaviour would have
changed) and are listed in §2 with reasons.

---

## 1. Per-item table

| Item | Status | Commit | Lines moved / deleted | Tests added | Evidence I ran |
|---|---|---|---|---|---|
| **1.1** guardrails constants | done | `6b5f106` | loop.py 1,785 → 1,484 (−301); guardrails.py 766 → 982 (+216, comments only) | `test_guardrails_identity.py`, 32 | §7.2 suite; §7.4 `grep -c "_WRITE_PRESSURE_MESSAGE\s*=" loop.py` → **0**; the four loop test files show **no diff** vs `origin/main`; AST dump of `guardrails.py` **identical** to HEAD's (comment-only diff proven mechanically) |
| **1.2** six dead symbols | done | `fe377c2` | −25 | — | §7.5 `grep -rn <name> src tests benchmarks scripts` → **0 hits for all six** |
| **2.1** split `commands.py` | done | `d85bf60` | commands.py 1,792 → 333 (−1,459); six `cmd_*.py` = 1,595 | — (existing parity test is the gate) | §7.9 `test_chat_commands.py` handler/help parity **unmodified and green**; all 222 chat-command tests green; toggle-factory output diffed against HEAD's handlers through a Rich console **with ANSI on**, 12/12 byte-identical; the four `_usage` sites diffed the same way, 4/4 identical |
| **2.2** lift chat/code launch | done | `4eb90c0` | cli.py 2,234 → 1,815 (−419); `chat/launch.py` 458 | — | §7.8 `COLUMNS=200 luxe <cmd> --help` **byte-identical** across 20 commands + 4 subcommands + the root list |
| **2.3** break C1/C2/C3 | done | `dffe774` | new `mounts.py` 58 + `cancel.py` 34 + `textfmt.py` 40; 9 function-local imports in gitkit removed | `test_layering.py`, 6 | §7.6 `grep -n "from luxe.chat" modelstore.py` → **0**; `grep -rn "from luxe.cli import\|from luxe.chat.render import" src/luxe/gitkit/` → **0**; **golden run before and after** (5 passed / 5 passed) because `_detect_languages_for_repo` feeds maintain's `run_single(languages=)` |
| **2.4** one git runner | done | `5e1a35c` | `gitcmd.py` 71; 8 wrappers migrated | — | full suite; each site's timeout preserved exactly (incl. gitkit.apply's *absence* of one) |
| **2.5** unify `pull` | done | `707022a` | ~−15 (see §2) | `TestPullSourceParity`, 5 | Both surfaces driven end-to-end with `resolve_pull_sources` spied — asserted to reach it with **identical arguments** |
| **2.6** cli idioms + small-fry | done | `60f95b4` | 3 teardowns → 1 helper; 10 config-defaults → 1; 3 backend checks → 1; 12 `~/.luxe` → `paths.luxe_home()`; 3 `_tilde` → 1; 4 glyph loops → 1; 3 `_DEFAULT_EXCLUDES` → 1 | — | help parity re-verified (24 surfaces identical) |
| **3.1** one pruned walk | done | `5de4aeb` | 5 walk copies → `fswalk.iter_pruned` / `iter_pruned_files` | `test_fswalk.py` +7 | **Golden before: 5 passed. Golden after: 5 passed.** Goldens untouched |
| **3.2** lift `maintain` | done | `9bd2f10` | cli.py 1,807 → 1,383 (−424); `maintain.py` 472 | — | **Golden before: 5 passed. Golden after: 5 passed.** `luxe maintain --help` byte-identical; `python -m luxe.cli maintain --help` differs only in Click's usage prog-name, as it always has |
| **3.3** `RunFlags.from_env` | done | `6f7daf6` | 16 `os.environ.get` → one frozen dataclass; loop.py 1,484 → 1,465 | `test_run_flags.py`, 111 | **Golden before: 5 passed. Golden after: 5 passed.** Differential check over **196 environments** (each var × 12 values + all-set combos): verbatim transcription of the old inline expressions vs `from_env` → **0 mismatches** |

**Commit-message form:** every commit is `refactor(<area>): <what> [P<phase>.<item>]`
with the repo's `Co-Authored-By` + `Claude-Session` trailers. Zero merge commits.

---

## 2. §0 anchor discrepancies (rule 4.7)

Seven anchors were wrong. In each case I stopped, kept behaviour, and recorded it
rather than improvising a bigger change.

1. **"27 byte-identical constants" → it is 28.** The AST sweep found
   `_EARLY_BAIL_MESSAGE_MODES` as well as the 27 named in §0. Harmless — I
   deduped all 28.

2. **"13 hand-written `Usage:` strings duplicate `_HELP_ROWS`" → only 4 do.**
   `/planeproxy`, `/plan`, `/attach`, `/compare` are genuine duplicates and now go
   through `_usage`. The other nine are **subcommand-specific** and the help table
   cannot express them: `/sys add <rule>`, `/sys remove <index>`,
   `/memory add|promote|forget <x>` (×3), `/pull --search <query>`,
   `/use chat|plan|code`, `/goal <objective> · /goal stop`,
   `/verbose [diff|full|off] (current: X)`. Routing those through the helper would
   change output. Left hand-written; the rule is now in `chat.sdd`.

3. **"6 pure toggle commands share an 8-line shape" → 5 do, and the collapse does
   not save lines.** `/web` prints a per-provider report and `/debug` drives two
   fields off a compound predicate — neither is the shape. The five that are
   (`/write` `/bash` `/reasoning` `/terse` `/compact`) need four different
   style pairs, two different label pairs, and one conditional tail, so the
   parameterised factory is about as long as what it replaced (plan estimated
   90→30). I kept it — it makes the five declarative and drift-proof, and it is
   proven byte-identical — but the line saving is ~0, not ~60.

4. **`chat.inspection._git` and `gitkit.health._run_git` do NOT share "the exact
   `(ok, out)` contract".** They differ in three ways: `git -C` vs `cwd=`;
   `str(e)` vs `"git not found"` / `"git timed out after Ns"`; and on a non-zero
   exit `stderr.strip()` vs `(stderr or stdout).strip()`. A "straight merge"
   would have changed user-visible strings in one of them. `gitcmd` therefore owns
   the **launch** and each caller keeps its **error policy**; `run_ok` is
   inspection's old body moved verbatim.

5. **`pull` is not "implemented twice" past the resolve step.** Source resolution
   genuinely was duplicated and is now shared. Everything after it differs by
   design: consent (chat previews then `--yes`; the CLI prompts `click.confirm`),
   progress (one line per 10% vs a Rich bar), every message string, and the
   CLI-only `--list` / `--remove` / HF-cache-materialize paths. Expected −120
   lines; actual ≈ −15. Recorded in `chat.sdd` so this isn't re-attempted.

6. **`gitkit/brief.py:89` is a `chat.project` import, not `chat.render`.** It is
   real gitkit→chat coupling (project resolution) but outside C2's scope and
   outside the plan's own `test_layering` check (c). Left alone; noted in §5.

7. **The plan's `cli.py ≈ 900` target is unreachable from its own item list.**
   2,234 − 450 (chat launch) − 449 (maintain body) = 1,335 *before* the two Click
   shells, the re-export blocks and the 2.6 helpers are added back. Actual: **1,383**
   — i.e. exactly where the plan's own arithmetic lands, +3.6% over the reachable
   figure and +54% over the stated one. No item was skipped to get there.

**One non-anchor discrepancy:** `_DEFAULT_EXCLUDES` could not be folded into the
existing `fswalk.DEFAULT_SKIP_DIRS` — that set is strictly larger (`.hg`, `.svn`,
`env`, `.tox`, `site-packages`) and reusing it would have changed what the three
index builders see. A separate `INDEX_EXCLUDE_DIRS` holds the exact 12 entries.

### Sub-parts skipped inside items (all §4.1 — output would change)

- **`human_bytes` consolidation (2.6 / D4).** The three ladders are not the same
  ladder. `chat.render._human_bytes` stops at MB, so 1 GiB renders `"1024.0 MB"`
  where `modelstore.human_bytes` says `"1.0 GB"`. `planeproxy._human_bytes` tests
  `n < 1024` where modelstore tests `abs(n) < 1024`, which diverges on negative
  input. A `sep=""` parameter fixes the spacing but not either of these. Skipped.
- **Merging `search._candidates` / `symbols._symbol_candidates` wholesale (3.1).**
  Their *walks* are now one call. Their precomputed-`files` branches genuinely
  differ — search filters that list by extension, symbols yields it whole — so
  merging them would change what a chat session indexes.
- **The `maintain` teardown copy (2.6).** Explicitly out of scope per the plan
  (`skip the :536 site`), and it differs anyway: it reports what it unloaded.
- **`smoke` / `init` teardowns (2.6).** Each constructs `Backend` from a different
  endpoint + key. Left as-is rather than adding parameters to `_unload_unless`.

---

## 3. Golden-request runs

`tests/test_golden_request.py` was run **immediately before and immediately after**
every Phase 3 commit, plus before and after the Phase 2.3 language-detection move
(a pure move with re-export, but it feeds `maintain`'s `run_single(languages=)`).

| When | Result |
|---|---|
| Before 2.3 (C3 language-detection move) | 5 passed |
| After 2.3 | 5 passed |
| Before 3.1 (pruned walk) | 5 passed |
| After 3.1 | 5 passed |
| Before 3.2 (`maintain` lift) | 5 passed |
| After 3.2 | 5 passed |
| Before 3.3 (`RunFlags`) | 5 passed |
| After 3.3 | 5 passed |
| Final | 5 passed |

`git diff origin/main -- tests/golden/` → **empty** at every point. `LUXE_UPDATE_GOLDEN`
was never set.

---

## 4. Final measurements

### The three big files

| File | origin/main | HEAD | Δ | §5 target | Verdict |
|---|---|---|---|---|---|
| `src/luxe/cli.py` | 2,234 | **1,383** | −851 (−38%) | ~900 | over, but see §2.7 — the plan's target is arithmetically unreachable; 1,383 is +3.6% over the reachable 1,335 |
| `src/luxe/chat/commands.py` | 1,792 | **333** | −1,459 (−81%) | 250–350 | **in range** |
| `src/luxe/agents/loop.py` | 1,785 | **1,465** | −320 (−18%) | 1,480–1,650 | 1.0% under the low end |
| **total** | **5,811** | **3,181** | **−2,630 (−45%)** | ~2,400 relocated | **exceeded** |

### Whole package

| Metric | origin/main | HEAD | Δ |
|---|---|---|---|
| files under `src/luxe` | 93 | 107 | +14 |
| total lines | 32,765 | 33,272 | **+507** |
| non-comment, non-blank lines | 25,665 | 25,963 | +298 |
| AST statements (docstrings excluded) | 14,748 | 14,790 | **+42** |

**The plan's "net deletion ≈ −600 lines" was not achieved, and I believe the target
was wrong rather than the execution.** Two reasons, both deliberate:

1. **Design rationale was relocated, not deleted.** loop.py's ~250 lines of
   threshold-calibration comments (the v1.7→v1.10.5 derivations, the refuted-variant
   notes, the v1.10.5c feature-vector table) moved into `guardrails.py` with their
   constants rather than being dropped. That is why guardrails grew 766→982 while
   its AST stayed *identical*. Deleting that history to hit a line target would
   have been the wrong trade in a repo whose CLAUDE.md is largely made of such
   findings.
2. **Fourteen new modules each carry a docstring explaining why they exist** —
   which of two callers they serve, what was *not* merged into them, and which
   quirk they preserve. Those docstrings are most of the +298 non-comment delta's
   companion comment growth.

The +42 statement delta is the honest measure of "new code": the helper bodies
(`gitcmd`, `paths`, `flags`, `_toggle`, `_usage`, `iter_pruned*`) cost roughly
what the duplication they replaced returned. **This cycle bought structure —
fewer sources of truth, no cycles, three hot files 45% smaller — not deletion.**

### Suite

```
2353 passed, 2 skipped, 4 deselected in 66.12s (0:01:06)
2354/2358 tests collected (4 deselected)
```

Pre-cycle: 2,193 passed / 2 skipped / 4 deselected, 2,199 collected.
**+159 tests, zero lost** (§7.2 satisfied). New durable tests:

| File | Tests | Pins |
|---|---|---|
| `tests/test_guardrails_identity.py` | 32 | `loop.X is guardrails.X` per name; AST checks that loop defines none and guardrails defines all |
| `tests/test_layering.py` | 6 | no module-level `luxe.chat` import from below chat/ (cli declared as the one exception); `agents/` never imports chat/; gitkit has no function-local `chat.render`/`cli` import; the neutral modules stay neutral; modelstore specifically |
| `tests/test_run_flags.py` | 111 | every switch's default, on/off spelling, malformed-value fallback; threshold + tuple parsing incl. arity and out-of-band members; the loop is driven with `from_env` patched and must obey; AST guard that `run_agent`'s body contains no `os.environ` |
| `tests/test_fswalk.py` (+7) | 7 | `iter_pruned` vs a verbatim copy of the old loop, as a set **and in order**; the expected result spelled out independently; the precedence quirk pinned in both directions; `keep=()`; accept/size/unstattable paths |
| `tests/test_modelstore.py` (+5) | 5 | `resolve_pull_sources` behaves like the code it replaced; **both** pull surfaces reach it with identical arguments |

### Verifier checks I ran myself (§7)

| # | Check | Result |
|---|---|---|
| 1 | clean tree, 11 commits, no merges, message form | ✅ `git status` clean; 0 merge commits |
| 2 | full suite green; collected ≥ 2,126 | ✅ 2,353 passed; 2,358 collected |
| 3 | goldens untouched + green | ✅ empty diff; 5 passed |
| 4 | guardrails identity; `grep -c` → 0; four loop test files unchanged | ✅ all four |
| 5 | six dead names → 0 hits each | ✅ |
| 6 | layering test; modelstore/gitkit spot-greps → empty | ✅ |
| 7 | size targets | commands ✅ in range; loop 1.0% under; cli over — see §2.7 |
| 8 | `luxe <cmd> --help` byte-parity, `COLUMNS=200` | ✅ **24 surfaces identical** (20 commands + 4 subcommands + root) |
| 9 | parity/structure tests unmodified and green | ✅ `git diff origin/main` over `test_chat_commands.py`, `test_cli_ready.py`, `test_sdd.py`, `test_prompts.py`, `test_gitkit.py`, `test_turn_core.py` is **empty** — the only test files touched are the five that gained tests |
| 10 | behavioural spot-checks | ✅ `luxe outage` exit 0 and prints the card; `printf '/help\n/quit\n' \| luxe chat --repo <scratch>` exit 0 with the help table rendered; `luxe ready` → **READY (1s)**, exit 0 |
| 11 | honest report | this document |

### `.sdd` files updated

`agents/agents.sdd` (guardrails sole-home rule; `RunFlags` rule), `chat/chat.sdd`
(`launch.py`; the six `cmd_*` modules + `_toggle` + `_usage`; the `/pull` shared
resolver), `gitkit/gitkit.sdd` (neutral-module imports), `luxe.sdd` (layering
Must + the "no function-local cycle-dodging" Must-not; `gitcmd`; `luxe_home`;
`maintain.py`; `iter_pruned` + the precedence quirk). `tests/test_sdd.py` green
throughout.

---

## 5. Deferred candidates

Things I noticed and deliberately did **not** do.

1. **The `.github` precedence quirk** — `d not in excludes and not d.startswith(".") or d in keep`
   parses as `(not-excluded AND not-a-dotdir) OR is-a-keep-dir`, so `.github`
   survives even when the caller *also* lists it in `excludes`. Almost certainly
   not intended. Now in exactly one place (`fswalk.iter_pruned`) and pinned by a
   test that asserts the surprising behaviour in both directions, so fixing it is
   a one-line change plus one test edit — but it changes what five subsystems
   index, so it needs evidence, not a refactor.
2. **`run_agent`'s body** — 1,200 lines of one algorithm, explicitly out of scope.
   Its preamble is now clean (`RunFlags`) and its state is all explicit, which is
   the precondition for anyone who later wants to split it.
3. **The three `_LANGUAGE_EXTENSIONS`-family tables** — `symbols` (tree-sitter
   parsers only), `repo_index` (everything worth counting in an overview),
   `repo_index._LANG_BY_EXT` (only what the prompt registry has guidance for). Out
   of scope; all three now carry a comment naming the other two and saying what
   merging would break.
4. **`human_bytes` ×3** — see §2. If someone wants one ladder, it is a behaviour
   change to `chat.render`'s ≥1 GiB rendering and `planeproxy`'s negative-input
   handling, and it should be decided on those merits.
5. **`gitkit/brief.py` → `chat.project`** — a genuine gitkit→chat dependency for
   project resolution (`resolve_target`). `chat/project.py` is a good candidate
   for the neutral tier (nothing in it is interactive), which would let
   `test_layering` tighten to "gitkit imports nothing from chat".
6. **`chat.launch` → `cli` and `maintain` → `cli`** — both reach back for
   `_resolve_repo` / `_default_chat_config` / `_infer_task_type` via
   function-local imports. That is the pattern this cycle removed from gitkit, and
   it is used here only because `cli` legitimately sits *above* both. If it ever
   needs removing, `_resolve_repo` (repo-or-URL resolution) belongs next to
   `gitclone.py` and `_infer_task_type` belongs in `agents/`.
7. **`commands._HELP_ROWS` could generate the group-module split** — the parity
   test already proves handlers ≡ help rows; a future pass could derive
   `_build_handlers` from a per-module registry instead of one hand-written dict.
   Not attempted: the current dict is the thing the parity test reads, and a
   generated one is harder to audit during an outage.
8. **`_pull_show_state` / `_pull_list` and `_pull_show_search` / `_pull_search`** —
   still two implementations each. Their *output* differs (headers, a 15-hit cap
   in chat, the remote-endpoint branch in the CLI), so sharing them means
   designing one output, which is a product decision.
9. **The one raw `subprocess.run(["git", …])` family left** — `pr.py`,
   `citations.py`, `spec_validator.py`, `gitclone.py`, `repo_index.py`,
   `chat/smoke.py`. Out of scope by the plan; `gitcmd` exists if a future pass
   wants them.
