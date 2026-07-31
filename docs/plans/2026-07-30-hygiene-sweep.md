# luxe hygiene sweep — 2026-07-30

Executable plan for a full bugfix / refactor / optimization pass over luxe.
Written for an Opus 5 Claude Code session as executor. Scope decisions made
by the user 2026-07-30:

- **Tiered risk**: everything is scannable; bench-critical files get a
  stricter change tier (see § Tiers).
- **Change types**: bugfixes, behavior-preserving refactors, AND measured
  optimizations are all in scope.
- **Dogfooding**: `luxe gitaudit` is one scan source, and the smallest
  well-scoped fixes are executed through `luxe code` as an agentic drill,
  with the executor verifying each result.

Design principle (from the user): **the smaller the pieces, the more likely
luxe could fix its own problems.** Every work item must be small enough that
a 35B local model with a task card could plausibly execute it. If an item
can't be written that small, split it or mark it `L` (executor-only).

---

## Standing constraints (load before any edit)

Non-negotiable, from CLAUDE.md + the `.sdd` chain. Re-read the relevant
`.sdd` before touching any file under its subtree:

1. `src/luxe/luxe.sdd` — mono only; temp=0; no `Path.rglob`/`glob` on
   user-chosen roots (use `luxe.fswalk`); no `origin/<branch>` reads.
2. `src/luxe/agents/agents.sdd` — prompt registry is the single source of
   truth. Never inline prompt strings in `single.py`/`cli.py`.
3. `src/luxe/tools/tools.sdd` — honesty guards + Forbids enforcement order.
4. `benchmarks/maintain_suite/maintain_suite.sdd` — bench rules.
5. `src/luxe/chat/chat.sdd`, `compare/compare.sdd`, `gitkit/gitkit.sdd`,
   `memory/memory.sdd` — before editing those packages.
6. Single-champion policy: no model fan-out beyond the sanctioned chat
   slots / per-host manifests. Do not "restore" the champion as the m1/m4
   interactive default.
7. Git: **rebase, never merge**; linear history on origin/main. One logical
   change per commit.
8. Benchmark/maintain path must stay byte-identical unless a work item
   explicitly targets it AND passes the Tier A ladder (below).
9. `src/luxe/memory/` must never read `~/.claude/` or repo `CLAUDE.md`.
10. Long-running runs (full bench, n≥14 sweeps): **hand the command to the
    user, do not auto-run** (standing user preference).

## Tiers

| Tier | Files | Rules |
|------|-------|-------|
| **A** | `agents/loop.py`, `agents/single.py`, `agents/prompts.py`, `backend.py`, `agents/guardrails.py`, `agents/convergence.py`, `benchmarks/**`, `tools/` fns reachable from the bench path | Behavior-preserving only. Golden-request byte-identity gate (Phase 0.5) mandatory per change. Full suite per change. Any semantic change → STOP, present to user with an offered bench command. |
| **B** | `cli.py`, `config.py`, `tools/` (chat-reachable), `mcp/`, `fswalk.py`, `search.py`, `symbols.py`, shared plumbing | Full suite per micro-batch; targeted tests per change; behavior changes allowed only with a regression test proving old vs new. |
| **C** | `chat/`, `compare/`, `gitkit/`, `memory/`, `modelstore.py`, `scripts/`, docs | Normal rules: targeted tests per change, full suite per micro-batch. Primary pool for `luxe code` drill items. |

When a file is ambiguous, treat it as the higher tier.

## Work-item card schema

Every finding that survives triage becomes one card in
`acceptance/hygiene_2026_07/queue.md`:

```
### HS-<nnn> <one-line title>
- file: <path>:<line>
- tier: A|B|C   type: bug|refactor|opt   size: S|M|L
- source: ruff|mypy|bandit|pip-audit|gitaudit|grep-invariant|manual|test-health
- evidence: <verbatim scanner output or repro>
- fix: <2-5 lines, concrete — what to change, not "improve">
- test: <the specific test to add/extend, or the existing test that pins it>
- verify: <exact commands + expected outcome>
- executor: opus|luxe-code
```

Size rubric: **S** = one file, ≤ ~25 changed lines, no signature changes
(luxe-code eligible if tier C). **M** = one module, may touch its tests.
**L** = cross-module or tier A (executor-only, split if possible).

---

