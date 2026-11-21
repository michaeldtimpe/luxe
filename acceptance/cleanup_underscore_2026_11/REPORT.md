# Cleanup turn and orphan_file gate: only dunder stems skip the reference test (2026-11-21, m5)

**Target.** `_test_health.py` was committed in 10 of 12 isomer-implement-healthcheck runs across the two
0.8.0 baseline sessions (`../baseline_080_2026_11/REPORT.md` section C). `cleanup.find_candidates` and the
`orphan_file` gate (`benchmarks/maintain_suite/gates.py`) both skipped any code file whose stem starts with
`_`, an exemption meant for `__init__.py`-style files. Fix (f270dd8, PR #70): only dunder stems
(`__init__`, `__main__`) skip the reference test, in both places, with unit tests for each.

**Live check.** Champion, oMLX 0.8.0, shipped defaults, bench at f270dd8, healthcheck x 3
(`queue.sh`, output in `fix/`), 2026-11-21 05:21 to 05:23.

## Result: fixed. `_test_health.py` is offered and deleted in 3/3; the patch is `app.py` only.

| rep | cleanup turn | patch files | printed | outcome |
|---|---|---|---|---|
| 1 | 1 candidate, deleted `_test_health.py` | app.py | PASS 4/5 | REAL |
| 2 | same | app.py | PASS 4/5 | REAL |
| 3 | same | app.py | PASS 4/5 | REAL |

- The three patches are byte-identical (`diff.patch` sha e020366dff), so this is one trajectory, n=1 by
  the in-session determinism rule. It is the trajectory that wrote the scratch file, which is the case the
  fix targets.
- The route is the known one: `@app.route("/health")` returning `jsonify({"status": "ok"}), 200` on the
  existing app.
- 4/5 is this fixture's ceiling on the bench: `pr_opened` always fails because the bench clone has no
  GitHub remote (the 0.8.0 baseline healthcheck runs score 4/5 the same way). The bench exits 1 because no
  cell reaches the v1 release bar, which a single-fixture run never does.
- No-harm: the candidate-selection replay over 618 past maintain_suite patches (done with the fix) offered
  nothing beyond `_`-stem scratch.
