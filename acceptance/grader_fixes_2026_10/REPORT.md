# grader_fixes_2026_10 — two printed false failures

Source: the harness_ab_2026_10 hand-verify (`../harness_ab_2026_10/REPORT.md`,
"Fairness and isolation caveats"). Hand-verify graded 21 of that round's doc
runs REAL while `grade.py` printed 1/5 for them; a 22nd, from the superseded
pilot, has the same shape. There were two separate causes.
Both fixes are grader-only: no model was run, and every number below comes
from regrading diffs already on disk.

## The two bugs

**1. `destructive_diff` prong 3 read shell comments as markdown headings**
(2 runs: codex/q38 r1 and codex-cat/q38 r2, isomer-document-quickstart).
The README's Quick Start is a ```bash block whose comments include
`# First-time setup: create a .env with a real secret.`. Both runs kept every
command, including the ISOMER_SECRET setup, and split the block into numbered
steps. `_headings()` matched the deleted comment as an ATX heading. It found
no retitle among the added headings and fired "section deleted".

*Fix:* prong 3 keeps only titles that are real headings, outside fenced code,
in the whole file at that side of the diff (`_doc_headings`,
`_unfenced_headings`). It reads the whole file because a hunk can open
mid-fence. If git can't read a side (the file was added, deleted or renamed),
that side keeps the old reading. The filter only removes candidates, so it
can never add a firing.

**2. `min_added_lines` measured line wrapping, not content**
(19 graded runs plus the pilot, almost all Glimmer, on
isomer-document-quickstart (floor 4) and the-game-document-architecture
(floor 8)). An unwrapped markdown paragraph
is one physical line. Glimmer's quickstart diffs were 48–71 words in 1–2
lines, about the word count of the champion's 4–10-line passes. Its
architecture diffs were 94–149 words in 4–7 lines against a floor of 8.

*Fix:* a line added to a markdown file counts as `ceil(len/80)` lines (an
empty line counts as 1). Code lines always count 1. A count can only go up,
so no earlier pass can flip, and a heading rename or a one-liner is under 80
columns and still counts as 1. A per-fixture word floor was rejected: it
would mean new numbers tuned on these outputs, and it would change earlier
scores in both directions.

## Audit 1 — prong 3 over this repo's history (`audit_commits.py`)

Prong 3 (old vs new) was run on every non-merge commit in this repo's
history, as task_type=document. Raw output: `audit_commits_out.txt`.

| commits | touch markdown | old firings | new firings | dropped | added |
|---|---|---|---|---|---|
| 719 | 321 | 17 | 16 | 1 | 0 |

The one dropped firing is `92ceb4cb`. The "section" `Inspector — the hard
new_file_in_diff blocker` is line 117 of RESUME.md, inside a ```bash fence
(lines 104–123). It is the same false positive. The other 16 human doc
restructures prong 3 was calibrated on still fire.

## Audit 2 — regrade of every collected fixture diff (`regrade.py`, on m5)

Every collected diff in `harness_ab_2026_10` (`diff.patch`) and in
`m5max_moe_r2_2026_10` + `tuning_2026_10` (`_review/*.diff`) was applied to
its fixture's `base_sha` and graded with the `origin/main` grade.py and the
fixed one. It covered every fixture whose outcome is `regex_present` or whose
task is document/manage, since nothing else can be touched by either change.
`.pyc` hunks were excluded (binary patches without a full index line).
Raw output: `regrade_out.txt`.

- **950 diffs graded.** 20 were empty, and 36 couldn't be applied: 33 are
  header-only no-write runs, and 3 Glimmer-4bit typing diffs don't apply to
  the base.
- **29 printed verdicts changed. All 29 went FAIL → PASS. No PASS became a
  FAIL.**

| source | changed | cause | hand grade |
|---|---|---|---|
| harness_ab (graded rounds) | 2 | prong 3, fenced comment | REAL ×2 (HAND-VERIFY lines 134, 180) |
| harness_ab (graded rounds) | 18 | line floor | REAL ×18 (Glimmer sections; several "minimal") |
| harness_ab `_pilot_online` | 1 | line floor (6 lines, 2 paragraphs) | not graded individually (pilot superseded); diff read here: names `shuffle`, `useEffect`, `GET /api/shuffle` |
| m5max_moe_r2 | 8 | line floor | REAL ×8: Glimmer-6bit quickstart R1 ("REAL (printed FAIL)") + R2 reps 1–3; Glimmer-4bit architecture R1/rep2/rep3 ("all four REAL"); K2-4bit quickstart rep2 ("REAL (minimal…; printed FAIL = line floor)") |

Of the 21 graded harness_ab false failures, 20 now print PASS (2 prong 3 +
18 line floor), and so does the pilot run. The one left is claude/glim r2 quickstart: 3 short lines and 34
words, graded "R (minimal)". It is genuinely under a 4-line floor, so it stays
a printed FAIL.

**Printed numbers that move:** these changes raise printed doc-fixture scores
for models that write unwrapped prose (Glimmer, K2), and for the two
quickstart runs above. **The champion's printed numbers don't move in any
graded round.** Its only changed diff is the superseded harness_ab pilot run,
under an outside harness. No luxe champion run in m5max_moe_r2 or
tuning_2026_10 changed.

## Reproduce

```bash
# m5: old/ and new/ each hold benchmarks/maintain_suite/grade.py (+ __init__.py)
cd ~/luxe-bench-tmp/grader_fix && ~/Downloads/luxe/.venv/bin/python regrade.py
# any host, from a checkout of the fixed grader:
PYTHONPATH=. python acceptance/grader_fixes_2026_10/audit_commits.py <repo> 100000
```
