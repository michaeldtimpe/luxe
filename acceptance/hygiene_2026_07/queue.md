# Work queue — hygiene sweep 2026-07-30

Ordered per plan § 2.3: bugs before refactors, C before B before A.
Status is updated with the commit sha as each card lands.

Counts: **6 cards** — 5 bug, 1 refactor. By tier: C 2 · B 2 · A 2.
By size: S 6. Executor: opus 4 · luxe-code 2.

Everything else Phase 1 surfaced is either deferred with a reason
(raw_findings § B) or refuted (§ C). Deliberately not padding the queue:
this codebase came in clean — 1877 green tests, no flakes, no skip debt,
no TODO backlog, 6 ruff findings in `src/`, and a mypy set that is
annotation debt rather than defects.

---

### HS-001 `live_model` tests run despite being documented as skip-by-default
- file: `pyproject.toml:54-58`, `tests/test_mlx_direct_smoke.py:1-17`
- tier: B   type: bug   size: S
- source: manual
- evidence: `pyproject.toml` registers `live_model` as *"skip by default; run
  manually after stopping oMLX"* and the module docstring repeats it, but no
  `addopts` and no conftest hook implement it. `--durations=25` shows
  `test_token_logprobs_basic` at 7.09s setup + 0.86s call — it loads real MLX
  weights on every run, ~17% of the 47.8s suite wall. In a dev-only venv it
  becomes 4 hard errors (`ModuleNotFoundError: No module named 'mlx'`).
- fix: add `addopts = "-m 'not live_model and not live_backend'"` to
  `[tool.pytest.ini_options]`. An explicit `-m` on the command line overrides
  it, so the documented manual invocation keeps working.
- test: `tests/test_marker_policy.py` — assert the live markers deselect by
  default and that `-m live_model` re-selects.
- verify: `uv run pytest -q` (mlx test no longer collected, wall drops);
  `uv run pytest tests/test_mlx_direct_smoke.py -m live_model --collect-only -q`
  still finds 4 tests.
- executor: opus
- status: PENDING

### HS-002 CI has been dead for 10 weeks
- file: `.github/workflows/luxe-tests.yml:5-21`
- tier: C   type: bug   size: S
- source: manual + `gh run list`
- evidence: `paths: luxe/**` + `working-directory: luxe` are left over from
  when luxe was a subdirectory of a monorepo. Since the 2026-04-29 extraction:
  branch/PR pushes never trigger, and the tag pushes that do (GitHub skips
  `paths` filters for tags) fail in 8–12s, 13 times consecutively, with
  `working directory '/home/runner/work/luxe/luxe/luxe'. No such file or
  directory`. Last green: 2026-04-28. Last run of any kind: 2026-05-20.
- fix: drop the `paths:` filters and the `working-directory:` default; run on
  push-to-main + all PRs. Replace `pytest -x` with a full run. Depends on
  HS-001 — without it, `ubuntu-latest` still errors on the MLX import.
- test: n/a (CI config). Verified by the dev-only-venv rehearsal below.
- verify: `uv sync --extra dev` into a scratch venv + `uv run pytest -q`
  reproduces the runner exactly → must be 0 failures / 0 errors.
- executor: opus
- status: PENDING

### HS-003 `format_sdd_block` injects a dangling header into the task prompt
- file: `src/luxe/spec_resolver.py:234-253`
- tier: **A**   type: bug   size: S
- source: manual (surfaced building the Phase 0.5 golden fixture)
- evidence: the `## Repository contracts (.sdd files)` header is emitted
  before the loop; every `.sdd` with no `forbids`/`forbids_create`/`owns` is
  then `continue`d. A repo whose contracts are all prose (`Must` / `Must not`
  / `Done when`) gets a bare header with nothing under it appended to every
  task prompt. Reproduced: task_prompt ended
  `"...final report.\n\n## Repository contracts (.sdd files)\n"`.
- fix: build the per-contract body first; return `""` when no contract
  contributed a line, and only then prepend the header.
- test: `tests/test_spec_resolver.py` — a prose-only `.sdd` yields `""`; a
  mixed set yields the header plus only the contributing contracts.
- verify: targeted test green; `tests/test_golden_request.py` **unchanged**
  (luxe's own contracts all carry `Owns`/`Forbids`, so the champion's request
  must not move — that is the proof this is behavior-preserving where it
  counts).
- executor: opus
- status: PENDING

### HS-004 `results` shadowed in the bench harness's `finally`
- file: `benchmarks/maintain_suite/run.py:1637`
- tier: **A**   type: refactor   size: S
- source: mypy (`attr-defined`, `assignment`)
- evidence: `results = _ub.unload_all_loaded()` rebinds the outer
  `list[FixtureResult]` to a `dict[str, bool]`, then calls `.values()` on it.
  Harmless today (it runs in `finally`, after the return value is computed)
  but it is a landmine and 2 of the 102 mypy errors.
- fix: rename the local to `unload_results`.
- test: existing bench tests pin the surrounding behaviour; no new test —
  this is a pure rename with no reachable behaviour change.
- verify: `uv run pytest tests/test_bench_resume.py -q`; mypy on the file
  shows 2 fewer errors; golden-request test unchanged.
- executor: opus
- status: PENDING

### HS-005 unused `os` import
- file: `src/luxe/chat/slots.py:14`
- tier: C   type: bug   size: S
- source: ruff (`F401`)
- evidence: `ruff check src` reports 6 findings; RESUME.md records 5
  deliberate ones. This is the new leftover.
- fix: delete the `import os` line.
- test: none needed — `ruff check src/luxe/chat/slots.py` is the assertion.
- verify: ruff clean on the file; `uv run pytest tests/test_chat_slots.py -q`.
- executor: **luxe-code** (drill)
- status: PENDING

### HS-006 `cached` doubles as a bool sentinel and a dict payload
- file: `src/luxe/gitkit/deep.py:1241,1249-1251`
- tier: C   type: refactor   size: S
- source: mypy (`assignment`, `index`)
- evidence: the diff-mode branch sets `cached = True` purely to skip the
  survey/save-map branch; the else-branch sets it to `load_map(...)`'s dict
  and then subscripts it. Correct at runtime, but the variable carries two
  unrelated meanings and produces 4 mypy errors.
- fix: introduce `skip_map_io: bool` for the sentinel and leave `cached` to
  mean only "the loaded map, or None".
- test: `tests/test_gitkit_deep.py` already pins both paths (diff-mode and
  cached-map reuse); confirm they pin before changing anything.
- verify: `uv run pytest tests/test_gitkit_deep.py tests/test_gitkit_diff.py -q`;
  4 fewer mypy errors in the file.
- executor: **luxe-code** (drill)
- status: PENDING

### HS-007 dead private helper `_looks_like_url`
- file: `src/luxe/gitkit/runner.py:96`
- tier: C   type: refactor   size: S
- source: manual (unreferenced-symbol scan, Phase 1.7)
- evidence: defined in `4432925` ("prompt to clone a URL when the target
  isn't a git repo") and referenced nowhere since — the caller was rewritten
  to ask `health`/git itself instead of pattern-matching the string. Grep
  over `src/`, `tests/`, `benchmarks/`, `scripts/` returns only the
  definition line.
- fix: delete the function.
- test: none — deleting an unreferenced private helper; the gitkit suite is
  the regression net.
- verify: `uv run pytest tests/test_gitkit*.py tests/test_gitplan.py -q`
  (165 green); ruff clean; full suite unchanged at 1883.
- executor: opus
- **status: DONE** — see commit below
