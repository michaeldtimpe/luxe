# Gemma 4 bake-off R1 — overnight STATUS (2026-08-19)

Written ~01:45 CDT by the provisioning session. **Exploratory probe, not a
promotion run.** The single-champion pin (`Qwen3.6-35B-A3B-6bit`) and every
config/manifest are untouched. Nothing was committed on either host — the
variants file is untracked on m1 and m5 by design; you review and commit.

---

## 1. Headline: the probe verdicts

**Tool calling works flawlessly on all three Gemma 4 candidates.** This was
the open question and it is settled. All three emit native, well-formed tool
calls with correct names and valid JSON arguments — **zero `textfallback_drop`,
zero `tool_reject`**. oMLX 0.5.7's `mlx_lm/tool_parsers/gemma4.py` is doing its
job. The 2026-05-02 empty-chat_template blocker is gone.

**They are also fast — the slow-vlm-engine risk did NOT materialize.** This was
the other big risk (it is what demoted dense Qwen3.6-27B on m1/m4). Gemma 4
chat-drill turns completed in 11-21s, comparable to or faster than the
reference. oMLX is not routing them to the vlm path.

| candidate | chat drill | tool calls | wall | code drill | verdict |
|---|---|---|---|---|---|
| `gemma-4-26b-a4b-it-6bit` (MoE) | **PASS** | 2, native | 21s | FAIL — loop abort at 8 steps | tool-calling PROVEN; code loop weak |
| `gemma-4-31b-it-4bit` (dense) | **PASS** | 2, native | 11s | FAIL — loop abort at 14 steps, **but produced the correct fix** | tool-calling PROVEN; correct, inefficient |
| `gemma-4-31b-it-6bit` (dense) | **PASS** | 2, native | 13s | FAIL — loop abort | tool-calling PROVEN; code loop weak |
| `Qwen3.6-27B-6bit` (control) | PASS | 7, native | — | **PASS**, 6 steps, clean | reference behaves correctly |

### What actually goes wrong in the code drill

All three abort with the identical reason: `Stuck in loop — repeated same tool
calls 2 consecutive turns`. The cause is **bash verification thrash**, not
inability and not a parser fault. Traced trajectory of the MoE:

```
list_dir . -> read_file test_calc.py -> bash "pytest" -> bash "pytest"
   -> bash "pytest -v" -> bash "pytest -vv" -> bash "pytest -v" -> bash "pytest -v"  [abort]
```

It never opened `calc.py`. It re-ran the test with escalating verbosity
instead. I verified the environment is innocent: `bash "pytest"` in the drill
repo returns a clean failure that *names the bug outright*
(`assert -1 == 5, where -1 = add(2, 3)`), and the control model read that same
output and fixed the bug in 6 steps. So the loop is behavioral.

**`gemma-4-31b-it-4bit` is the interesting case**: it thrashed on nine pytest
invocations, then *did* read `calc.py` and emit exactly the right edit —

```
edit_file {'path':'calc.py','old_string':'    return a - b  # planted bug','new_string':'    return a + b'}
```

— confirmed landed via `git diff` in the kept scratch repo. It was then killed
by the loop guard re-running pytest twice *after already being correct*. So the
capability is there; the step efficiency is not. It got 14 steps rather than 8
only because `_code_drill_steps` grants low-bit models a larger budget — the
MoE and the 6-bit likely ran out of budget before reaching the file.

