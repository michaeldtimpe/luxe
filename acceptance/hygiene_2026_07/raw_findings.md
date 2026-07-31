# Raw findings ledger — hygiene sweep 2026-07-30

Phase 1 output. Every lead, including the ones that died. Negative results
are kept with their refutation reason — the next sweep should not re-spend
time on them.

---

## A. Confirmed (became cards)

### A1 — CI has been dead since 2026-04-29
`source: manual + gh` · `.github/workflows/luxe-tests.yml`

The plan's seed said "CI has likely never triggered". The truth is sharper
and worse. Evidence from `gh run list --limit 100`:

- **2026-04-27 → 04-28**: 23 green runs on branch + `main` pushes. The
  workflow was correct *then* — luxe was a `luxe/` subdirectory of a larger
  monorepo, so `paths: luxe/**` and `working-directory: luxe` both resolved.
- **2026-04-29 onward**: the repo was extracted to its own root. The
  workflow was never updated. Since then:
  - branch/PR pushes **never trigger** — `paths: luxe/**` matches nothing;
  - tag pushes **do** trigger (GitHub does not evaluate `paths` filters for
    tag pushes) and **fail in 8–12s, 13 consecutive times**.
- Verbatim failure (run 26168744098):
  `##[error]An error occurred trying to start process '/usr/bin/bash' with
  working directory '/home/runner/work/luxe/luxe/luxe'. No such file or directory`
- Last run of any kind: **2026-05-20**. No CI signal for 10 weeks.

### A2 — fixing A1 alone would still be red
`source: manual (empirically verified)`

Built a dev-only venv exactly as the runner does
(`uv sync --extra dev`, no `--extra chat`, no `extended-bench`) and ran the
suite: **1852 passed, 7 skipped, 4 errors**. The 4 errors are
`tests/test_mlx_direct_smoke.py`, `ModuleNotFoundError: No module named 'mlx'`.
MLX is Apple-silicon only and can never install on `ubuntu-latest`. So the
workflow fix must be paired with A3 or CI stays red.

### A3 — the `live_model` marker is declared but never enforced
`source: manual` · `pyproject.toml:56-58`, `tests/test_mlx_direct_smoke.py:17`

`pyproject.toml` registers the marker with the text *"skip by default; run
manually after stopping oMLX"*, and the test module's docstring repeats
*"Marked `live_model` — skipped by default."* Neither is true: there is no
`addopts = -m "not live_model"` and no `conftest.py` hook. Consequences,
all measured:

1. The test **runs on every local `pytest`**, loading real MLX weights.
2. It is the slowest thing in the suite by an order of magnitude —
   7.09s setup + 0.86s call = **~17% of a 47.8s full-suite wall**.
3. It is the direct cause of A2.

`live_backend` is registered too and used by **zero** tests — dead marker.

### A4 — `format_sdd_block` emits a dangling header
`source: manual (found while building the golden fixture)` ·
`src/luxe/spec_resolver.py:234-253`

The function writes the `## Repository contracts (.sdd files)` header
*before* the loop, then `continue`s past any `.sdd` with no
`forbids`/`forbids_create`/`owns`. A repo whose contracts are all
prose-only (`Must` / `Must not` / `Done when`) therefore injects a bare
header with nothing under it into every task prompt. Reproduced directly:
a fixture `.sdd` carrying only `Must:`/`Must not:` produced exactly
`"...final report.\n\n## Repository contracts (.sdd files)\n"`.

**Tier A** — this is on the benchmark prompt path. The fix is
behavior-preserving for every repo that has at least one enforceable
contract (including luxe itself), and the golden-request test pins that.

### A5 — unused import, new since the last ruff sweep
`source: ruff` · `src/luxe/chat/slots.py:14: F401 'os' imported but unused`

RESUME.md records "ruff still the 5 deliberate findings"; `ruff check src`
now reports **6**. The five known ones (2× E402, 2× E731, plus
`convergence.py`) are deliberate. This one is a leftover.

### A6 — `results` shadowed inside the bench harness's `finally`
`source: mypy` · `benchmarks/maintain_suite/run.py:1637-1639`

