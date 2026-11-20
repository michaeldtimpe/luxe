# Cleanup turn: catching data-file litter (2026-11-20, m5)

**Target.** Under `LUXE_IDLE_NARROW=wide` the champion committed `test_pe_scan/fake.gguf` in 3/3
lpe-rope-calc-implement-strict-flag runs (`../idle_wide_rerun_2026_11/REPORT.md`). Each check below is
3 reps of that fixture under the arm that produced it (`LUXE_FAILED_OUTPUT=1 LUXE_IDLE_NARROW=wide`),
oMLX 0.8.0, deterministic (identical tokens across reps within each check).

## Result: fixed. Strict-flag passes 3/3 and the patch is `pe_scan.py` only.

| label | code | cleanup candidates | model reply | deleted | patch files | outcome |
|---|---|---|---|---|---|---|
| (idle re-run) | 5f05acf | `verify_strict.py` | `DELETE verify_strict.py` | verify_strict.py | pe_scan.py, fake.gguf | PASS, litter |
| `champ` | cfc3844 (#65 data candidates) | fake.gguf, verify_strict.py | `DELETE verify_strict.py -- scratch…` / `KEEP fake.gguf -- test fixture…` | **none** | pe_scan.py, fake.gguf, verify_strict.py | **FAIL** (orphan_file) x3 |
| `fix66` | 09c9ca7 (#66 reason allowed) | same | same | verify_strict.py | pe_scan.py, fake.gguf | PASS, litter x3 |
| `prompt` | cd6a47f (prompt fix on 09c9ca7) | same | `DELETE fake.gguf` / `DELETE verify_strict.py` | **fake.gguf only** | pe_scan.py, verify_strict.py | **FAIL** (orphan_file) |
| `prompt2` | 5f6d653 (#67 + #68) | same | `DELETE fake.gguf` / `DELETE verify_strict.py` | both | **pe_scan.py** | **PASS x3** |

## What each step found

1. **#65 (cfc3844): data files become candidates.** Detection worked, and `fake.gguf` was offered. But once
   the model had a KEEP with a reason to write, it put `-- reason` on its DELETE line too, and the parser
   (`^…DELETE path\s*$`) silently dropped it. That exposed a latent bug: 3 of 2,422 historical cleanup
   turns had lost an intended DELETE the same way. Here it left `verify_strict.py` and failed strict-flag.
2. **#66 (09c9ca7): a DELETE line may carry a reason.** PASS restored. The model still KEPT `fake.gguf` as
   "a test fixture for --strict flag behavior" although nothing reads it.
3. **#68 (5f6d653): one sentence in the cleanup system prompt** says an unread data file is scratch, not a
   fixture. The model then DELETEd both files, which exposed a regression in #66: its trailing group used
   `\s`, which matched the newline, so the first DELETE line swallowed the next and only one file was
   deleted.
4. **#67 (fb06c80): horizontal whitespace only.** Replayed over all 96 historical replies with text:
   0 mismatches. With #67 + #68, PASS 3/3 and the litter is gone.

## No-harm

- Candidate selection replayed over 553 distinct past maintain_suite patches: the data kind fires on 3,
  all strict-flag litter (`fake.gguf`, `test_bad.gguf`, `pe_scan.py.bak`), and on no doc/config task.
- Shipped defaults: 0 cleanup candidates in all 60 runs of `../baseline_080_2026_11/`, so the prompt
  change triggers no model call there.