## Phase 0 — Baseline capture (no mutations)

Goal: a recorded "known good" to diff every later step against.

- **0.1 Constraint load-in.** Read CLAUDE.md, all seven `.sdd` files,
  `RESUME.md` current-state header, `lessons.md` table of contents.
  *Verify:* write a 10-line constraints digest into
  `acceptance/hygiene_2026_07/NOTES.md`.
- **0.2 Env.** `uv sync --extra dev --extra chat --extra analyzers`.
  *Verify:* `ruff --version`, `mypy --version`, `bandit --version`,
  `pip-audit --version` all resolve from `.venv/bin`. (As of 2026-07-30 the
  analyzers extra is NOT installed in the venv — expect this to change it.)
- **0.3 Test baseline.** `uv run pytest -q` (1883 tests collected as of
  2026-07-30). Save full output to `acceptance/hygiene_2026_07/baseline_pytest.txt`.
  *Verify:* 0 failures, or every failure documented as pre-existing in
  NOTES.md before proceeding. Also record wall time and the 10 slowest
  tests (`--durations=10`).
- **0.4 Analyzer baselines.** Run each, save raw output under
  `acceptance/hygiene_2026_07/baseline_<tool>.txt`:
  - `ruff check src tests benchmarks scripts --output-format=concise`
  - `mypy src/luxe --ignore-missing-imports` (record error count; do NOT
    chase to zero — the baseline is the reference)
  - `bandit -r src/luxe -ll`
  - `pip-audit` (advisory only; dependency bumps are their own cards)
  *Verify:* files exist and are non-empty (or explicitly note "clean").
- **0.5 Byte-identity guard (the key new safety net).** Add
  `tests/test_golden_request.py`: with a mocked backend, call `run_single`
  on a small fixed fixture (default `extra_context=""`, `on_token=None`)
  and snapshot the **exact** request payload(s) — messages, tools array,
  sampling params — to `tests/golden/run_single_request.json`. Same for a
  representative `prompts.py` registry render. Assert byte-equality.
  *Verify:* test passes twice in a row (deterministic); intentionally
  perturb a prompt string locally, confirm the test FAILS, revert.
  This test is the per-change gate for every Tier A edit.
  Commit Phase 0.5 as its own commit before any fix lands.
- **0.6 Smoke.** If oMLX is up: `luxe smoke` (minutes). Record exit code.
  If the host/model is unavailable, note it and continue — smoke reruns at
  close-out. *Verify:* exit 0, or a NOTES.md entry explaining why not.

**Gate to Phase 1:** baseline files committed (one commit,
`chore(hygiene): phase 0 baselines + golden-request guard`).

## Phase 1 — Scan (read-only; parallelize freely)

Each scan appends candidate findings to a raw ledger
`acceptance/hygiene_2026_07/raw_findings.md`. No fixes in this phase.

- **1.1 ruff** — full rule diff vs baseline is the finding set; also run
  `ruff check --select ALL --statistics` once for a rule-class census
  (census informs triage; only defensible classes become cards).
- **1.2 mypy** — every error in `src/luxe` is a candidate card. Group by
  file; type-stub noise is triaged out, real Optional/None bugs in.
- **1.3 bandit + pip-audit** — security candidates. Anything touching
  subprocess/shell in `tools/shell.py`, `gitkit/apply.py`, `modelstore.py`
  gets a manual read regardless of scanner verdict.
