# SWE-bench Verified miss diagnosis (champion, 2026-11-19)

Question: where does `Qwen3.6-35B-A3B-6bit` lose SWE-bench Verified instances (about 35.7/75 against a 42/75 gate), now that read budget and context levers are ruled out (`../n75_ab_2026_11_06/REPORT.md`, `../oversize_window_2026_11_08/REPORT.md`)?

Data: one control rep over the 46-instance subset `benchmarks/swebench/subsets/diag_misses_2026_11.json` (27 never resolved in 9 prior reps, 19 flaky; the 4 ungradeable requests ids excluded). Result 12/46 resolved, 37 graded, 9 empty patches (`grade.log`, `control/harness/harness_summary.json`). Bench checkout `luxe-bench` at `ac2ff9f`, oMLX 0.8.0, config `configs/single_64gb_swebench.yaml` (`run.sh`). First SWE-bench run with saved conversations (`LUXE_SAVE_MESSAGES`).

Method: `classify.py` (this directory) computes every per-instance mechanical field from `control/<iid>.json`, the harness `report.json` files, `~/.luxe/runs/<run_id>/events.jsonl` and `messages.jsonl.gz`, and the gold patch in `benchmarks/swebench/subsets/raw/verified.jsonl`. Output: `classify.csv` (all columns) and `classify.md` (aggregates with instance ids). Numbers marked [cm] come from `classify.md`; [csv] from `classify.csv`. C/F/D assignments were made by reading the problem statement, the model diff vs the gold diff and the model's last message for 21 of the 25 patched misses (list in section 2).

## 1. Headline

**The agent never sees a single test or reproduction result on its own patch.** In all 46 runs, 0 test-like commands produced output the model could read, and 0 ran before the first write [cm]. The run then ends at the first verification attempt. Localization is mostly fine (23 of 25 patched misses edit a gold file, [csv] `overlap`); the misses are single-shot fixes that are incomplete or break other tests, and nothing in the loop can catch that.

Four independent blockers stack, each verified:

| Blocker | Evidence |
|---|---|
| Validator refuses the commands the model writes | 124 bash calls across 46 runs: 90 refused by the validator, 9 dedup-refused [cm]. 70 commands start with `cd ` (usually a hallucinated `/home/user/repo`), 56 carry a pipe or `2>&1` [cm]. 11 multi-line `python -c` reproductions refused as "Unquoted newline" (message tally over `messages.jsonl.gz`). |
| Output of a command that ran with a nonzero exit is dropped | `src/luxe/agents/loop.py:1496` `content = executed.error or executed.result`; `tools/shell.py:435` returns `(output, "exit code N")`, so the model sees only `exit code N`. 18 bash calls ran, produced 82 to 2289 bytes (`tool_call.bytes_out` in events), and showed the model only `exit code N` [cm, `bash_ran_output_hidden`]. The 7 visible results were `ast.parse`, two `git show`, one `find`, one `ls` (none a test). The comment at `tools/shell.py:244-250` ("a non-zero exit is reported while the output is still returned") is not true at the loop level. In the code since `7332da1` (v1.0). |
| Host environment cannot import the target repos | Tests run with the bench venv on the host (Python 3.15). Re-running the model's own command for `psf__requests-2931` (`python -m pytest test_requests.py -x -q --tb=short`) in its workspace: collection error, `ModuleNotFoundError: No module named 'cgi'`. |
| Reproducer files are forbidden | The adapter injects an `.sdd` whose Forbids include `test_*.py`, `**/test_*.py`, `verify_*.py`, `tmp_*.py` (`benchmarks/swebench/adapter.py` `SWEBENCH_SDD_BODY`). The model edits or creates tests in 0 misses [cm]. |

Consequence: 34 of 46 runs end by `post_write_idle_exit`, 32 of them right after exactly three consecutive bash calls (20 misses, all 12 resolved) [cm]. Median wall 38.5 s (misses) and 32 s (resolved) against an 1800 s per-instance timeout; median 18 and 16 steps of 30 (`classify.csv` `wall_s`, `steps`).

