# Hygiene sweep 2026-07-30 — operator notes

Executor: Opus 5 Claude Code session, unattended overnight run authorised by
the user ("perform this work overnight without intervention"). That
authorisation supersedes the plan's § 2.4 user checkpoint; triage is
presented in REPORT.md instead of gating execution.

## Constraints digest (Phase 0.1)

Read: CLAUDE.md, all eight `.sdd` files, RESUME.md handoff header,
lessons.md index.

1. **Mono only.** No swarm/micro/phased; `src/{swarm,micro,phased}/**` are
   Forbids globs in `luxe.sdd`. No feature flag may resurrect them.
2. **Single champion** `Qwen3.6-35B-A3B-6bit` (benchmark pin). The only
   sanctioned fan-out is chat slots + per-host manifests, chat-only. The
   m1/m4 interactive default is deliberately NOT the champion.
3. **Bench path byte-identical.** temp=0.0, pinned `--work-dir`,
   `on_token=None` ⇒ `stream=False`. Now mechanised by
   `tests/test_golden_request.py` (Phase 0.5).
4. **Prompts live in the registry** (`agents/prompts.py`). Never inline a
   prompt string in `single.py` / `cli.py` / gitkit.
5. **Tool layer**: honesty guards run before SpecDD Forbids;
   `SddParseError` must be caught before `ValueError` (it subclasses it);
   tool errors return tuples, never raise.
6. **Never `Path.rglob`/`glob` a user-chosen root** — use
   `luxe.fswalk.iter_files`. (2026-07-29 chat crash on a dead NAS mount.)
7. **`src/luxe/memory/` must not read `~/.claude/` or repo `CLAUDE.md`.**
   Exception already sanctioned: `chat/theme.py` reads only the statusline
   theme *name* file, which is not the memory subsystem.
8. **Git: rebase, never merge.** Linear history on origin/main; no
   `--no-verify`; no force-push to main. One logical change per commit.
9. **Subprocess calls that run user-controlled commands need a wall cap**
   (`PRConfig.test_timeout_s` lesson, v1.10.2).
10. **Long-running runs are offered, not auto-run** (standing preference).
    Applies to any bench sweep; `luxe smoke` is explicitly in-plan and was
    run directly.

## Phase 0 deviations from the plan

- **0.2 `uv sync --extra dev --extra chat --extra analyzers` NOT run.** The
  plan assumed the analyzers extra was missing from the venv. It is not:
  ruff 0.15.15, mypy 2.1.0, bandit 1.9.4, pip-audit 2.10.0 all resolve from
  `.venv/bin`. Running that sync would have *removed* the `bfcl` and
  `extended-bench` extras (mpmath, mlx_lm, datasets, esprima) that are
  currently installed, breaking the extended-benchmark suite for no gain.
  Verified versions instead. **Plan seed finding 1.8b ("analyzers not
  installed → `tools/analysis.py` silently degraded") is refuted locally**,
  but the underlying fragility is real on a fresh box — carded as a
  `/doctor` check rather than dropped.

## Phase 0 baselines

| Artifact | Result |
|---|---|
| `baseline_pytest.txt` | **1877 passed, 6 skipped, 0 failed**, 47.80s wall |
| `baseline_ruff.txt` | 223 findings repo-wide; **6 in `src/`** (5 known-deliberate + 1 new) |
| `baseline_mypy.txt` | 102 errors in 18 files (76 source files checked) |
| `baseline_bandit.txt` | 120 low, 1 medium, 2 high (at `-ll`: 3 reported) |
| `baseline_pipaudit.txt` | 18 known vulns across 8 packages, all transitive |
| `baseline_smoke.txt` | see file |

Slowest test: `test_mlx_direct_smoke.py::test_token_logprobs_basic` (7.09s
setup + 0.86s call — it loads real MLX weights). Everything else is
sub-second; there is no slow-test problem in this suite.

### Golden-request guard (0.5)

`tests/test_golden_request.py` + `tests/golden/{run_single_request,prompt_registry}.json`.

Drives a real `Backend` with a stubbed `_client.post`, runs `run_single`
against a fixed in-test repo (with an `src/src.sdd` so Lever 2 is
exercised), and snapshots the **exact HTTP body**: model, messages, the
full OpenAI tools array, and all sampling params. Role config comes from
the shipped `configs/single_64gb.yaml`, so config drift is caught too.

Verification performed (plan requirement):

- passes twice in a row, and a `_deterministic` test asserts two
  invocations produce identical bodies;
- perturbing `_BASELINE_SYSTEM` in `prompts.py` → **2 tests fail** with a
  readable one-line diff; reverted;
- perturbing a `read_file` tool description in `tools/fs.py` → **fails** on
  `tools[0].function.description`; reverted.

Regenerate intentionally with
`LUXE_UPDATE_GOLDEN=1 uv run pytest tests/test_golden_request.py -q`.
