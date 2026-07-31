# Hygiene sweep 2026-07-30 — final report

Executed overnight, unattended, per the plan in
`docs/plans/2026-07-30-hygiene-sweep.md`. The user's "perform this work
overnight without intervention" superseded the plan's § 2.4 checkpoint;
triage is presented here instead of gating execution. **Nothing has been
pushed** — all commits are local on `main`, awaiting review.

## Headline

The codebase came in clean, and the sweep's value turned out to be almost
entirely in **things that were silently not running** rather than in defects:

1. **CI has been dead for ten weeks** and nobody could have known from the
   repo — the workflow's trigger never matched, so there was no red tick to
   notice. Fixed and rehearsed.
2. **A pytest marker documented a policy that nothing implemented**, so
   model-loading tests ran on every invocation and made CI unfixable.
   Fixed; suite is 17% faster as a side effect.
3. **The `luxe code` drill protocol was measuring nothing** — worktree
   isolation doesn't isolate imports, so the agent (and, on first pass, I)
   verified a change against the unmodified parent checkout.

## Cards

7 carded, **7 landed**. All Tier A changes cleared the byte-identity gate.
HS-007 was added during the Phase 1.7 manual sweep, after the queue was
first written.

| ID | Title | Tier | Type | Commit |
|----|-------|------|------|--------|
| HS-001 | `live_*` markers now actually skip by default | B | bug | `49de966` |
| HS-002 | Resurrect the CI workflow | C | bug | `3e4a695` |
| HS-003 | No `.sdd` contracts header with nothing under it | **A** | bug | `3153608` |
| HS-004 | Stop shadowing `results` in the bench `finally` | **A** | refactor | `26c7763` |
| HS-005 | Drop unused `os` import | C | bug | `bb49706` |
| HS-006 | Split `cached` from the skip-map-io sentinel | C | refactor | `9f18b0b` |
| HS-007 | Drop dead `_looks_like_url` helper | C | refactor | `74270ac` |

Plus `b0d4eeb` (Phase 0 baselines + golden-request guard) and the docs
commits carrying this report. **10 commits total, all local.**

## The golden-request guard (the durable deliverable)

`tests/test_golden_request.py` + `tests/golden/*.json`. "The benchmark path
stays byte-identical" was previously enforced by care; it is now a test.
It drives a real `Backend` with a stubbed transport and snapshots the exact
HTTP body — model, messages, full OpenAI tools array, every sampling
param — with the role read from the shipped `configs/single_64gb.yaml`.

Verified to guard rather than decorate: perturbing `_BASELINE_SYSTEM` fails
2 tests with a readable one-line diff; perturbing a `read_file` tool
description fails on `tools[0].function.description`. Both reverted.
Intentional changes regenerate with `LUXE_UPDATE_GOLDEN=1`.

It paid for itself immediately — HS-003 changes prompt-assembly code on the
bench path, and the unchanged snapshot is the proof it's behavior-preserving
where it counts.

## Verification

| Gate | Baseline | Final |
|---|---|---|
| pytest | 1877 passed, 6 skipped | **1883 passed, 6 skipped, 4 deselected** |
| ruff (repo) | 223 | **222** (−1, the HS-005 fix; no new findings) |
| ruff (`src/`) | 6 | **5** — exactly the deliberate set RESUME.md documents |
| mypy | 102 errors / 18 files | **95** (−7: 3 from HS-004, 4 from HS-006) |
| bandit | 120 low / 1 med / 2 high | **identical** |
| `luxe smoke` | READY (17s) | **READY (17s)** |
| `luxe smoke --chat --code` | not run | **READY (44s)** — both drills pass |

Test count reconciles exactly: 1877 + 5 golden + 3 marker-policy + 2
spec-resolver = 1887, − 4 deselected = 1883.

## Performance

Only one candidate survived measurement, and it was a bug fix, not an
optimization. 3 runs each, back-to-back under identical load:

| | median wall |
|---|---|
| with live tests (pre-HS-001) | 46.02s |
| default (post-HS-001) | **38.21s** |

**−7.81s, −17.0%.** Past the plan's ≥10% keep rule.

