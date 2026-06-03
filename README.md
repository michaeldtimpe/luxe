# luxe

MLX-only repo maintainer for Apple Silicon. Takes any of your repos and adds
features, fixes bugs, updates docs, or audits maintenance — and opens a PR.

> **Status:** v1.8.0 shipped (2026-05-13). The cycle migrates control
> logic from prompts into the runtime: Track 2's pre-dispatch spec gate
> converts `expects_zero_calls` from policy-scored to capability-gated.
> BFCL n=1240 agent: irrelevance 100% (240/240, +9.58pp), total
> 90.24% (+1.85pp). SWE-bench n=75 wash with v1.7 (empty_patch ≤13 floor
> missed at 17; deferred to v1.9 — needs action_density gating). Track 5
> taxonomy (`src/luxe/agents/outcomes.py`) is the observability primitive
> for future mechanism-level comparisons. 712 tests. v1.6.1 (substrate
> hardening + SpecDD Lever 2 in maintain_suite + BFCL anchors) was the
> previous shipped tag. See `RESUME.md` for active state.
>
> **Extended benchmark suite (2026-05-28):** MMLU / ARC-Challenge /
> GSM8K / CodeNeedle / Perplexity added as a broad-capability layer on
> top of the agentic suite. Implementation in
> `benchmarks/{gsm8k,codeneedle,mmlu,arc_challenge,perplexity}/`; shared
> utilities in `benchmarks/_eval_common/`; suite runner at
> `scripts/run_eval_suite.sh`. See `benchmarks/EXTENDED_BENCH.md` for
> design + usage. 102 offline tests added; existing agentic-suite code
> paths unchanged.

## What luxe does

```
luxe maintain <repo> "<goal>"
  ↓
single capable model + full tool surface (read / write / shell / git / search)
  ↓
agentic loop bounded by max_steps; .sdd contracts enforced tool-side
  ↓
diff-aware citation lint (zero unresolved fabrications)
  ↓
git checkout -b → commit → tests → push → gh pr create
```

Mono-only execution. Earlier swarm/micro/phased modes were retired (see
`src/luxe/luxe.sdd`). The capable monolith runs the whole task; the SpecDD
`.sdd` chain is what scales the system to large repos and constrains
behavior, not multi-agent orchestration.

## SpecDD `.sdd` chain

Every directory of consequence carries a `<dir>/<dir>.sdd` contract listing
**Must / Must not / Owns / Forbids / Forbids creating**. The chain is
walked by `find_all_sdd` at task start, surfaced into the prompt, and
enforced by the `_write_file` / `_edit_file` tools at write time.

- `src/luxe/luxe.sdd` — root invariants (mono-only, temp=0, pinned `--work-dir`,
  no MoE Instruct-2507, no `origin/<branch>` reads on offline cache)
- `src/luxe/agents/agents.sdd` — prompt registry is the single source of truth
- `src/luxe/tools/tools.sdd` — honesty guards, Forbids enforcement order
- `benchmarks/maintain_suite/maintain_suite.sdd` — bench rules
  (`vacuous_test` gates, `--keep-loaded`, sidecar regrade)

`Forbids creating` (v1.6) fires only when a write would create a new file —
v1.5 broad-glob path-aware semantics gave way to operation-aware semantics
so legitimate edits to existing files aren't caught by scaffolding-name
patterns. See `RESUME.md §Architectural reframe` for the full rationale.

## Why MLX-only

Earlier multi-backend versions (Ollama / llama.cpp / LM Studio / oMLX / MLX)
produced repeated real failures: silent context truncation, fabricated
citations, model-loop bugs. luxe ships oMLX-only — every other moving part
is one less thing that can lie about token budgets.

