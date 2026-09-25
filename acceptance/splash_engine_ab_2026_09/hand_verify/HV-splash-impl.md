# Hand-verification — splash_engine_ab_2026_09 / splash4 / IMPLEMENT fixtures

**Subject:** `mono__splash-35b-a3b-4bit` (incoai/Qwen3.6-35B-A3B-Splash via Splash
engine), reps 1–3, printed 4/5 (=pass) on all 4 IMPLEMENT fixtures × 3 reps = 12/12.
**Method:** every diff read by hand from `origin/luxe/implement/<slug>-<n>` (local
branch refs for the two older reps per fixture had been reset to base_sha by
`_prune_old_branches`/reuse — origin always retained the real diff; verified with
`git diff --stat <base_sha> <local>` vs `... origin/<local>` before trusting any
ref), judged against the fixture goal text in `benchmarks/maintain_suite/fixtures.yaml`,
not the regex/tests_pass grader. Verified read-only from m5, `~/.luxe/bench-workspace`
+ `~/.luxe/runs/<run_id>/pr_state.json` for branch names.

## VERDICT

**11 REAL, 1 THIN, 0 VACUOUS, 0 DAMAGING** (12/12 hand-verified).

| Fixture | Rep | Verdict | Diff stat | Reason | Tests |
|---|---|---|---|---|---|
| lpe-rope-calc-implement-strict-flag | 1 | REAL | pe_scan.py +34/-13 | `--strict` registered, gates exit code via `bad_gguf_count`, default (non-strict) path unchanged, wired into `scan_ollama`/`scan_gguf_dir`/`scan_lmstudio`/`main()` | no suite; py_compile OK, smoke-ran `--no-default-paths --strict` → rc=0 |
| lpe-rope-calc-implement-strict-flag | 2 | REAL | identical to rep1 (byte-for-byte) | same | same |
| lpe-rope-calc-implement-strict-flag | 3 | REAL | identical to rep1 | same | same |
| the-game-implement-shuffle-shortcut | 1 | REAL | src/App.jsx +11/-0 | new `useEffect` binds `keydown` once, calls existing `shuffle()`, skips INPUT/TEXTAREA/contentEditable per goal | `npm test`→rc=1, but **pre-existing**: package.json has no `test` script at base_sha either (confirmed 0-line diff on package.json) — not a regression |
| the-game-implement-shuffle-shortcut | 2 | REAL | identical to rep1 | same | same (structural, not caused by diff) |
| the-game-implement-shuffle-shortcut | 3 | REAL | identical to rep1 | same | same |
| neon-rain-implement-reset-shortcut | 1 | REAL | Game.js +1, HtmlInputHandler.js +10/... | wires `keydown`→`eventBus.emit('game:restart')` in the **existing** `src/input/HtmlInputHandler.js`, Game.js shows a confirm modal then `_startNewGame()` (minor UX deviation from literal "restarts", still no reload) | `npm test` rc=0, 3/3 personas won (captured in pr_state.json); agent **aborted "stuck in loop"** after the diff/test/push had already completed — diff unaffected |
| neon-rain-implement-reset-shortcut | 2 | REAL | Game.js +1, HtmlInputHandler.js +10 | cleaner: direct `game:reset-run`→`_startNewGame()`, same input-handler wiring, no confirm dialog | `npm test` rc=0, 3/3 personas won |
| neon-rain-implement-reset-shortcut | 3 | **THIN** | Game.js +13 (single file) | functionally restarts on Shift+R and **npm test rc=0**, but goal explicitly says "Use the existing input system in `src/input/`" — this rep bypasses `HtmlInputHandler.js` entirely and adds a raw `document.addEventListener('keydown', …)` inline in `Game.js`'s constructor with no `dispose`/removal path. Grader (`tests_pass`) cannot see the architecture miss. | `npm test` rc=0 (per pr_state.json; not independently re-run — no `node_modules` in worktree, would need `npm install`) |
| isomer-implement-healthcheck | 1 | REAL | app.py +7/-0 | `@app.route("/health")` on the existing `app` instance, `@csrf.exempt`, returns `jsonify({"status":"ok"}), 200`; `csrf`/`jsonify` both already in scope above the insertion point | no suite; py_compile OK (flask not installed on m5 system python, so full Flask test-client run skipped as not-cheap) |
| isomer-implement-healthcheck | 2 | REAL | identical to rep1 | same | same |
| isomer-implement-healthcheck | 3 | REAL | identical to rep1 | same | same |

**Tally: 12/12 hand-verified — 11 REAL, 1 THIN, 0 VACUOUS, 0 DAMAGING.**

## Notable observations

- Every rep of every fixture except neon-rain reproduced a **byte-identical diff**
  (temp=0 determinism holds even across separate bench reps on this engine).
- The only real defect found: neon-rain rep3 ignored an explicit architectural
  instruction ("use the existing input system") — functionally fine, tests pass,
  but the grader has zero way to catch it since `tests_pass` only checks `npm test`.
- neon-rain rep1's agent loop **aborted with "stuck in loop"** (`abort_reason`
  in diagnostics.json) yet still ended up with a complete, tested, pushed diff —
  the abort happened after the useful work, not instead of it.
- `test_passed: False` on all 3 the-game reps is **not a model failure**: the
  fixture repo has no `test` script in package.json at base_sha (confirmed via
  0-line `git diff` on package.json), so `npm test` always exits 1 there,
  independent of the diff's correctness. Don't read this diagnostics field for
  the-game as a signal.
- `gates_triggered: []` on all 12 — consistent with clean runs, no orphan-file
  or forbids_create violations detected anywhere.
- No PR was opened on any run (`gh pr create` fails: "none of the git remotes
  point to a known GitHub host") — this is the fixed 1-point deduction behind
  every printed 4/5 score, unrelated to engine/model quality.
- Local branch refs for the two non-current reps per fixture were reset to
  base_sha in the workspace clone (retention/reuse artifact) while `origin`
  kept the real commits — always diff against `origin/<branch>`, not the local
  ref, when re-verifying this run.
