# Hand-verification — gemma4_r1_2026_08_19

**Subject:** `mono__gemma-4-26b-a4b-it-6bit` (printed 9/10, wall 6.9 min)
**Control:** `mono__qwen3.6-27b-6bit-ref` (printed 10/10, wall 32.6 min)
**Method:** every diff read by hand from the fixture-cache branch (or the clone's
reflog commit where the branch was never created), judged against the fixture's
goal text — not against either grader.
**Verified from:** m5 `~/Downloads/luxe/acceptance/gemma4_r1_2026_08_19/`, read-only.

---

## VERDICT

**Of the 9 MoE passes: 5 REAL, 3 THIN, 1 VACUOUS. Three of the nine leave the
repository in a worse state than base_sha.**

The speed is **real, not an artifact** — the MoE completed *more* agent steps
(161 vs 112) and prefilled *25% more* prompt tokens than the reference, and was
still 4.7× faster in wall. About 2.8× of that is genuine decode throughput
(46.4 vs 16.6 tok/s wall, the A3B active-parameter win) and about 1.7× is
producing 41% fewer output tokens. It did not pass by skipping steps.

But it passed by **skipping verification and skipping care**. Across all 10
fixtures the MoE issued **zero** `lint`, `typecheck`, `git_diff` or `glob`
calls; the reference issued 19. The consequences are visible in the diffs: two
destructive rewrites nobody caught, one silent functional regression, and one
file whose new content is a verbatim line-numbered echo of `read_file`'s own
output.

**Recommendation: REFUTED. Do not spend a 3-replicate promotion-bar matrix on
this tonight.** Confidence: **high** — every classification below rests on diff
text, not on a grader signal.

---

## The single most important finding

`neon-rain-document-modules` — MoE, PASSED, score 4/5, +132/−132:

```
-# Architecture
-
-## Overview
+1	# Architecture
+2	
+3	## Overview
```

It rewrote the entire `ARCHITECTURE.md` by prefixing **every line with a line
number and a tab** — i.e. it wrote back the `read_file` tool's own rendering as
if it were file content. It added **no** Subdirectories section, **no**
per-directory descriptions, **nothing**; it also stripped the trailing newline.
The task asked it to *add a section and preserve existing content*. It deleted
the document and replaced it with a corrupted copy.

It scored 4/5 because the legacy regex found `actions|phases|render|state|input`
in 22 "added" lines — which are the *original file's own lines*, re-added.
`spec_validation` R2 ("Document all 9 src/ subdirectories") was likewise marked
**satisfied** off those same copied lines. Only R1 (the heading) caught it, and
R1 does not drive PASS/FAIL.

This is the exact 2026-05-02 `overnight_moe` failure mode: content copied into
the diff to clear a `min_added_lines` floor. It is also a **tool-output
contamination** bug, which for a fallback dev tool is disqualifying on its own.

---

## Per-fixture table

`sp` = `spec_all_satisfied`. Per the coordinator's calibration it is reported
for completeness only — it was `true` for the vacuous strict-flag arms elsewhere
and `true` for 8 of the 9 MoE passes here, including two destructive ones.

| Fixture | MoE pass/score | MoE +/− | MoE sp | MoE wall | Ref pass/score | Ref +/− | Ref sp | Ref wall | Class |
|---|---|---|---|---|---|---|---|---|---|
| nothing-ever-happens-document-config | ✓ 4 | +87/−0 | T | 74s | ✓ 4 | +120/−0 | T | 490s | **REAL (beats ref)** |
| isomer-document-quickstart | ✓ 4 | +22/−12 | T | 21s | ✓ 4 | +12/−3 | T | 119s | REAL |
| isomer-implement-healthcheck | ✓ 4 | +5/−0 | T | 15s | ✓ 4 | +9/−0 | T | 58s | REAL |
| lpe-rope-calc-document-typing | ✓ 4 | +4/−4 | T | 37s | ✓ 4 | +2/−2 | T | 116s | REAL |
| the-game-implement-shuffle-shortcut | ✓ 4 | +18/−2 | T | 27s | ✓ 4 | +11/−0 | T | 130s | REAL (scope creep) |
| lpe-rope-calc-implement-strict-flag | ✓ 4 | +35/−10 | T | 94s | ✓ 4 | +21/−7 | T | 252s | **REAL but DAMAGING** |
| nothing-ever-happens-manage-deps-audit | ✓ 4 | +28/−0 | T | 22s | ✓ 4 | +117/−0 | T | 269s | **THIN** |
| the-game-document-architecture | ✓ 4 | +9/−9 | T | 13s | ✓ 4 | +15/−0 | T | 74s | **THIN + DESTRUCTIVE** |
| neon-rain-document-modules | ✓ 4 | +132/−132 | **F** | 39s | ✓ 4 | +12/−0 | T | 125s | **VACUOUS + DESTRUCTIVE** |
| neon-rain-implement-reset-shortcut | ✗ 1 | +0/−0 | T | 74s | ✓ 4 | +12/−0 | T | 320s | MoE FAIL |