```
results = _ub.unload_all_loaded()      # rebinds the outer list[FixtureResult]
n_ok = sum(1 for v in results.values() if v)
```
Harmless *today* — the rebind happens in `finally` after the return value is
computed — but it is a live landmine for anyone who later adds a statement
between them, and it is 2 of the 102 mypy errors. Tier A, rename-only.

---

## B. Confirmed but deferred (written up, not executed)

### B1 — `git clone` with no timeout
`src/luxe/cli.py:37`, `src/luxe/gitkit/runner.py:120`. A hung network clone
blocks forever with no feedback. Real, but any cap risks killing a
legitimately slow clone of a large repo, and neither path is on the
fallback-kit critical route. Wants a user decision on the cap.

### B2 — 18 transitive dependency vulnerabilities
`pip-audit`: aiohttp 3.14.0 (8), starlette 1.2.1 (3), setuptools 82.0.1 (2),
cryptography 48.0.0, mcp 1.27.2, msgpack 1.1.2, pydantic-settings 2.14.1,
python-multipart 0.0.30. All transitive; all have fix versions. **Dependency
bumps are an explicit stop-and-ask trigger in the plan** and a bad idea to
land unattended in a tool whose entire mission is being available. Proposal
only — see REPORT.md.

### B3 — mypy annotation debt (102 errors, ~0 bugs)
Read every high-signal class. `union-attr`, `attr-defined`, `index` and
`assignment` hits in `gitkit/{plan,diffscope,runner,deep}.py`,
`mcp/client.py`, `agents/guardrails.py` are **all** narrowing failures on
code that is correctly guarded at runtime (verified by reading each site).
The honest characterisation: this is annotation debt, not defects. Fixing it
means touching Tier A files for zero runtime benefit, so it is deferred as a
batch rather than dribbled through this sweep. The one exception, A6, is
carded because it is a genuine readability hazard.

### B6 — a model unloaded mid-request hangs luxe past its read timeout
`src/luxe/backend.py:146,167` — `timeout_s: float = 600.0`,
`httpx.Timeout(timeout_s, connect=30.0)`.

Observed, not theorised. After B5 evicted the weights out from under the
`gitaudit` run, its in-flight `/v1/chat/completions` call **never returned
and never timed out**:

- request issued ~22:26; weights evicted ~22:29; still blocked at 22:49.
- **23 minutes on a 600s read timeout.** No `BackendError`, no retry, no
  log line — the process sat in `S` state on an ESTABLISHED socket with
  2.45s of CPU consumed over 40 minutes of wall.
- The server was fine throughout: `luxe smoke` run immediately after the
  kill was green in 17s (endpoint, catalog, main turn, tool call, fallback
  turn). So this is a **client-side** hang, not an oMLX outage.

**Not root-caused.** I did not determine why the httpx read timeout failed
to fire — plausible candidates are the server holding the connection open
while dribbling nothing, or the timeout not applying to the phase the
request was parked in. That investigation is the first step, not the fix.

Why it matters more than its trigger: luxe's entire mission is being
available during an outage. A hang with no timeout, no error and no log
line is the worst failure shape for a fallback tool — `~/.luxe/sessions/`
gets nothing, so post-hoc diagnosis has nothing to read either. The trigger
does not require my mistake: any concurrent `luxe chat` `/quit` (B5), any
admin unload, or an oMLX restart mid-turn reproduces it.

Repro sketch: start a long `luxe gitaudit --deep`, wait for a chunk pass to
begin, then `curl` the oMLX admin unload (or quit a chat session on the same
endpoint). Expect a `BackendError` within `timeout_s`; observe an indefinite
hang.

### B5 — `luxe chat` exit unloads *every* model on the server, not its own
`src/luxe/chat/repl.py:391-396` → `slots.unload_all()` →
`backend.unload_all_loaded()`, which iterates `loaded_models()` — i.e. the
server's whole resident set — and unloads all of it unless `--keep-loaded`
was passed.

**Hit live during this sweep.** A `/quit` from a 30-second `/status`
+ `/doctor` spot-check evicted the weights of the `luxe gitaudit` run that
had been going for ten minutes in another process, on the same endpoint
(`loaded_count` went 1 → 0). oMLX reloads on demand so it is recoverable,
not corrupting, but it costs a reload and can interrupt an in-flight request.