`import luxe.cli` measures ~110ms (pydantic 24ms via `luxe.config`, rich
10.6ms) — irrelevant next to loading a 21 GB model, so no lazy-import work
was done. **`scripts/hygiene_perf.py` was deliberately not built**: the
plan requires "no measurement, no merge" and forbids speculative
optimization, and after measuring there were no optimization candidates
left for a harness to serve. Building one would have been the exact
speculative work the plan rules out. No Tier A perf proposals arose.

## Dogfood: `luxe gitaudit` — abandoned, and it found a bug anyway

Launched at Phase 1 start against this repo. It self-planned **73 chunks /
74 passes / ~287 min**, completed chunk 1, and then **wedged permanently on
chunk 2** — killed at 22:49 (`EXIT=143`) after 40 minutes, 1/73 chunks done.
The plan permits abandoning with a reason; this is the reason.

It wedged because I evicted its weights (B5) with a 30-second `/status` +
`/doctor` spot-check. But the *response* to that eviction is a genuine bug:

**B6 — the request never returned and never timed out.** 23 minutes on a
600s read timeout, no `BackendError`, no retry, no log line, process parked
in `S` on an ESTABLISHED socket. The server was healthy the whole time —
`luxe smoke` was green in 17s immediately after the kill — so this is a
client-side hang in `Backend`, not an oMLX outage.

**Now root-caused** (2026-07-31, `raw_findings.md` § B6): `httpx.Timeout` is
a **per-read** deadline, not a total-request cap, and oMLX emits keepalive
bytes on both response paths — `"model":"keepalive"` SSE chunks when
streaming, and a bare space byte every ~10s under chunked encoding when
not. Every keepalive resets luxe's only clock, so the request can never
time out; `_chat_stream` discards the keepalives silently because their
`content` is `""`. Reproduced on both paths against a synthetic server and
against live oMLX. **The non-stream path is the benchmark/maintain path**,
so an n=75 sweep can wedge on one fixture forever with no error. **Fixed in `1d1724a`** — a progress deadline (keepalives are liveness, not
progress), validated against live oMLX on both paths with the golden-request
snapshot untouched. For a tool whose mission is
being available during an outage, a silent unbounded hang is the worst
available failure shape, and it needs no mistake to trigger: any concurrent
chat `/quit`, admin unload, or oMLX restart mid-turn does it.

So the dogfood produced no findings *of its own* — no card in this sweep
came from gitaudit's output — but running it surfaced two real defects (B5,
B6) that no static analyzer would have.

Product feedback on gitkit itself:

- It is honest about cost up front (`plan: 73 chunks · ~287 min`), which is
  right, but ~5 hours makes it an overnight tool at this repo size.
- **No resume.** 40 minutes of work and chunk 1's findings evaporated on
  kill. The `map/` cache survives (survey + partition), and per-chunk notes
  are cached under `map/notes/<kind>/` — but the incremental path is
  HEAD-keyed and only reuses *validated* chunk notes, so an interrupted run
  restarts the chunk pass. A deep run this long wants to be killable and
  resumable.

## Drill: can luxe fix its own problems?

**1 pass / 2 attempts** — full detail in `drill_log.md`.

- **HS-005 (delete an unused import): PASS.** Minimal, correct diff.
- **HS-006 (split an overloaded local): FAIL.** Produced a plausible,
  well-shaped diff containing an `UnboundLocalError` on the diff-mode path,
  then reported "All 109 tests pass."

The real finding is that **the verification loop was fake**. The venv's
editable install points at the main checkout, so `pytest` run inside a
worktree imports the *parent's* source. The agent's green run was real and
irrelevant; so was mine, until I forced `PYTHONPATH=<worktree>/src` and the
existing test failed immediately. **The plan's drill protocol (§ Phase 3,
steps c–d) is wrong as written** and needs a per-worktree `PYTHONPATH` or
venv before any future drill result means anything.

Honest read at n=2: the local model handles mechanical single-site edits and
is not reliable on control-flow refactors — but that conclusion is
provisional, because the loop that was supposed to catch its mistakes wasn't
pointed at its code.

Secondary: piped multi-line prompts become **one turn per line** in the line
REPL. The README's headless pattern only supports single-line messages.
Not fixed — it's a chat behaviour change, outside this sweep's remit.