Another behavioral signature worth noting: **Gemma 4 emits no narration at
all** — `text` is empty on every single turn and `reasoning_chars` is 0. The
control interleaves prose and reasoning with its calls ("Found the bug. In
`calc.py` line 2..."). Gemma is pure silent tool emission.

### Why all four arms were kept despite the code-drill failures

A judgment call, flagged for you explicitly. The stated gate was "prove they
can actually tool-call in luxe's agent loop" — they demonstrably do, and the
chat drill passes on all three. The code-drill failure is a *step-efficiency*
result, which is precisely the thing the 10-fixture bake-off exists to
quantify, and maintain_suite grants far larger step budgets than the drill's
8/14. Dropping all three would have left a one-arm "bake-off" with no
comparison in it. The machine was otherwise idle. **Reverse this freely** —
it is only data.

---

## 2. What was pulled

All three from HuggingFace via `luxe pull --hf --yes` (kappa is not mountable
from m5). **64.7 GB in 6m33s wall — ~165 MB/s aggregate.**

| ref | size | wall |
|---|---|---|
| `mlx-community/gemma-4-26b-a4b-it-6bit` | 20.2 GB | 01:25:10 -> 01:27:19 (2m09s) |
| `mlx-community/gemma-4-31b-it-4bit` | 18.4 GB | 01:27:19 -> 01:29:08 (1m49s) |
| `mlx-community/gemma-4-31b-it-6bit` | 26.1 GB | 01:29:08 -> 01:31:43 (2m35s) |

**Registered ids match the variants file's guesses exactly** — no correction
was needed on either host:
`gemma-4-26b-a4b-it-6bit`, `gemma-4-31b-it-4bit`, `gemma-4-31b-it-6bit`.

`brew services restart omlx` was required (as expected — no rescan endpoint).
It also cleared the three phantom `Qwen3.8-27B-*` ids. Disk went from 326 GiB
free to **327 GiB free despite adding 64.7 GB** — the restart released ~93 GB
of deleted-but-still-open Qwen3.8 inodes the old server process was holding.

---

## 3. What was launched

- **Command**: `benchmarks.maintain_suite.run --variants
  benchmarks/maintain_suite/variants_gemma4.yaml --output
  acceptance/gemma4_r1_2026_08_19 --per-fixture-timeout 1800 --all`
- **Launched**: 01:40 CDT, `nohup`, verified surviving ssh disconnect
  (`setsid` does not exist on macOS; nohup was used).
- **PID**: `89947` on m5.
- **Arm-count rule fired**: probes finished before 02:15 CDT -> **all 4 arms**.
- **Matrix** (4 arms x 10 fixtures = 40 runs), in the variants file's
  deliberate priority order:
  1. `mono__qwen3.6-27b-6bit-ref` (incumbent thick fallback, reference)
  2. `mono__gemma-4-26b-a4b-it-6bit` (MoE 26B-A4B)
  3. `mono__gemma-4-31b-it-4bit` (dense 31B)
  4. `mono__gemma-4-31b-it-6bit` (dense 31B)
- **Nothing dropped.**
- **Log**: `acceptance/gemma4_r1_2026_08_19/full_launch.log` — **expect this to
  be 0 bytes for most of the run**; Python block-buffers stdout when it is not
  a TTY. It is not a sign of trouble. Track progress via `history.jsonl`.
- **Expected finish**: ~05:50 CDT (qwen38 precedent at this exact shape was
  ~4h10m). First fixture completed in 116s, so it may land earlier.
- **Confirmed progressing** before this file was written: pid alive after
  disconnect, `mono__qwen3.6-27b-6bit-ref/lpe-rope-calc-document-typing/result.json`
  written (score 4/5, passed, 115.7s), second fixture underway.

### Is it still alive?

```sh
ssh m5 'pgrep -f maintain_suite.run; wc -l ~/Downloads/luxe/acceptance/gemma4_r1_2026_08_19/history.jsonl'
```
40 lines in `history.jsonl` = complete.

---

## 4. Run-id manifest

`history.jsonl` carries the fixture->run_id mapping **live**, incrementally, so
the manifest can be built at any time (it does not need the run to finish).
Per the standing lesson that `stdout.log` gets overwritten, build and keep it:

```sh
ssh m5 'cd ~/Downloads/luxe/acceptance/gemma4_r1_2026_08_19 && python3 -c "
import json
for l in open(\"history.jsonl\"):
    e = json.loads(l)
    print(\"\t\".join([\"gemma4_r1_2026_08_19\", e[\"variant\"], e[\"fixture\"], e[\"run_id\"], e[\"status\"]]))
"' > acceptance/gemma4_run_id_manifest.tsv
```

Same five-column shape as `acceptance/qwen38_27b_run_id_manifest.tsv`.

---

## 5. How to triage in the morning

Per-fixture results land at
`acceptance/gemma4_r1_2026_08_19/<variant>/<fixture>/result.json` (plus
`diagnostics.json`, `state.json`, `stdout.log`, `stderr.log`). Rollup scores
are in `history.jsonl`.

Three specific gotchas:

**(a) The `context_overflow` label lies about max-steps deaths.**
`run.py:866`'s `bailout_type="context_overflow"` classifier string-matches
`"max steps"`, so a max-steps death is mislabelled as context overflow. Always
read peak context % alongside it before believing the label. Given the probe,
**expect max-steps deaths to be Gemma's dominant failure mode** — the thrash
pattern burns budget — so this mislabelling is likely to bite on exactly this
run.

**(b) Separate "never tried" from "tried and was rejected."**
```sh
grep -h -o '"kind": "tool_reject"[^}]*' ~/.luxe/runs/<run_id>/events.jsonl
grep -h -o '"kind": "textfallback_drop"[^}]*' ~/.luxe/runs/<run_id>/events.jsonl
```
`tool_reject` carries `reason=schema|unknown_tool`. At probe time both were
**zero** for all three candidates, so any appearance here is new information.

**(c) Compare `nothing-ever-happens-document-config` first.** It is the
discriminating fixture: Qwen3.8 died on it (write-avoidant exploration, ~35
reads and zero writes) while `Qwen3.6-27B-6bit` went 3/3. Gemma's probe
signature is *also* a form of write-avoidance — it explored and re-tested
instead of editing — so this fixture is the sharpest read on whether that
generalizes. Check it before anything else.

Also worth a look: **wall time per fixture**. Gemma was fast per turn but
spends turns wastefully; total wall is the honest efficiency measure, and it is
what a fallback model actually gets judged on.

---

## 6. Precedent

`acceptance/qwen38_27b_r1_2026_08_17/REPORT.md` — the directly comparable
run (same 1 rep x 10 fixtures x 4 arms shape, same reference arm, same
`--per-fixture-timeout 1800`). Verdict there was NOT PROMOTED. Use its
structure for this report.

## 7. Reproduce the probes

```sh
ssh m5 'cd ~/Downloads/luxe && set -a && . ~/.luxe/secrets.env && set +a &&
  .venv/bin/python -m luxe.cli smoke --chat --code --model gemma-4-26b-a4b-it-6bit'
```
The per-turn tool-call tracer used for the trajectories above is at
`~/pullhome/diag.py` on m5 (`.venv/bin/python ~/pullhome/diag.py <model-id>`);
pull logs are in `~/pullhome/pull.log`, probe logs in `~/pullhome/probes/`.

A ready-made builder is saved on m5 at `~/pullhome/build_manifest.sh`; run
`ssh m5 "~/pullhome/build_manifest.sh" > acceptance/gemma4_run_id_manifest.tsv`.
An initial (partial) manifest was already written to
`acceptance/gemma4_run_id_manifest.tsv` on m5 — re-run the builder after the
bench finishes to capture all 40 rows.