## 2. Buckets (34 misses)

| Class | n | Instance ids |
|---|---|---|
| A no patch | 9 | habituation_exit at step 20: matplotlib-20826, matplotlib-25775, sphinx-10449. Max steps (30) without a write: django-11734, matplotlib-20676. Stuck loop on a duplicate `grep`: seaborn-3069, pylint-4604. Truncated then empty terminal turns (205 s, 221 s): requests-6028, sphinx-10323. |
| B wrong location | 2 | matplotlib-25479 (model `pyplot.py`, gold `cm.py`), pylint-6386 (model `callback_actions.py`, gold `argument.py` + `arguments_manager.py`) |
| C right file, wrong or incomplete fix | 17 | astropy-13033, astropy-13453, astropy-13977, astropy-14182, django-10999, matplotlib-24870, seaborn-3187, xarray-6938, pylint-4661, pylint-6528, pytest-10051, pytest-5840, sphinx-10435, sphinx-10614, sphinx-10673, sphinx-7462, sympy-13091 |
| D fix right, broke other tests | 3 | requests-2931 (1 P2P), requests-5414 (8 P2P), astropy-13236 (core hunk identical to gold, but the model also deleted `from .ndarray_mixin import NdarrayMixin`; grading `test_output.txt`: `ImportError: cannot import name 'NdarrayMixin' from 'astropy.table.table'`, 644 P2P failures) |
| E environment or grading | 0 | Every graded patch applied (`patch_applied` [csv]); no harness errors (`grade.log`) |
| F problem misunderstanding | 3 | astropy-14096 (issue wants the real missing attribute surfaced; model raises a new message that still hides it), scikit-learn-12682 (issue asks `SparseCoder` to expose `max_iter`; model only threads it through `_sparse_encode`, says "The fix is complete"), sympy-17318 (patches the `IndexError` site in `split_surds`; gold changes the `_sqrt_match` condition) |

A, B and D-by-P2P are mechanical [cm]. C and F were read for: astropy-13033, -13453, -13977, -14096, -14182, -13236, django-10999, matplotlib-24870, seaborn-3187, xarray-6938, pylint-4661, -6528, pytest-10051, -5840, scikit-learn-12682, sphinx-10435, -10614, -7462, sympy-17318, requests-2931, -5414 (21). sphinx-10673 and sympy-13091 are C by the mechanical test only (gold file, nearby hunks, F2P failing) and were not read.

Representative C cases: astropy-13453 adds `_set_col_formats()` but misses `self.data.cols = cols` (also 7 P2P regressions); astropy-14182 adds `header_rows` to `RST.__init__` but not the `write()` line indexing; django-10999 applies the lookahead change the issue text itself suggests, while the gold rewrites the sign handling; pylint-4661 hardcodes `~/.cache/pylint` where the tests expect `appdirs.user_cache_dir`; astropy-13033 builds the right message in a different format than the test asserts.

## 3. Cross-cutting patterns

| Pattern | Misses | Resolved | Source |
|---|---|---|---|
| Ran a test or repro before the first write | 0/34 | 0/12 | [cm] |
| A test-like command actually executed (any time) | 11/34, every one after the first write, output hidden or an import failure | 4/12 | [cm] |
| Command named the right F2P test file | 9: astropy-13453, -14182, matplotlib-24870, requests-2931, scikit-learn-12682, sphinx-10435, -10614, -10673, -7462 | | [cm] |
| Command named the file holding its own P2P regression | 2: astropy-13453, requests-2931 | | [cm] |
| Patch causes P2P regressions | 9: astropy-13236, -13453, -13977, seaborn-3187, requests-2931, -5414, pylint-6528, pytest-10051, sympy-13091 | | [cm] |
| Exactly one write, then exit | 16 | 6 | [cm] |
| Fewer hunks than gold (patched runs) | 17/25 | 4/12 | [cm] |
| early_bail fired (median first step 4) | 30/34 | 9/12 | [cm], [csv] |
| Edited or created tests | 0 | 0 | [cm] |
| Misread the issue (F) | 3/34 | | section 2 |

