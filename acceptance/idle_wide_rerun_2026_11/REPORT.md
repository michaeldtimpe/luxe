# LUXE_IDLE_NARROW=wide re-run with failing output visible (2026-11-20, m5)

**Question.** `abort_report_ab_2026_11` declined to promote `wide` (one REAL short, +34% prompt tokens,
a litter file in 3/3 strict-flag runs). That A/B ran while a failing command's output was hidden from
the model (lessons.md 2026-11-20). Does the verdict change once the output is visible?

**Run.** Champion, oMLX 0.8.0, bench `luxe-bench` at 5f05acf, 10 fixtures x 3 reps interleaved. Both
arms have `LUXE_FAILED_OUTPUT=1`: `ctl` = that alone, `wide` = + `LUXE_IDLE_NARROW=wide`. Warm-up
passed (rc=1 is the one-fixture v1-release exit). 14:37 to 15:12, every arm rc=0. `analyze.py champ`
(`runs.json`). Document-config scored with `scripts/doc_config_envcheck.py`.

## Verdict: same as before. Do not promote `wide`.

Visible output did not change the trade. Wide still converts idle exits into model-written reports
(9 to 1 runs without one), and the abort-report fallback in ctl already covers that need. Wide still
pays +33% prompt tokens and still commits the same litter file. Hand-verified quality is equal within
one run, now in wide's favour (27 vs 26) where last time it was against (26 vs 27).

| | ctl | wide | source |
|---|---|---|---|
| printed pass | 30/30 | 30/30 | analyze |
| hand-verified REAL | 26 | 27 | below |
| post-write idle exits | 9 | 0 | analyze `pwi_exit` |
| runs with no MODEL report (fallback written) | 9 | 1 | `empty_report` |
| failing output shown to the model | 1 | 6 | `fo_shown` |
| stuck-loop aborts | 0 | 1 (shuffle r3; patch identical to ctl, fallback written) | `stuck_abort` |
| wall, sum | 969 s | 1049 s (+8%) | `wall_sum` |
| prompt tokens | 2.90 M | 3.86 M (+33%) | `ptok_M` |
| litter committed | 0 | `test_pe_scan/fake.gguf` in 3/3 strict-flag | diff.patch |

## Hand verification (patches that differ between runs)

- **lpe-rope-calc-implement-strict-flag.** ctl `7daa217e67` x3, wide `400bed02fb` x3. Executed with
  `../thinkingcap_4bit_2026_11/hv/strict_probe.py`. Both arms behave identically: `--strict` exits 1 on
  bad magic, old version, and a short header; exits 0 on a truncated value (the partial-metadata path);
  default exit 0 everywhere. REAL with the same gap in both. Wide spends 27 steps (ctl 18) and 5
  verification calls (ctl 1) to reach the same code, and commits `test_pe_scan/fake.gguf`. The cleanup
  turn does not offer it: it only considers code/test files, and a `.gguf` is neither.
- **neon-rain-implement-reset-shortcut.** All six bind `keydown` on `document` (the old board-focus
  defect does not recur) and restart via `_startNewGame()`, the same call the existing `__restart` path
  makes (`Game.js:292`). **ctl rep 1 is THIN:** it wires the key directly in `Game.js` and never touches
  `src/input/`, against the goal's "use the existing input system in src/input/". ctl 2/3 and wide 1-3
  route through `HtmlInputHandler`. Nit (wide 1-3, ctl 2/3): no `PLAYING` guard, so Shift+R on the menu
  starts a game. Pre-existing restart wiring has the same shape.
- **nothing-ever-happens-document-config.** 32/55 in all six (all 23 `PM_NH_*` missing): THIN in both
  arms. This is the oMLX 0.8.0 effect in `../doc_config_rescore_2026_11/DIAGNOSIS.md`, not an arm effect.
- The other 7 fixtures produced one patch per arm, the same in both arms, REAL (unchanged from the
  failed_output / abort_report reads of the same patches).

Totals: ctl 30 - 3 (doc-config) - 1 (neon r1) = **26**; wide 30 - 3 (doc-config) = **27**.

## What would change the answer

- The litter is the cleanest objection and is fixable on its own: offer non-code files the run ADDED
  under a test-looking directory to the cleanup turn. Independent of `wide`.
- The +33% tokens buy model-written reports the fallback already provides, plus extra verification
  calls that changed no outcome here. On SWE-bench, where verification can flip outcomes (see the
  image-exec slice), `wide` might earn its cost. That would be a separate arm there, not a
  maintain_suite default.