`gates_triggered` was **empty for all 20 runs**. The `destructive_diff` strict
gate did not fire on either of the two destructive MoE rewrites.

**Bailouts — MoE 4/10, reference 0/10:** `context_overflow` (max steps 30) on
lpe-document-typing, lpe-strict-flag and neon-rain-reset; `stuck_no_output` on
the-game-shuffle. Three of those four still counted as passes.

---

## Evidence per classification

### REAL — and better than the reference

**`nothing-ever-happens-document-config`** (74s vs 490s — the cell flagged as
most suspicious). The MoE **wins this outright.**

- MoE documents **58 distinct env vars**; the reference documents **27**.
- The repo has exactly **23 `PM_NH_*` strategy-override vars** read through
  `_env_int`/`_env_float`/`_env_bool` in `bot/config.py`. The MoE documents all
  23 with defaults and line refs. **The reference omits the entire block** —
  against a goal that says "documents *every* environment variable the bot
  reads at startup".
- Its cross-references are exact, spot-checked against base_sha:
  `bot/main.py:122` = `LOG_LEVEL`, `:128` = `DATABASE_URL`,
  `:158` = `PM_BACKGROUND_EXECUTOR_WORKERS`, `:193` = `PORT`/`DASHBOARD_PORT`;
  `bot/risk_controls.py:42/45/48` all correct. No hallucinated citations found.
- It reached this with `read_file:11 grep:2`; the reference burned
  `read_file:41 grep:11` and 8,519 completion tokens to produce less coverage.

This is not a doc that lists names without meaning — every row carries a
default, a one-sentence description, and a file:line. **I would merge the MoE's
CONFIG.md over the reference's.**

### REAL — peer-quality

- **`isomer-document-quickstart`**: restructures Quick Start into numbered
  steps, keeps the `ISOMER_SECRET` generation intact, adds port 27001 and the
  dashboard URL plus a loopback-binding note. The 12 deletions are comments
  converted to prose, not stripped content. Peer of the reference.
- **`isomer-implement-healthcheck`**: `@app.route("/health")` →
  `jsonify({"status": "ok"}), 200`. Functionally identical to the reference
  (which spends 4 extra lines on a section banner). `jsonify` already imported.
- **`lpe-rope-calc-document-typing`**: `f: IO` on `_read_gguf_value` *plus*
  `dict` → `dict[str, Any]` on two other signatures. `IO` is less precise than
  the reference's `BinaryIO`, but it is a real type, not the degenerate `Any`
  the 31b arm emitted — and the MoE typed two signatures the reference did not.
- **`the-game-implement-shuffle-shortcut`**: correct `keydown` listener with a
  better input guard than the reference (`isContentEditable` + case-insensitive
  key), correct cleanup. Caveat: it also made **two unrequested edits**
  (`className="library-title"` / `"studio-title"` on `<h2>` elements) unrelated
  to the goal, and the run ended in a `stuck_no_output` bailout.

### REAL but DAMAGING — would not merge

**`lpe-rope-calc-implement-strict-flag`.** `--strict` is registered and gates
the exit code, so the goal is met. But the MoE rewrote `scan_ollama` and
`scan_gguf_dir` to return tuples (an API break) and, at the call site,
substituted:

```
-            records += scan_lmstudio(root)
+            recs, err = scan_gguf_dir(root, "lmstudio", strict=args.strict)
```

`scan_lmstudio` does more than `scan_gguf_dir` — after the GGUF pass it walks
for `config.json` + `*.safetensors|*.bin|*.npz` to pick up MLX/HF-format model
dirs. **That entire branch is now dead: LM Studio MLX models silently stop
being scanned.** The reference threaded an optional `failures: list[str]`
through instead, preserved every signature and `scan_lmstudio` itself, and
prints the offending files before returning 1. The reference's diff is smaller,
non-breaking, and more useful. This is over-eager refactoring with no
verification pass behind it — consistent with 0 `typecheck`/`lint` calls
(the reference made 4 on this fixture alone).

### THIN

**`nothing-ever-happens-manage-deps-audit`** (22s vs 269s), 28 lines vs 117.
Two real findings (python-dotenv, web3). The third is self-nullifying:

> *"Note: The SQLAlchemy finding is included to demonstrate audit process,
> though the current version constraint in requirements.txt already mitigates
> the specific CVEs found."*

That is padding to reach the `min_matches: 3` floor, stated out loud. It also
**misses aiohttp entirely** — the reference found `CVE-2024-23334` plus three
further aiohttp advisories, listed the clean dependencies it checked, and
supplied a concrete `requirements.txt` diff block. Same tool (`cve_lookup`, 6
calls vs 7); the MoE simply stopped at three rows.

**`the-game-document-architecture`** (13s — "fast enough to be suspicious on
its face"). It did **not** earn it. The prose is accurate — it names
`src/App.jsx`, `server.js`, `/api/shuffle` and `selectWithNeighbors` — but it
obtained the room for it by **deleting the `## Features` section**:

```
-## Features
-- 5-second splash animation on first load
-- Play Again button to reshuffle without reloading
   … (7 bullets removed)
+## How It Works
```

Section headings before: `What It Does · Quick Start · Configuration · Studio
Lists · **Features** · API Endpoints · Previous Versions`. After: the same list
with **Features** replaced by **How It Works**. Net documentation value ≈ 0; the
goal said *add* a section. The reference inserted 15 lines after the intro,
deleted nothing, and gave more mechanism detail (line numbers for
`fetchPlexLibrary`, `selectWithNeighbors`, the neighbor-window logic, the
`tmdbCache`). Reference is clearly better.

### VACUOUS

`neon-rain-document-modules` — see the top section. For contrast, the
reference's 12 added lines are a genuine `## Subdirectories` section naming all
9 directories with real per-directory prose, including files that are **not** in
the ARCHITECTURE.md tree listing (`TextRenderer`, `CooldownTracker`,
`IOBuffer`, `ModalManager`, `ViewportScaler`) — it actually walked the tree
(`list_dir:10`). The MoE issued `list_dir:1, read_file:2` and never looked at
`src/`.

### The one MoE failure

`neon-rain-implement-reset-shortcut`: 30 tool calls — **26 `read_file`, 3
`list_dir`, 1 `grep`, zero writes** — until the step cap, then
`context_overflow`. 74 seconds and 315k prompt tokens spent reading, nothing
produced. The reference implemented it in the way the goal specified (a
`game:reset` event on the EventBus wired through the existing
`src/input/HtmlInputHandler`), 2 files, +12.

---

## Task 4 — where the speed came from

| | MoE | Reference | Ratio |
|---|---|---|---|
| Total wall | 414.9 s (6.92 min) | 1,954.3 s (32.57 min) | **4.71×** |
| Decode throughput (`gen_tps_wall`) | 46.37 tok/s | 16.64 tok/s | **2.79×** |
| Completion tokens (sum) | 19,243 | 32,527 | 0.59× |
| Prompt tokens (sum) | 1,460,409 | 1,169,056 | **1.25×** |
| Tool calls (sum) | 163 | 185 | 0.88× |
| Completed steps (`tool_step_done`) | **161** | 112 | **1.44×** |
| Mean steps / fixture | 16.1 | 11.2 | 1.44× |
| Mean additions / fixture | 34.0 | 33.1 | 1.03× |
| …excluding neon-rain-document-modules | **23.1** | 35.4 | 0.65× |
| Verification calls (lint+typecheck+git_diff+glob) | **0** | 19 | — |
| Bailouts | 4 | 0 | — |

Decomposition: 4.71× ≈ **2.79× real throughput × 1.69× less output generated.**

So the honest answer to "did it do the work faster, or do less work?" is
**both, and the split is measurable**: the throughput half is a genuine
architectural win, the output half is 35% less real content (once the 132 lines
of copied text are excluded from its credit). It is emphatically *not* taking
fewer steps — it takes 44% more, and hits the 30-step cap on 3 of 10 fixtures.

The qualitative gap is the verification column. The reference `lint`s,
`typecheck`s and `git_diff`s its own work; the MoE never did, on any fixture.
Every one of the three damaged repos would have been caught by a single
`git_diff` review step.

---

## Methodology note — a trap in the stated recipe

The brief's recovery recipe silently produced a **false byte-identical result**
on `nothing-ever-happens-manage-deps-audit`. Both arms' `pr_state.json` name the
branch `luxe/manage/audit-requirements-txt-identify-any-pinned-5`, but only the
MoE created it. The reference's agent had already `git commit`ed its own work
via bash, so `pr.py`'s commit step failed with
`no diff produced (failed_no_mutations_produced)` and never created a branch —
while `grade_fixture` (`benchmarks/maintain_suite/grade.py:699`) reads
`git diff base HEAD` in the *clone*, which correctly saw the agent's own commit.
Diffing the named branch therefore compares the MoE's output against itself.

The reference's true diff is the dangling clone commit `bc29b07` (117
insertions), reachable only via
`~/.luxe/bench-workspace/nothing-ever-happens-manage-deps-audit-clone` reflog.
**Always cross-check `pr_state.json`'s commit step status before trusting
`branch_name`** — the MoE arm has the same condition on
`neon-rain-implement-reset-shortcut` (which genuinely produced nothing).