What the resolved 12 did differently: nothing behavioral. Same exit (all 12 by idle exit after three bash calls), same early_bail step, same unverified single pass. Their gold patches are smaller (median 1.5 gold hunks vs 2.5 for misses, [csv] `gold_hunks`), and 9 of 12 had prior records of 6/9 or better (`per_instance` in the subset file). Resolution currently tracks whether the first plausible edit is the whole fix.

## 4. Levers (ranked; nothing implemented)

| # | Lever | Targets | Plausible flips (this subset) | Cost |
|---|---|---|---|---|
| 1 | Open the verification channel on the bench path: (a) return output with the exit code on a nonzero exit (`loop.py:1496`); (b) run model bash in the per-instance SWE-bench image (37 already built for grading) or an equivalent per-instance env; (c) accept a leading `cd <repo-root> &&` and the existing `LUXE_BASH_NOOP=strip` suffixes; (d) allow scratch reproducers outside the diff (drop the anti-reproducer Forbids, strip untracked scratch before `extract_diff`) | C, D | 4 to 8. Strongest: the 3 D (a run of the test file the model already tried catches requests-2931; any import catches astropy-13236), the C cases with P2P regressions (astropy-13453, -13977, pylint-6528, pytest-10051, seaborn-3187, sympy-13091), and C cases whose issue text is a runnable repro (astropy-14182, matplotlib-24870, sphinx-7462, django-10999) | High for (b) and (d); (a) is one line but changes model-visible content on every path that shares the loop, so it needs its own maintain_suite no-harm run |
| 2 | Drop the SWE-bench commit-pressure stack once verification exists (early_bail soft_anchor/commit_imperative, write_pressure, action_density, convergence gate; habituation_exit only fires after these) | A (3 habituation exits), C (single-shot) | 0 to 3; sphinx-10449 (7/9 prior, lost to habituation_exit) is the clearest. It was tuned in v1.7 to v1.10 against empty patches, which are now 9/34 | Cheap: existing `benchmarks.swebench.run` flags `--no-early-bail --no-write-pressure --no-action-density-gate` |
| 3 | `max_steps` 30 to 60 in a config copy | A max-step (django-11734, matplotlib-20676), sympy-13091 (C, hit 30 steps with 6 writes) | 0 to 1 alone; more if stacked on lever 1, where verification turns consume steps | Cheap: one YAML key |

Note on prior A/Bs: `bash_noop_ab_2026_11` (strip/hint inert) and `idle_capped_ab_2026_11` (wide idle-narrow) were measured on maintain_suite with the hidden-output behavior in place. Any arm whose benefit depends on the model reading a failing command's output was confounded by it; those verdicts should not be carried over to a post-fix world without a re-run.

### How to A/B cheaply

1. Feasibility probe before any model run (minutes, no model): for each of the 46 instances run the gold F2P test file in (i) the host bench venv and (ii) the instance's SWE-bench image with the workspace repo mounted, and record whether collection succeeds. This decides between (b) options.
2. Lever 1 slice: the 12 C/D instances named in the table above plus the 12 resolved as the no-harm arm, 3 reps, control vs arm. Count successes as flips against the 9-rep prior record and regressions on the resolved 12 (at most 1 net loss). Read every new PASS by hand.
3. Lever 2: the same 24 instances plus the 3 habituation-exit A instances, as a second arm stacked on lever 1.
4. Promotion still requires the n=75 x 3 run against the 42/75 gate.

## 5. Files

- `classify.py`: the classifier (read-only over the inputs; rerun with `python3 classify.py`)
- `classify.csv`, `classify.md`: its output
- `REPORT.md`: this report
