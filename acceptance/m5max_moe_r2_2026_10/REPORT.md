# m5max_moe_r2 — REPORT (2026-10-15 → 10-17, m5)

**Verdict: nothing changes.** No challenger displaces the champion, the m5 dense
fallback, or either m1/m4 4-bit model. Every elimination recurs in ≥2 of 3 runs,
and neither harness lever (step budget, compaction) converts a single challenger
failure. One open decision for the user (m1/m4 main vs fallback, §3).

Evidence: `HAND-VERIFY.md` (every printed pass read, strict-flag executed,
doc-config checked against the source), `PLAN.md` (phases, rules).
Raw outputs on m5 under this directory (`r1*`, `r2*`, `h1_steps60`,
`h2_nocompact`, `determinism`). Grading tools: `m5:~/luxe-bench-tmp/r2/`
(`collect.py`, `attrcheck.py`, `envcheck.py`, `strict_test.sh`, `diverge.py`).

Grades: **REAL** (works, correct against base) · THIN (materially incomplete) ·
VACUOUS (grader satisfied, feature does not work) · BROKEN (leaves the repo
worse than base) · FAIL (no usable output). Advance bar ≥ 9 REAL / 10.

## 1. Champion — Qwen3.6-35B-A3B-6bit stays

| arm (6-bit) | REAL | defect that recurs (≥2 of 3) | avg wall / 10 fixtures |
|---|---|---|---|
| **Qwen3.6-35B-A3B (champion)** | **38 / 40** (R1 + 3 reps) | doc-config only (variance: 9 REAL / 16 across every run) | **8.1 min** |
| K2-Horizon-MoVA-36B-A4B (self-converted) | ≤ 8 every run | strict-flag BROKEN (`scan_lmstudio()` TypeError on every bare run) 2/3 + 1 VACUOUS; neon reset FAIL 3/3 | 18.0 min |
| LFM2.5-24B-A2B | ≤ 6 every run | document-typing BROKEN 3/3 (byte-identical `from io import BinaryIO` → ImportError); neon non-REAL 3/3 | 11.6 min |
| Laguna-XS-2.1 | ≤ 8 every run | neon non-REAL 3/3 (+ systematic `addEventListener`→`addEventlistener` rename that crashes the page); doc-config FAIL | 9.2 min |

None reaches parity, and none is faster than the champion. The bar
(≥ parity AND a material wall win) is missed on both counts.

## 2. m5 dense fallback — Qwen3.6-27B-6bit stays

| arm (6-bit) | REAL / 40 | neon reset | doc-config | avg wall |
|---|---|---|---|---|
| **Qwen3.6-27B (fallback)** | **37** | 4/4 | 1/4 (omits the 23 `PM_NH_*`) | **29.0 min** |
| muse-glimmer-30B | 34 | **0/4** (never writes; also 0/6 at 60 steps / no compaction) | 2/4 | 47.5 min |

Glimmer wrote the best single config doc of the round (R1), but its neon failure
is total and it is 1.6× slower. Eliminated under the 3-rep rule.

## 3. m1/m4 (4-bit) — no challenger; one decision for the user

| arm (4-bit, run on m5) | REAL / 30 | recurring defect | avg wall |
|---|---|---|---|
| **Qwen3.6-27B-4bit (current FALLBACK)** | **30** | none | 34.9 min |
| Qwen3.6-35B-A3B-4bit (current MAIN) | 23 | neon VACUOUS 3/3, byte-identical (`e.key === 'r'` under Shift); doc-config THIN 3/3 | 10.1 min |
| K2-4bit | 23 | strict-flag 3/3 (2 crash code that worked); neon FAIL 3/3 | 17.8 min |
| LFM2.5-4bit | ≤ 21 | strict-flag BROKEN 2/3; neon non-REAL 3/3 | 12.7 min |
| Laguna-4bit | ≤ 19 | neon / shuffle / doc-config 3/3 | 7.2 min |
| Glimmer-4bit | ≤ 8 / 10 every run | neon FAIL 3/3 | 27.6 min |

K2-4bit ties the MAIN on count, but its defect is worse (it breaks working code;
the MAIN's is a feature that silently does nothing), and it is 1.8× slower.

**The decision (user's, no change made):** 4-bit costs the 35B MoE real quality
that 6-bit does not (6-bit neon 4/4 REAL; 4-bit 0/3), while the 27B-4bit loses
nothing — at 3.5× the wall. The 2026-07-30 MoE-first flip bought chat latency.
Options: (a) keep as is; (b) swap main and fallback; (c) 27B first for
`luxe code` only, MoE stays main for `luxe chat` — the recommendation, since
code is where the MoE's 4-bit defects land and chat is where its latency matters.

## 4. Was it the model? (user's questions)

- **Tool calls / plumbing:** zero `tool_reject`, zero `textfallback_drop` in every
  run. All arms passed the plumbing preflight. Failures are behaviour.
- **The harness (P4c, 3 reps × 3 hard fixtures × all six 6-bit arms):** doubling
  `max_steps` to 60, or turning TieredCompact off, converts nothing. Champion 17/18,
  27B 14/18; every challenger ≤ 10/18 with its failure mode unchanged (Glimmer
  still never writes neon; K2 now writes it, with the wrong key; Laguna repeats
  the casing rename; LFM omits the same 25 variables, byte-identically).
- **Variance (3-rep rule):** every elimination recurs. One earlier call was
  retracted as variance (27B doc-config "systematic THIN").
- **Determinism (P4e):** the persistent oMLX prefix cache is **not** why reps
  diverge. Cold reps (fresh empty cache + server restart each) diverge from each
  other at step 3 — as early as warm reps — with byte-identical prompts and
  identical cache-hit counts. Greedy decode on the server is itself
  non-deterministic at near-ties; no cache setting makes runs reproducible, so
  the 3-rep rule stays and cold-cache runs are not worth their cost.

## 5. Things the round got wrong first (and fixed)

- **Branch-name reuse mis-attributed diffs** (14 → 30 of 310): bench branch
  names are first-free, so `base..<branch>` can show another run's commit.
  `collect.py` now resolves each run's commit from the clone's reflog;
  `attrcheck.py` verifies every diff against the run-time stat (0 mismatches on
  every collection after the fix). No grade changed.
- **`envcheck.py` only counted backticked names** — docs listing variables in
  bold were under-credited. Fixed, every doc-config diff re-checked; no grade
  changed.
- **Printed passes over-credit** (again): every VACUOUS grade and most BROKEN
  grades in HAND-VERIFY.md were printed passes with `gates_triggered` empty — e.g. K2's
  strict-flag TypeError, LFM's ImportError, Laguna's listener rename, and the
  35B-4bit's `'r'`-under-Shift shortcut.
- **`bailout_type: context_overflow` is a mislabel** for "max steps reached"
  (`run.py` `_classify_bailout`); peak context pressure in those runs was ~0.55.
  Not fixed here (changing it breaks comparability with history) — a follow-up.

## 6. Not done / follow-ups

- **P6 long smoke tests** were scoped to new survivors; there are none, so they
  were not run. The incumbents are already covered by `luxe smoke`.
- **Weights cleanup (waiting for the user's OK):** delete from m5 the losers' weights
  (K2-6bit + 4-bit self-conversions, the K2 bf16 checkpoint, Laguna 6/4,
  LFM2.5 6/4, Glimmer 6/4). Gemma 4 is kept. The incumbents' weights are untouched.
- `bailout_type` relabel (above), if wanted.