## Install

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Requires:
- Python 3.11+
- A running [oMLX](https://github.com/nicholasgasior/omlx) server on
  `localhost:8000`. The brew launchd unit's `KeepAlive` is recommended
  (oMLX/Metal has occasional `gpu::check_error` crashes; auto-restart hides them).
- `gh` CLI authenticated (`gh auth login`) for the PR cycle
- Apple Silicon with ≥64 GB unified memory

## CLI

```
luxe maintain <repo> "<goal>" [--task review|implement|bugfix|document|summarize|manage]
                              [--config <path>] [--allow-dirty] [--yes]
                              [--watch-ci] [--keep-loaded]
                              [--spec-yaml <path>] [--save-report]
luxe chat   [--repo <path>] [--config <path>]   # interactive Claude-CLI-style agent
            [--chat-model/--plan-model/--code-model <id>] [--resume <id>]
luxe gitsummary  <repo> [--no-save]             # project / deps / health / use-risk verdict
luxe gitreview   <repo> [--no-save]             # serious bugs + security findings (read-only)
luxe gitrefactor <repo> [--no-save]             # ordered structural refactor plan (read-only)
luxe pr     <run-id> [--push-only]              # resume a partially-completed PR cycle
luxe runs   list | luxe runs gc                 # housekeeping
luxe unload [--except <model-id>]               # free oMLX RAM (auto-runs after maintain)
luxe serve  [--transport stdio|sse] [--unsafe]  # MCP server (read-only by default)
luxe check                                      # oMLX + models + gh auth
```

Examples:

```bash
# Default — single capable model, agentic loop
luxe maintain ~/code/my-app "fix the off-by-one in pagination"

# Read-only review (no PR)
luxe maintain ~/code/my-app "review the auth module for security bugs" --task review

# Resume just the PR cycle (commit / push / create / watch_ci) after auth expired
luxe pr <run-id>
```

### GitKit — read-only repo analysis

Point luxe at any existing repo (a local path or a git URL it clones) and get a
packaged markdown report. All three are **read-only** and **single-pass** (one
agent run, no edits, no PR). The **full report is always saved** to
`~/.luxe/reports/<repo-hash>/<kind>-<ts>-<rand>.md`; on screen you get a short
preview + the saved path (pass `--verbose`/`-v`, or `/verbose full` in chat, to
print the whole report). A live spinner shows progress while the model works.

| Command | Aliases | What it produces |
|---|---|---|
| `luxe gitsummary`  | `git-summary`, `gsum` | purpose, stack, dependencies & their risk, size/health (commit cadence, contributors, PR/issue/release activity), security posture, **use-risk verdict** (low/med/high) |
| `luxe gitreview`   | `git-review`, `grev`  | serious bugs & security concerns, each with severity + `file:line` evidence (ungrounded/speculative findings are omitted) |
| `luxe gitrefactor` | `git-refactor`, `gref`| an ordered structural refactor plan (coupling, cohesion, boundaries, duplication, dead code, testability) |

```bash
luxe gsum  ~/code/my-app                       # summarize a local repo
luxe grev  https://github.com/acme/widget      # clone + review a remote repo
luxe gref  . --verbose                          # refactor plan for the cwd, full report on screen
```

Run from a non-git directory and it asks for a URL and clones a local copy first.

The same three are available inside `luxe chat` as `/gitsummary`, `/gitreview`,
`/gitrefactor` (analyzing the session's repo). **GitHub data** (merged/open PRs,
issues, releases, stars) is gathered via the `gh` CLI when it's installed and
authenticated against a GitHub remote; otherwise the report degrades gracefully
to local-git signals and notes that `gh` was unavailable. `gh` is invoked
out-of-band — it is never added to the agent's bash allowlist, so the benchmark
tool surface stays byte-identical.

### Interactive chat tiers

`luxe chat` is the same champion (`Qwen3.6-35B-A3B-6bit`) wrapped in the
agentic loop. How much luxe *harness* sits on top of the model is a spectrum
controlled by the `LUXE_*` substrate flags (`src/luxe/agents/loop.py`) and the
role's prompt overlay. Three shortcuts pin the useful points (the wrappers live
in `~/dotfiles/bin`, synced across hosts; they assume luxe at `~/Downloads/luxe`,
override with `LUXE_HOME`):

| Command | Substrate (`loop.py`) | Prompt / SpecDD | Config |
|---|---|---|---|
| `luxe`      | tiered-compaction on only *(shipped default)* | `manage_strict_only` overlay | `configs/chat.yaml` |
| `luxe-bare` | **all interventions off** | baseline prompts, **no SDD** | `configs/chat_bare.yaml` |
| `luxe-full` | **all validated levers on** | `manage_strict_only` overlay | `configs/chat.yaml` |

- **`luxe-bare`** is the "plain Claude-CLI clone" — the raw champion. It exports
  `LUXE_TIERED_COMPACT=0 LUXE_REFLECT=0 LUXE_ADAPTIVE_POLICY=0
  LUXE_WRITE_PRESSURE=0 LUXE_EARLY_BAIL=0 LUXE_PROSE_BURST=0
  LUXE_ACTION_DENSITY_GATE=0 LUXE_CONVERGENCE_GATE=0 LUXE_REPROMPT_ON_DOC=0`
  and points `chat` at `configs/chat_bare.yaml` (identical to `chat.yaml` minus
  the `task_overlay_id: manage_strict_only` line → `RoleConfig` baseline
  prompts). Equivalent to `luxe compare` mode-1's "bare champion" side.