This matters more than it looks: `configs/chat.yaml` `backends:` makes m5 a
**shared fleet endpoint**. Under that topology one host quitting a chat
session unloads the models another host is actively using.

Deferred, not fixed — it is a behaviour change to the chat front-end
(an explicit stop-and-ask trigger). Two candidate shapes, both needing a
decision: unload only the models *this session* loaded (`slots` already
tracks swaps, so the set is known), or skip the unload when the endpoint is
non-local. `except_for` already exists on `unload_all_loaded` and is the
natural seam.

### B4 — four git-subprocess wrappers in `chat/`
`status.py:_run_git` (timeout 2), `inspection.py:_git` (10),
`project.py:_git_root` (10), `smoke.py` (30, inline). Looks like textbook
duplication, but the contracts genuinely differ — status.py's 2s cap is
tuned for a per-keystroke toolbar redraw, and the return shapes are
different by design. Consolidating would flatten deliberate decisions.
Deferred as **not worth the risk**, recorded so the next sweep doesn't
re-discover it as new.

---

## C. Refuted (do not re-spend time here)

| Lead | Verdict |
|---|---|
| 70 `read_text`/`write_text` + 8 `open()` sites lack `encoding=` | **Refuted empirically.** Under `LC_ALL=C LANG=C`, Python 3.11 on macOS still reports `utf-8` (PEP 538/540 C-locale coercion) and a unicode `write_text` succeeds. Also, every JSONL writer goes through `json.dumps`, which is `ensure_ascii=True` by default. `chat/debuglog.py` already passes `encoding="utf-8"` explicitly. |
| Plan seed 1.8b: analyzers extra missing → `tools/analysis.py` silently degraded | **Refuted twice.** (1) The analyzers *are* installed. (2) `analysis.py` does not degrade silently — `_resolve()` falls back PATH → `python -m` → `uvx`, and `_skipped()` returns a structured payload naming the missing tool and the install command. The pyproject comment describes a historical incident this design already fixed. No `/doctor` card. |
| `src/luxe/memory/` reads `~/.claude/` or `CLAUDE.md` | Clean. Only `Path.home()/".luxe"`; the only `~/.claude` mentions are docstrings restating the prohibition. |
| Inline prompt outside the registry (`agents/reflect.py:85`) | Sanctioned. `agents.sdd:15` — *"reflect.py is the single source of truth for the [verifier] surface"*. |
| `Path.glob` on a user root (`tools/fs.py:326`) | Sanctioned and correct — `_glob_matches_tolerant` exists specifically to catch the `OSError(ETIMEDOUT)` that `luxe.sdd` warns about, and returns a partial result with a reason. |
| Bare `except:` in `src/` | None. |
| `/help` rows vs the command dispatch table | Consistent. 35 help rows, 41 dispatch entries; all 6 extras (`/exit`, `/q`, `/gitsummary`, `/gitreview`, `/gitrefactor`, `/gitplan`) are commented as intentional hidden aliases. The 2026-07-30 `/help` audit holds. |
| TODO/FIXME/XXX backlog | 2 hits in `src/`, both explanatory prose about the placeholder guard. No backlog. |
| Skipped tests | 6, each with a justification (4× un-vendored BFCL data, 1× absent m5 artifacts, 1× deferred compaction integration with a documented substitute test). No cards. |
| Flaky tests | None. Three full runs: 1877/1877/1882 (the +5 is this sweep's golden tests), identical skip sets. |
| Slow tests > 5s | Exactly one, and it is A3. Nothing else in the suite exceeds 0.6s. |
| `subprocess` without `timeout` | 12 sites; the risky class (running user-controlled commands) is already capped — `spec_validator.py:258` has `timeout=600`, `gitkit/apply.py:104` and `chat/smoke.py:257` are capped. The remainder are local `git` invocations, plus `chat/commands.py:1329` `subprocess.call([editor, …])` where a timeout would be actively wrong. Only the two clones (B1) are exposed. |

---

## D. `luxe gitaudit` (dogfood)

Launched at Phase 1 start against this repo. See § "Dogfood" in REPORT.md
for outcome and for product feedback on gitkit itself.