- **1.4 Grep invariant audit** (the project's own rules, mechanically):
  - `rglob\(|\.glob\(` outside tests → luxe.sdd violation candidates
  - `subprocess.(run|Popen|check_)` without `timeout` → wall-cap candidates
    (lesson: user-controlled commands deadlocked pytest before)
  - `except Exception:`/bare `except:` swallowing without log
  - inline prompt-looking strings (`"""You are` etc.) outside `prompts.py`
  - `Path.home()`/`~/.claude` references inside `src/luxe/memory/`
  - `open(` without encoding in cross-platform paths
  - TODO/FIXME/XXX inventory
- **1.5 Dogfood: `luxe gitaudit`.** Requires oMLX + champion. Run
  `luxe gitaudit --json` (repo footprint → deep map-reduce; incremental
  cache applies). This is long — launch in background at Phase 1 start,
  collect whenever it finishes; do not block other scans on it. Findings
  merge into the ledger tagged `source: gitaudit`. Treat them as *leads*,
  not truth: each must be independently confirmed by the executor reading
  the code before it becomes a card. (Also note gitaudit's own misses/false
  positives in NOTES.md — that's free product feedback for gitkit.)
- **1.6 Test-suite health.**
  - skipped/xfail inventory (`pytest -q -rs`), each skip justified or carded
  - run the suite twice; any test passing once and failing once → flake card
  - `--durations=25`: tests > 5s get an optimization candidate
  - coverage snapshot (`pytest --cov=luxe --cov-report=term-missing`,
    optional if pytest-cov absent — add to dev extra as a card, not ad hoc):
    modules < 50% coverage listed as test-gap cards, capped at the 10 worst
- **1.7 Manual sweep of the four largest surfaces** — `cli.py` (1978 lines),
  `agents/loop.py` (1714), `chat/commands.py` (1428), `chat/repl.py` (1265).
  30–60 min each, looking for: dead branches, duplicated helpers,
  argument-parsing drift, error paths that print but return success, and
  split-file opportunities (refactor cards, tier-scoped).
- **1.8 Seed findings** (already observed while writing this plan — verify,
  then card them):
  - `.github/workflows/luxe-tests.yml` filters on paths `luxe/**` with
    `working-directory: luxe`, but the repo root IS luxe and has no `luxe/`
    subdirectory — **CI has likely never triggered on this repo**. Fix
    paths + working-directory; verify with a `gh run list` after next push.
  - Analyzers extra defined in pyproject but not installed → `luxe`'s own
    ruff-backed analysis tool (`tools/analysis.py`) may be silently
    degraded locally (the pyproject comment about "silently skipped"
    suggests this bit before). Consider a `/doctor` check.
  - CI runs `pytest -x` (stops at first failure) — full-suite visibility
    card.

**Gate to Phase 2:** ledger complete; gitaudit finished or explicitly
abandoned with reason; ledger committed.

## Phase 2 — Triage (no mutations)

- **2.1** Dedup ledger across sources; mark false positives with a
  one-line reason (kept in the ledger — negative results are data).
- **2.2** Convert survivors to cards (schema above) in `queue.md`. Every
  card gets tier, type, size, executor. Splitting rule: if `fix:` needs
  more than 5 lines to describe, split the card.
- **2.3** Order the queue: bugs → refactors → optimizations; within each,
  C before B before A (build confidence on low-risk ground first).
  Cap Tier A cards at what genuinely matters — Tier A churn is risk.
- **2.4 USER CHECKPOINT.** Present: counts by tier/type/size, the full
  Tier A list, the proposed `luxe code` drill subset, and anything
  triaged out that a reasonable person might disagree with. **Do not
  proceed to Phase 3 without user ack.**

## Phase 3 — Execute (micro-batches)

The per-card loop — no card skips a step:

1. Read the card's file + its `.sdd`. Confirm the finding still reproduces.
2. **Test first** for bugs: write/extend the regression test, watch it fail.
   For refactors: confirm existing tests pin current behavior; if nothing
   pins it, write the pin test BEFORE refactoring.
3. Apply the fix.
4. Targeted: `uv run pytest <card's test file> -q` → green.
5. Delta lint: `ruff check <touched files>`; `mypy <touched files>` —
   no NEW issues vs baseline.
6. Tier A only: `uv run pytest tests/test_golden_request.py tests/test_prompts.py -q`
   → byte-identical. Any golden diff → revert immediately, re-scope the card.
7. Commit: `fix|refactor|perf(<area>): <title> [HS-nnn]`, one card per
   commit. Never batch cards into one commit — small pieces is the point,
   and `git revert` granularity is the rollback story.
8. Mark the card done in `queue.md` with the commit sha.

**Micro-batch cadence:** after every **5 cards** (or any single Tier A/B
card): full `uv run pytest -q` + full ruff vs baseline. Regression →
bisect within the batch (5 commits max to check), revert the offender,
reopen its card with the failure attached.

**`luxe code` drill protocol** (cards marked `executor: luxe-code` —
tier C, size S only):

- a. `git worktree add /tmp/luxe-drill-HS-nnn main` — luxe never writes to
  the live checkout.
- b. Compose the task card as a prompt file: goal, exact file, the failing
  test command (`pytest tests/<file> -q`), and "do not touch other files".
- c. Run headless: `printf '<task>\n/quit\n' | luxe code --repo /tmp/luxe-drill-HS-nnn`
  (write tools on from turn one; bash gated — include the test command in
  the task so the agent runs it via the gated path, or run `--dev` if the
  card needs pytest execution).
- d. Executor verifies in the worktree: `git diff` review + targeted test +
  full suite. Pass → `git diff | git apply` onto main (or cherry-pick),
  then the normal steps 4–8. Fail → record the transcript path
  (`~/.luxe/sessions/<id>/`) + failure mode in
  `acceptance/hygiene_2026_07/drill_log.md`, then Opus fixes it directly.
- e. `git worktree remove` after each card.
- The drill log (attempt count, success rate, failure taxonomy) is a
  first-class deliverable — it measures the fallback kit's real repair
  capability, which is luxe's mission.

**Stop-and-ask triggers:** any golden-request diff; any fix requiring an
`.sdd` contract change; any dependency version bump; any behavior change a
user might notice in `luxe chat`; queue item that grew beyond its size class.

## Phase 4 — Optimizations (measure → change → measure)

Only after bugs + refactors land. Every opt card needs numbers on both
sides; no measurement, no merge.

- **4.1 Fixed measurement harness first**: a small script
  (`scripts/hygiene_perf.py`) timing (a) `luxe chat --repo .` cold start to
  first prompt, (b) index build on this repo, (c) `import luxe.cli` time,
  (d) full pytest wall. 3 runs each, report median. Commit the script; it
  runs before and after every opt card.
- **4.2 Candidates** (from Phase 1.6 durations + profiling, plus known
  suspects): slow tests > 5s (mock instead of sleep/subprocess where the
  sleep isn't the thing under test), `cli.py` import-time weight
  (lazy-import heavy deps behind subcommands), repeated config/YAML parsing,
  chat startup path. Profile with `python -X importtime` / `cProfile`
  before proposing a fix — no speculative optimization.
- **4.3 Keep rule**: ≥10% median improvement on the relevant metric and
  zero test regressions, else revert. Record before/after in the card.
- **4.4** Bench-path (Tier A) perf ideas are NOT executed — they're written
  up as proposals with the offered bench command
  (`python -m benchmarks.maintain_suite.run --variants <yaml>`) for the
  user to run.

## Phase 5 — Close-out

- **5.1** Full `uv run pytest -q` — green, count ≥ baseline (new tests
  should have raised it; record the new number).
- **5.2** Full analyzer runs diffed against Phase 0 baselines — every delta
  is either an intentional fix or explained.
- **5.3** `luxe smoke` and, with oMLX up, `luxe smoke --chat --code` —
  exit 0. This is the "did we break the fallback kit" gate.
- **5.4** Interactive spot-check script (5 min, by hand or headless REPL):
  `luxe chat --repo .` → one freeform turn, `/status`, `/doctor`, `/model`,
  `/quit`. No tracebacks, doctor warnings unchanged-or-better.
- **5.5** If ANY Tier A file changed: present the user the bench command
  for a validation run (do not run it): 
  `python -m benchmarks.maintain_suite.run --variants configs/single_64gb.yaml --work-dir ~/.luxe/bench-workspace`.
- **5.6** Write the final report `acceptance/hygiene_2026_07/REPORT.md`:
  cards fixed / deferred / false-positive by tier+type, drill success rate,
  perf deltas, new-test count, and the deferred list as seed for the next
  sweep. Add a RESUME.md handoff entry and any genuine surprises to
  `lessons.md`. Final commit + offer to push.

---

## Execution notes for the operator

- Phases 0–2 are safe to run unattended end-to-end (read-only apart from
  the guard test + ledger commits). Phase 3 starts only after the 2.4
  checkpoint.
- Everything lives under `acceptance/hygiene_2026_07/` so the sweep is
  auditable and resumable; if the session dies, the queue file is the
  restart point (cards are idempotent: reproduce-first means a half-done
  card just re-runs).
- Expected effort: Phase 0 ≈ 1h, Phase 1 ≈ 3–5h (gitaudit in background),
  Phase 2 ≈ 1–2h, Phase 3 dominated by queue size (budget ~15–30 min/card),
  Phase 4 ≈ 2–4h, Phase 5 ≈ 1h.