- **`luxe-full`** flips every *validated* lever to `1`. The three default-OFF
  *refuted* experimental flags (`LUXE_RESPOND_TERMINAL`,
  `LUXE_EARLY_BAIL_TRAJECTORY_SHAPE`, `LUXE_EARLY_BAIL_COMMIT_ONLY`) stay off.
- The model weights never change — bare vs full is purely harness scaffolding.
- chat starts read-only; type `/write` in the REPL to enable edits + `bash`.

### Chat commands

`/help` lists them all. Beyond the slot/model/context controls (`/model`, `/use`,
`/ctx`, `/write`, `/bash`, `/sys`, `/memory`, `/resume`, `/clear`):

**Startup flags** (so autonomous `/goal` users don't have to type REPL commands
first): `luxe chat --verbose diff|full`, `--show-reasoning`, `--no-terse`,
`--debug` (= verbose full + reasoning), and `--theme auto|cool|warm|mono` (curated
palettes). The shipped **default palette is `cool`**, resolved `--theme` flag →
`LUXE_THEME` env → `cool`; set `LUXE_THEME=auto` (or `--theme auto`) to track your
terminal/YASL theme instead. The banner shows the build's git short-SHA so a run
is traceable to a commit. `/ctx huge` reaches a 256K window where the box's
`num_ctx_max` allows it (default window stays 32K).

**Git & MCP:** native read-only `git_diff/log/show` tools are always available; for
richer git, add the commented git MCP server in `configs/mcp.yaml` (it's auto-
namespaced `mcp__git__<tool>`). luxe has no large-repo *chunking* yet — it relies
on BM25 search + the symbol index + tiered context compaction; repo-splitting for
big refactors is future work (`docs/g1-context-lifecycle-design.md`).

**Output verbosity** is three independent toggles, not a single dial:

| Command | What it does |
|---|---|
| (default) | One terse line per tool call: `→ tool(arg) ✓ <bytes>`; the final answer is **truncated to a preview** with a `… +N lines — /verbose for full` hint. |
| `/compact` | Tighter on-screen ceiling for the final answer (also `luxe chat --compact`). Independent of `/verbose`/`/terse`. |
| `/verbose [diff\|full\|off]` | Expand tool I/O: `diff` shows `edit_file` as a highlighted unified diff, `write_file` headers, and tool result/error bodies (capped); `full` syntax-highlights whole file contents AND prints the final answer untruncated. Also renders the working-state ledger each turn. Bare `/verbose` toggles off↔diff. |
| `/reasoning` | Stream the model's thinking live (dim) between tool calls. Independent of `/verbose`; responsiveness tracks the backend's streaming cadence. |
| `/debug` | Convenience: turns on `/verbose full` + `/reasoning` together ("show me everything"); toggles both back off. |
| `/terse` | Toggle terse *model* output (default **ON**). Injects a "report only deltas" instruction to cut wordy prose and save tokens; never abbreviates tool output or errors. |

The `LUXE_*` env flags + `[token-progress]` logging in `agents/loop.py` are a
separate lower-level debug layer (see that module).

| Command | What it does |
|---|---|
| `/goal <objective>` · `/goal stop` | Autonomous runner: round 1 = objective, later rounds = `continue work`, until the objective is reached, the round budget (20) is hit, the agent is stuck, or 3 consecutive crashes. Completion is **ledger-aware**: a settled round (no edits) is only DONE when the ledger corroborates (completed non-empty, in_progress cleared) for 2 rounds; settled rounds that record no new completed work trip an honest **"stuck — needs a human"** exit. Each round prints `[goal round N/M]`. Needs `/write`; Ctrl-C or `/goal stop` halts it. |
| `/plan <objective>` | Draft an implementation plan **read-only** (no edits), then choose: **s**ave to a file (never clobbers an existing `plan.md`), **e**xecute it (hands off to the `/goal` runner with the plan as provenance context), **b**oth, or **d**iscard. |

**Working-state ledger.** Across `continue work` / `/goal` rounds, luxe keeps a
compact per-session ledger (`~/.luxe/sessions/<id>/ledger.json`) of
decided / completed / in-progress / blocked items plus files written — injected
as a `<working_state>` block so the model trusts known state instead of
re-reading `plan.md` + every source each round (the dominant token sink at small
context windows). The model maintains it via the `update_ledger` tool; files
written/edited are tracked automatically.