## Deferred — all four now addressed (2026-07-31)

| | was | now |
|---|---|---|
| **B5** chat exit evicted every model on the server | open | **fixed** `245ed05` |
| **B2** 18 transitive CVEs | open | **0 vulns** `67cd79f` |
| **B1** `git clone` unbounded | needed a number | **fixed** `1443021` |
| **B3** 95 mypy errors | annotation debt | **96 -> 41** `0685009` |
| **B6** silent unbounded hang | root-caused | **fixed** `1d1724a`, per-endpoint `2586bf1` |

Notes on the judgement calls, since each departed from the filed shape:

- **B5** was filed as "exit unloads everything", but the same mass-eviction
  ran at two *more* places — first-use residency enforcement and every weight
  swap. Fixing only the exit would have left a shared endpoint cleared on the
  first turn of every session. All three are gated on a new
  `BackendEntry.shared` (auto-detected: non-loopback = shared). The asymmetry
  is deliberate: wrongly assuming shared costs RAM we own, wrongly assuming
  owned evicts a colleague's model mid-generation.
- **B2** — unconstrained, the resolver took `mcp` to **2.0.0**, a major bump
  to the client API driving the fleet relays. The CVE is fixed in 1.28.1, so
  pyproject pins `mcp>=1.28.1,<2`: the fix without the migration. Lifting the
  pin should be gated on a real relay drill, not a green suite.
- **B1** — you'd asked for a number; a duration is the wrong knob. A large
  clone over a slow link isn't a failure. The guard is git's own low-speed
  stall detection (under 1 KB/s for 60s), with `timeout=1800` only as a
  backstop, plus `GIT_TERMINAL_PROMPT=0` so a private URL fails instead of
  blocking on a prompt. The two duplicate clone implementations now share
  `luxe.gitclone`.
- **B3** — halved from four root causes, not 55 scattered annotations. A
  `ConsoleLike` Protocol was tried and **reverted**: net +1 error, because
  `rich.Console.is_terminal` is a read-only property. The residual 41 are
  narrowing failures on correctly-guarded code, no cluster larger than 3.

Refuted leads stay in `raw_findings.md` § C so the next sweep doesn't
re-spend time on them — notably the `encoding=` audit (70 sites, refuted
empirically) and the plan's own seed 1.8b (refuted twice).

## If any Tier A file changed — bench validation

`src/luxe/spec_resolver.py` and `benchmarks/maintain_suite/run.py` changed.
Both cleared the golden-request gate, so the champion's request is
byte-identical and a re-bench is *not* expected to move. Offered, not run:

```
python -m benchmarks.maintain_suite.run --variants configs/single_64gb.yaml \
    --work-dir ~/.luxe/bench-workspace
```

## Close-out status

All plan gates cleared.

- **5.1** full suite green, count up from baseline: **1883 passed, 6
  skipped, 4 deselected** (1877 + 10 new tests − 4 deselected).
- **5.2** analyzers diffed against Phase 0: ruff 223 → 222 (one removal,
  no new findings), mypy 102 → 95 (exactly the 7 targeted), bandit
  identical.
- **5.3** `luxe smoke` **READY (17s)**; `luxe smoke --chat --code`
  **READY (44s)** — chat drill recovered the planted magic word (3 steps,
  2 tool calls); code drill fixed the planted bug with pytest green and
  *exactly* `calc.py` changed (7 steps, 9 tool calls). The fallback kit is
  intact after every change in this sweep.
- **5.4** interactive spot-check: `/status` and `/doctor` clean, no
  tracebacks, `doctor` all-clear on 15 checks, index built in 0.9s.
- **5.5** Tier A files changed (`spec_resolver.py`,
  `maintain_suite/run.py`), both byte-identity-gated. Bench command offered
  above, **not run**.
- **5.6** this report, `RESUME.md` handoff, 2 `lessons.md` entries.

Worth noting the contrast with the drill log: `luxe smoke --code`'s planted
bug-fix drill **passed**, while the real HS-006 refactor drill failed. The
smoke drill runs in a standalone scratch repo with no editable-install
shadowing, so its verification is real — which is exactly the property the
worktree drill protocol lacked.
