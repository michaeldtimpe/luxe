# luxe

MLX-only repo maintainer for Apple Silicon. Takes any of your repos and adds
features, fixes bugs, updates docs, or audits maintenance — and opens a PR.

> **Status:** pre-v1.0. Implementation work for phases 0–8 of the
> [plan](.claude/plans/linked-leaping-curry.md) is shipped (~7.5k LOC,
> 282 tests). v1.0 release is gated on the acceptance suite — populate
> `benchmarks/maintain_suite/fixtures.yaml` and run the bench until ≥8/10
> fixtures pass. See [`Benchmark workflow`](#benchmark-workflow) below.

## What luxe does

```
luxe maintain <repo> "<goal>"
  ↓
mode selection (single | swarm)  →  ~/.luxe/runs/<id>/
  ↓
pipeline (architect → workers → validator → synthesizer)
  ↓
diff-aware citation lint (zero unresolved fabrications)
  ↓
git checkout -b → commit → tests → push → gh pr create
```

Two execution modes share one agent loop:

- **single** — one capable model (Qwen2.5-32B), full read+write+shell+git
  surface, agentic loop. Small/familiar repos, summaries, quick fixes.
- **swarm** — architect → workers → validator → synthesizer pipeline.
  Larger repos, multi-step changes, anything where context exhaustion
  would bite a single model.

Mode is picked deterministically: goal-keyword classifier first
(`implement`/`refactor` → swarm; `review`/`summarize` → single), source-byte
threshold as fallback (>500 KB of source → swarm; one 3000-line file in a
49-file repo would still go to swarm).

## Why MLX-only

Earlier multi-backend versions (Ollama / llama.cpp / LM Studio / oMLX / MLX)
produced repeated real failures: silent context truncation, fabricated
citations, model-loop bugs. luxe v1.0 strips to oMLX only — every other
moving part is one less thing that can lie about token budgets.

## Install

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Requires:
- Python 3.11+
- A running [oMLX](https://github.com/nicholasgasior/omlx) server on
  `localhost:8000`
- `gh` CLI authenticated (`gh auth login`) for the PR cycle
- Apple Silicon with ≥64 GB unified memory for the production roster

## CLI

```
luxe maintain <repo> "<goal>" [--mode auto|single|swarm] [--task <type>]
                              [--allow-dirty] [--yes] [--watch-ci]
luxe resume  <run-id> [--force-resume]      # resume a paused/failed run
luxe pr      <run-id> [--push-only]         # resume just the PR cycle
luxe runs list | luxe runs gc               # housekeeping
luxe serve   [--transport stdio|sse] [--unsafe]  # MCP server (read-only by default)
luxe check                                  # oMLX + models + gh auth
```

Examples:

```bash
# Auto-pick mode based on goal + repo size
luxe maintain ~/code/my-app "fix the off-by-one in pagination"

# Force swarm mode for a hard refactor
luxe maintain ~/code/my-app "rewrite the storage layer to use sqlite" --mode swarm

# Read-only review (no PR)
luxe maintain ~/code/my-app "review the auth module for security bugs" --task review

# Resume a long swarm run that crashed at the synthesizer stage
luxe resume <run-id>
```

## Production roster (64 GB)

| Role | Model | Mem | Used in |
|---|---|---|---|
| Architect | Qwen2.5-7B-Instruct-4bit | 5 GB | swarm |
| Worker (read) | DeepSeek-Coder-V2-Lite-Instruct-4bit | 8.8 GB | swarm |
| Worker (code/analyze) | Qwen2.5-Coder-14B-Instruct-MLX-4bit | 8 GB | swarm |
| Validator | Qwen2.5-7B-Instruct-4bit | 5 GB | swarm |
| Synthesizer + single-mode monolith | Qwen2.5-32B-Instruct-4bit | 19 GB | both |

Sequential loading; oMLX swaps. Single mode and swarm-synthesizer share the
32B weights so warm-cache benefits are real when alternating modes.

## Resilience

- **Body-aware backend retry.** Distinguishes `loading`/`swapping`/`warming`
  (retry with exponential backoff) from `unavailable`/`crashed`/`oom` (fail
  fast). 3 attempts max.
- **Per-stage checkpoints.** architect / worker_<i> / validator / synthesizer
  outputs persisted under `~/.luxe/runs/<id>/stages/`. `luxe resume` skips
  completed stages; HEAD-vs-base_sha drift detection blocks accidental
  resume after the repo has moved.
- **Concurrency lock.** `~/.luxe/locks/<sha256(repo_abs_path)>.lock` — two
  parallel runs on the same repo fast-fail with the holding PID. Auto-
  releases on holder death.
- **PR step ledger.** commit / test / push / create / watch_ci each
  checkpointed. Auth expired between push and PR-create? `luxe pr <run-id>`
  picks up where it left off.
- **Diff-aware citation linter.** Build-breaking gate. Forgives line shifts
  via fuzzy snippet match within ±20 lines on edited files.
- **Validator output contract.** Structured JSON envelope with status
  `cleared` / `verified` / `ambiguous`. >50% removed → ambiguous (synthesizer
  flags the run; PR still opens — ambiguity ≠ block).

## Repo-size scaling

Two retrieval indices, built once per session:

- **BM25** (`bm25_search`) — `rank_bm25` over source files; tokenizer splits
  on non-alphanumerics AND camelCase. Better than `grep` for natural-
  language queries ("where is auth middleware applied?").
- **AST symbols** (`find_symbol`) — tree-sitter for Python, JavaScript,
  TypeScript, Rust, Go. Exact lookup ("show me class UserService"). Returns
  a clear `note` pointing back to BM25 when the language isn't covered, so
  the agent never silently sees zero matches on Java/Ruby/etc.

The architect's repo summary surfaces `symbol_index_coverage: {language:
file_count}` so it knows which queries have AST coverage and which fall
back to BM25.

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

## Benchmark workflow

The acceptance suite is the v1.0 release gate. It also doubles as the
diagnostic harness for tuning luxe to your repos.

### 1. Curate fixtures

Edit `benchmarks/maintain_suite/fixtures.yaml`:

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
    repo_path: ~/code/my-api
    base_sha: e5f6...
    goal: "add a rate limiter to the /signup endpoint"
    task_type: implement
    expected_outcome:
      kind: tests_pass
      command: "pytest tests/test_rate_limit.py -q"

  - id: github-issue-via-mcp
    repo_url: https://github.com/me/some-public-repo
    base_sha: deadbeef...
    goal: "..."
    task_type: bugfix
    expected_outcome: { kind: regex_absent, pattern: "TODO" }
    required_env: [GITHUB_TOKEN]
```

Per [plan §10](.claude/plans/linked-leaping-curry.md), aim for:
- Mix: 3 bugfix / 3 implement / 2 document / 2 manage.
- Difficulty: 4 trivial-medium (≤50 files), 4 medium (50–500 files),
  2 hard (>500 files; ≥1 must be >100k LOC).
- ≥3 fixtures from repos with no prior luxe history.
- ≤2 `manual_review` fixtures (others must auto-grade).

### 2. Run the bench

```bash
# Run all fixtures (resumable)
python -m benchmarks.maintain_suite.run --all

# One specific fixture
python -m benchmarks.maintain_suite.run --id my-typo-fix

# Re-run errored fixtures after fixing whatever broke them
python -m benchmarks.maintain_suite.run --all --retry-errors

# Re-run skipped fixtures after setting GITHUB_TOKEN etc.
python -m benchmarks.maintain_suite.run --all --retry-skipped

# Force a re-run, discarding cached luxe_run_id
python -m benchmarks.maintain_suite.run --id feature-rate-limit --force

# See decisions without invoking luxe
python -m benchmarks.maintain_suite.run --all --dry-run

# Persistent clone dir (avoid re-cloning between invocations)
python -m benchmarks.maintain_suite.run --all --work-dir ~/luxe-bench-clones
```

### 3. Recovery

The runner has three layers of recovery — kill it any time, restart picks up:

1. **Per-fixture status** in `acceptance/<id>/state.json`.
   `pending → running → done | error | skipped`. Restart skips DONE / SKIPPED
   fixtures; `RUNNING` fixtures are resumed.
2. **Per-stage checkpoints** at `~/.luxe/runs/<run-id>/stages/`. When the
   runner sees a `running` fixture with a saved `luxe_run_id`, it calls
   `luxe resume` instead of `luxe maintain` so worker findings aren't
   recomputed.
3. **PR-step ledger** at `~/.luxe/runs/<run-id>/pr_state.json`. `luxe resume`
   replays only the incomplete PR steps.

### 4. Diagnostics

After a bench run, `acceptance/summary.json` includes a `diagnostics` block
with tuning hints — e.g. "validator_status=ambiguous in 4/10 fixtures →
consider a stronger validator model or tighter worker prompts". Use these
to guide config edits in `configs/swarm_64gb.yaml` or your own
`configs/<your-name>.yaml`.

Per-fixture `acceptance/<id>/diagnostics.json` has the granular telemetry:
stages completed, stages resumed (from cache), token totals, validator
status, citation lint summary, PR draft state.

### 5. v1.0 release gate

`luxe maintain` ships at v1.0 when:
- ≥8 of ≥10 fixtures pass automated grading (≥4/5 points each)
- Zero unresolved citations across all runs
- Largest fixture (>100k LOC) passes without context exhaustion
- No fixture skipped due to luxe-runtime errors (`skipped_credentials` is
  fine; `skipped_runtime_error` is not)

The runner's exit code is `0` only when those bars are met.

## Layout

```
luxe/
├── configs/
│   ├── swarm_64gb.yaml            # swarm pipeline config
│   ├── single_64gb.yaml           # single-mode monolith
│   ├── mode.yaml                  # mode-selector keywords + threshold
│   ├── mcp.yaml                   # MCP client servers + server policy
│   ├── pr.yaml                    # PR cycle (test detection, dirty-tree, watch-ci)
│   └── runs.yaml                  # checkpoint retention
├── src/luxe/
│   ├── cli.py                     # luxe maintain | resume | pr | runs | serve | check
│   ├── mode_select.py             # deterministic mode selection
│   ├── escalation.py              # single → swarm context preservation
│   ├── pr.py                      # branch → commit → test → push → PR
│   ├── locks.py                   # per-repo flock
│   ├── run_state.py               # RunSpec, stage checkpoints, PR ledger, events
│   ├── citations.py               # diff-aware citation linter
│   ├── search.py                  # BM25 retrieval
│   ├── symbols.py                 # tree-sitter AST symbols
│   ├── repo_index.py              # architect-facing summary + coverage
│   ├── backend.py                 # oMLX client (body-aware retry)
│   ├── agents/                    # loop, single, architect, worker, validator, synthesizer
│   ├── pipeline/orchestrator.py   # swarm driver with stage checkpointing
│   ├── tools/                     # fs, git, analysis, shell, base
│   └── mcp/                       # client, server, bridge
├── benchmarks/maintain_suite/
│   ├── fixtures.yaml              # YOUR fixtures go here
│   ├── run.py                     # the bench runner (resumable)
│   └── grade.py                   # automated scorer
└── tests/                         # 282 tests
```

## Testing

```bash
pytest tests/ -v
```

282 tests covering: agent loop, mode selection, single-mode escalation,
validator structured envelope, diff-aware citation linter (incl. line-shift
forgiveness on edited files), backend body-aware retry, PR cycle (preflight,
empty-diff semantics, resume from each step), per-repo flock, run-state
checkpointing + drift detection, MCP client (timeouts, circuit breaker,
caps), MCP server (read-only-by-default, token gate, audit log), BM25
indexing, tree-sitter symbol indexing across 5 languages, repo summary
coverage transparency, acceptance grader scoring rules, bench runner
resumption decisions.

Tests run without a live oMLX server (HTTP transport mocked).

## Development plan

[`.claude/plans/linked-leaping-curry.md`](.claude/plans/linked-leaping-curry.md)
is the implementation-ready plan. It folds two rounds of reviewer feedback
and maps each round-table point to its plan section in an appendix.

## License

[`LICENSE`](LICENSE)