**Interrupting.** Ctrl-C cancels mid-generation (not only at tool boundaries) and
saves the partial turn. A long-running `bash` command finishes first.

**Static analyzers** (`lint`/`typecheck`/`security_scan`/`deps_audit`) resolve
their binary via PATH → `python -m` → `uvx` (no auto-install). When a tool is
genuinely unavailable they return a structured `{"status":"skipped",…}` result
rather than an error, so a missing linter is never misread as "passed". Install
the toolchain with `pip install -e '.[analyzers]'`.

**Theme.** The chat UI (tool lines, status bar, ledger, banner, prompt arrows)
draws its colors from theme *roles* resolved in `chat/theme.py`, which follow your
active `yet-another-statusline` theme (`CLAUDE_STATUSLINE_THEME` →
`~/.claude/statusline-theme`) and otherwise fall back to ANSI-named colors that
track your terminal/iTerm profile — so luxe matches your terminal instead of a
fixed palette.

## Production model

The monolith is configured in `configs/single_64gb.yaml` (see the file for
the exact pin and the rollback alternate). `temp=0.0` is mandatory — the
v1.0 variance probe showed temp=0.2 produced ±2-fixture swings between
identical runs. `src/luxe/luxe.sdd` Forbids the MoE Instruct-2507 family
(long-context fabrication + skipped optional-tool calls).

Bench overlays:
- `configs/single_64gb_swebench.yaml` — SWE-bench A/B
- `configs/single_64gb_swebench_counterexample.yaml` — counterexample probe

## Resilience

- **Body-aware backend retry.** Distinguishes `loading` / `swapping` /
  `warming` (retry with exponential backoff) from `unavailable` / `crashed`
  / `oom` (fail fast). 3 attempts max.
- **Per-stage checkpoints.** Stage outputs persisted under
  `~/.luxe/runs/<id>/stages/`. HEAD-vs-base_sha drift detection blocks
  accidental resume after the repo has moved.
- **Concurrency lock.** `~/.luxe/locks/<sha256(repo_abs_path)>.lock` — two
  parallel runs on the same repo fast-fail with the holding PID. Auto-
  releases on holder death.
- **PR step ledger.** `commit` / `test` / `push` / `create` / `watch_ci`
  each checkpointed. Auth expired between push and PR-create?
  `luxe pr <run-id>` picks up where it left off.
- **Diff-aware citation linter.** Build-breaking gate. Forgives line shifts
  via fuzzy snippet match within ±20 lines on edited files.
- **`.sdd` Forbids enforcement.** Write tools check `Forbids` (any
  operation) and `Forbids creating` (creates only) against the resolved
  `.sdd` chain before mutating the tree; violations raise distinct error
  messages so the model can reroute rather than bail.

## Repo-size scaling

Two retrieval indices, built once per session:

- **BM25** (`bm25_search`) — `rank_bm25` over source files; tokenizer splits
  on non-alphanumerics AND camelCase. Better than `grep` for natural-
  language queries ("where is auth middleware applied?").
- **AST symbols** (`find_symbol`) — tree-sitter for Python, JavaScript,
  TypeScript, Rust, Go. Exact lookup ("show me class UserService"). Returns
  a clear `note` pointing back to BM25 when the language isn't covered, so
  the agent never silently sees zero matches on Java/Ruby/etc.

The repo summary surfaces `symbol_index_coverage: {language: file_count}`
so the model knows which queries have AST coverage and which fall back to
BM25.

## MCP

luxe is both an MCP **client** and an MCP **server**.

**As client** — opt-in via `configs/mcp.yaml`. Per-call timeout (30s),
3-fail circuit breaker, per-server soft cap (50/run) and global hard cap
(200/run). Stdio subprocess lifetime owned by the manager; SIGTERM then
SIGKILL on close.

**As server** — `luxe serve` exposes three read-only tools by default
(`luxe_review`, `luxe_summarize`, `luxe_explain`); mutation
(`luxe_maintain`) is gated behind THREE locks (the `--unsafe` flag at boot,
`LUXE_MCP_UNSAFE=1` env at call time, and a `confirm_token` matching env-set
`LUXE_MCP_TOKEN`). Per-tool rate limits. Audit log at
`~/.luxe/mcp_audit.jsonl` with secrets redacted.

## Benchmarks

Three benches live in `benchmarks/`. Use them in this order:

### `maintain_suite` — 10-fixture acceptance harness

