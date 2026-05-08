# luxe

MLX-only repo maintainer for Apple Silicon. Takes any of your repos and adds
features, fixes bugs, updates docs, or audits maintenance — and opens a PR.

> **Status:** v1.6.0-rc-1 (SpecDD Lever 2, creation-only forbids). 643 tests
> passing. Code shipped; tag held until the SWE-bench n=75 v3 rerun confirms
> the ship floor (`new_file_in_diff = 0`, strong ≥14, strong+plausible ≥30,
> empty_patch ≤18). See `RESUME.md` for the active task and the launch command.

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

Tool-call evaluation across the Python subset (n=1240). Pre-SpecDD raw-mode
baseline is 76.29% (single-call 82-92%, parallel cliff at 49-66%) — see
`project_bfcl_pre_specdd_baseline`.

```bash
.venv/bin/python -m benchmarks.bfcl.run --subset python --output <dir>
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

## Project state

- `RESUME.md` — current state, active task, exact launch commands. Read first.
- `lessons.md` — postmortems for every historical surprise. Read before
  proposing architectural changes.
- `CLAUDE.md` — Claude Code instructions (the `.sdd` chain reading order,
  what's retired, what's bench-as-truth).

## License

[`LICENSE`](LICENSE)