---

## Recommendation

**REFUTED. Do not run the 3-replicate promotion-bar matrix tonight.**

1. **A 3-rep would not measure what needs measuring.** The binary PASS is
   already known to be loose, and this run shows `spec_validation` is loose in
   the same direction — R2 credited "documents all 9 subdirectories" against a
   file the model had just destroyed. More replicates of a grader that cannot
   see destruction yields a more precise hollow number.
2. **The failure modes are disqualifying for the stated mission.** luxe is the
   *fallback dev tool*: availability over capability, and it has to work when
   reached for. A model that (a) writes `read_file`'s line-numbered output into
   a tracked file, (b) deletes a README section to make room for its own, and
   (c) drops a code path while refactoring — all without one verification call —
   fails "works when reached for" three separate ways in ten fixtures. Add 4/10
   bailouts against the reference's 0.
3. **The reference is undamaged at 10/10.** There is no cell where the MoE is
   both faster *and* better except `document-config`, and that one is a real,
   genuine, notable win that deserves to be recorded on its own terms.

**What is worth doing instead** (cheap, hours not a night):

- Reproduce the **tool-output echo** (`neon-rain-document-modules`) 3×. If it is
  reproducible it is a substrate finding, not a model finding — worth a
  `write_file` guard that rejects content matching the `^\d+\t` read_file
  rendering, which would protect *every* model including the champion.
- Close the **`destructive_diff` gate gap**: a 132/132 replace-in-place and a
  9/9 section swap both passed the gate. A ratio-based gate cannot see either.
- Keep the `document-config` result. Whatever this MoE does on
  grep-and-enumerate documentation tasks, it does better and 6.6× faster than
  the champion, and that is a genuine, reportable finding independent of the
  promotion question.

**Confidence: high.** Every call above is grounded in diff text I read, with the
decisive lines quoted. The two graders agree with each other and are wrong
together on 3 of 9; that is the headline, and it reproduces the 2026-05-02
lesson exactly.