The original v1.0 gate (≥8/10) was met in v1.4 and the suite is now used
as a regression / variance probe across prompt and config changes.

```bash
# Run all fixtures (resumable)
.venv/bin/python -m benchmarks.maintain_suite.run --all \
    --work-dir ~/.luxe/bench-workspace --keep-loaded

# One specific fixture
.venv/bin/python -m benchmarks.maintain_suite.run --id <fixture-id> \
    --work-dir ~/.luxe/bench-workspace --keep-loaded

# Re-run errored / skipped fixtures, or force a re-run
.venv/bin/python -m benchmarks.maintain_suite.run --all --retry-errors
.venv/bin/python -m benchmarks.maintain_suite.run --all --retry-skipped
.venv/bin/python -m benchmarks.maintain_suite.run --id <id> --force
```

Fixture format in `benchmarks/maintain_suite/fixtures.yaml`:

```yaml
fixtures:
  - id: my-typo-fix
    repo_path: ~/code/my-blog
    base_sha: a1b2c3d4...
    goal: "fix the typo in the README"
    task_type: bugfix
    expected_outcome:
      kind: regex_present
      pattern: "previously misspelled phrase"

  - id: feature-rate-limit
    repo_url: https://github.com/me/some-public-repo
    base_sha: e5f6...
    goal: "add a rate limiter to the /signup endpoint"
    task_type: implement
    expected_outcome:
      kind: tests_pass
      command: "pytest tests/test_rate_limit.py -q"
```

`expected_outcome.kind` ∈ `regex_present` / `regex_absent` / `tests_pass` /
`manual_review`. `--variants <yaml>` runs the same fixture set across
multiple `(prompt, config)` cells; results land under
`acceptance/<output>/<variant_id>/<fixture_id>/` and the harness emits a
`comparison.json` plus a printed table.

**Pin `--work-dir`.** Random tempdirs leak into prompts and dominate
temp=0 variance — see `project_workdir_variance_leak`.

The runner has three layers of recovery — kill it any time, restart picks up:

1. **Per-fixture status** in `acceptance/<id>/state.json`.
   `pending → running → done | error | skipped`.
2. **Per-stage checkpoints** at `~/.luxe/runs/<run-id>/stages/`. The runner
   calls `luxe pr` instead of restarting the whole pipeline when a `running`
   fixture has a saved `luxe_run_id`.
3. **PR-step ledger** at `~/.luxe/runs/<run-id>/pr_state.json`.

### `swebench` — SWE-bench Verified A/B (active v1.6 ship gate)

Pre/post-SpecDD comparison on the curated n=75 subset
(`benchmarks/swebench/subsets/v1_baseline_n75.json`). Pre-SpecDD baseline
is in `acceptance/swebench/pre_specdd_v141_n75/`; v1.6 v3 is the next
target.

```bash
brew services restart omlx && sleep 5 && \
LUXE_LOG_TOOL_CALLS=1 OMLX_API_KEY=omlx-sdb25582k3mq8pf9 \
  .venv/bin/python -m benchmarks.swebench.run \
    --subset benchmarks/swebench/subsets/v1_baseline_n75.json \
    --output acceptance/swebench/post_specdd_v16_creation_only_n75/rep_1/

# Compare two runs (verdict deltas + escape audit)
.venv/bin/python -m benchmarks.swebench.compare_runs \
    --pre  <pre>/predictions.json --post <post>/predictions.json \
    --gold-source benchmarks/swebench/subsets/raw/verified.jsonl

# Inspect a single run (verdict tally, new_file_in_diff escapes)
.venv/bin/python -m benchmarks.swebench.smoke_inspect \
    --predictions <run>/predictions.json \
    --gold-source benchmarks/swebench/subsets/raw/verified.jsonl
```

The adapter binds `LUXE_WRITE_PRESSURE=1` and disables `commit.gpgsign`
automatically; no shell env munging needed beyond `OMLX_API_KEY`.

### `bfcl` — Berkeley Function-Call Leaderboard v3

Tool-call evaluation across the Python subset (n=1240). Two anchors filed:

| Run | Total | Parallel cliff (parallel / parallel_multiple) | Irrelevance | Wall |
|---|---|---|---|---|
| Pre-SpecDD raw (v1.4.1, 2026-05-04) | 76.29% | 66.00% / 49.00% | 91.67% | ~6.7h |
| Post-SpecDD raw (v1.6, 2026-05-10) | 76.45% | 65.50% / 48.00% | 92.08% | ~6.1h |
| Post-SpecDD **agent** (v1.6, 2026-05-11) | **83.71%** | **82.50% / 64.50%** | 85.83% | ~8.5h |

Raw-mode delta v1.4.1→v1.6 is +0.16pp — no infra drift across SpecDD ship.
Agent-mode adds +7.26pp over raw, with the parallel cliff lifting +16–17pp.
**Caveat**: agent-mode irrelevance regresses −6.25pp (loop primes
tool-eagerness); BFCL agent adapter does NOT wire `.sdd` or the Lever 1
spec validator yet, so the lift is loop-vs-single-shot, not SpecDD-driven.

```bash
.venv/bin/python -m benchmarks.bfcl.run --mode raw   --output <dir>   # ~6h
.venv/bin/python -m benchmarks.bfcl.run --mode agent --output <dir>   # ~8.5h
```

## Layout

```
luxe/
├── configs/
│   ├── single_64gb.yaml                          # default mono config
│   ├── single_64gb_swebench.yaml                 # SWE-bench A/B overlay
│   ├── single_64gb_swebench_counterexample.yaml  # counterexample probe
│   ├── mcp.yaml                                  # MCP client + server policy
│   └── pr.yaml                                   # PR cycle config
├── src/luxe/
│   ├── cli.py                  # luxe maintain | pr | runs | serve | check | unload
│   ├── luxe.sdd                # root architectural contract
│   ├── pr.py                   # branch → commit → test → push → PR
│   ├── locks.py                # per-repo flock
│   ├── run_state.py            # RunSpec, stage checkpoints, PR ledger, events
│   ├── citations.py            # diff-aware citation linter
│   ├── search.py               # BM25 retrieval
│   ├── symbols.py              # tree-sitter AST symbols
│   ├── repo_index.py           # repo summary + symbol coverage
│   ├── backend.py              # oMLX client (body-aware retry)
│   ├── sdd.py                  # .sdd chain parser
│   ├── spec.py                 # SpecDD Lever 1 spec parser
│   ├── spec_resolver.py        # .sdd resolution + Forbids/Forbids-creating eval
│   ├── spec_validator.py       # spec-vs-diff per-requirement validator
│   ├── agents/                 # loop.py, single.py, prompts.py (registry)
│   ├── tools/                  # fs (write/edit + Forbids gate), git, shell, analysis, cve_lookup
│   └── mcp/                    # client, server, bridge
├── benchmarks/
│   ├── maintain_suite/         # 10-fixture acceptance harness + variants
│   ├── swebench/               # SWE-bench Verified n=75 A/B
│   └── bfcl/                   # BFCL v3 Python subset
├── tests/                      # 643 tests
├── RESUME.md                   # current project state, active task, ship gates
└── lessons.md                  # postmortems for every historical surprise
```

## Testing

```bash
pytest tests/ -v
```

643 tests covering: agent loop, mono-mode prompt registry, diff-aware
citation linter (incl. line-shift forgiveness on edited files), backend
body-aware retry, PR cycle (preflight, empty-diff semantics, resume from
each step), per-repo flock, run-state checkpointing + drift detection, MCP
client (timeouts, circuit breaker, caps), MCP server (read-only-by-default,
token gate, audit log), BM25 indexing, tree-sitter symbol indexing across
5 languages, repo summary coverage transparency, SpecDD `.sdd` parsing
+ resolution + Forbids / Forbids-creating semantics, SpecDD Lever 1 spec
parsing + per-requirement validation, write-pressure loop, BFCL adapter +
schemas + grader, SWE-bench adapter, acceptance grader scoring rules,
bench runner resumption decisions.

Tests run without a live oMLX server (HTTP transport mocked).

## Research notes

External-project teardowns kept for cross-pollination. These also touch
sibling projects (`micro-mind`, `mage-hands`), not luxe alone.

- [`docs/research/forge-overlap-analysis.md`](docs/research/forge-overlap-analysis.md)
  — forge ↔ luxe ↔ micro-mind overlap + candidate port items.
- [`docs/research/hermes-harvest-backlog.md`](docs/research/hermes-harvest-backlog.md)
  — Hermes Agent (Nous Research) feature backlog harvest.

## Project state

- `RESUME.md` — current state, active task, exact launch commands. Read first.
- `lessons.md` — postmortems for every historical surprise. Read before
  proposing architectural changes.
- `CLAUDE.md` — Claude Code instructions (the `.sdd` chain reading order,
  what's retired, what's bench-as-truth).

## License

[`LICENSE`](LICENSE)
