# Is SWE-bench the right venue for `LUXE_POST_WRITE_IDLE_REPEATS`? — No

Follow-up to `REPORT.md`, which found the switch inert on maintain_suite and
recommended "a corpus where the pattern occurs — SWE-bench". This checks that
recommendation **offline against 1,369 archived trajectories** rather than by
running a bench, because the whole lesson of the first A/B was to verify
opportunity before spending hours.

Method: replay every archived run's `tool_call` events and simulate both
streak rules — current (`bytes_out == 0`) and with the switch (`bytes_out == 0`
OR the call's `key_hash` was already seen) — counting runs where the current
rule stays under `_POST_WRITE_IDLE_MAX = 3` but the switch reaches it. Those
are the runs whose outcome the switch would change.

## Answer: SWE-bench is a WORSE venue, not a better one

| corpus | runs | switch would change | rate |
|---|---|---|---|
| **SWE-bench** | 896 | **7** | **0.8%** |
| maintain_suite (all history) | 473 | 40 | 8.5% |

By month:

| bucket | runs | changed | rate |
|---|---|---|---|
| swebench / 2026-05 | 760 | 7 | 0.9% |
| swebench / 2026-06 | 136 | 0 | 0.0% |
| maintain / 2026-05 | 269 | 27 | 10.0% |
| maintain / 2026-06 | 50 | 4 | 8.0% |
| **maintain / 2026-08** | 154 | **9** | **5.8%** |

An n=75 SWE-bench A/B would expect **well under one** affected instance. That
is not an underpowered experiment, it is an experiment with no signal to
measure, at hours of runtime. **Do not bench SWE-bench for this switch.**

## maintain_suite is now the better venue — which contradicts what I wrote

`agents.sdd` currently says "Do not re-run this suite expecting a different
answer." That is wrong as of today: **9 of 154 August maintain_suite runs
(5.8%) would change**, all on `nothing-ever-happens-document-config`.

The A/B's own 60 runs really did have zero opportunity — re-checked, and
would-change is **0/60** there, so `REPORT.md`'s verdict stands. The pattern
appears in the runs that came *after* it:

| experiment | runs | would change |
|---|---|---|
| pwir A/B (what REPORT.md measured) | 60 | **0** |
| truncated_turn A/B | 60 | 6 |
| truncated_turn confirmation | 30 | 3 |

`nothing-ever-happens-document-config` is the fixture with documented temp=0
variance — it is the subject of the 2026-05-17 `variants_mode_c_3rep`
experiment for exactly that reason — so run-to-run variation is the likeliest
explanation for it surfacing now and not then, rather than anything the
truncated-turn change did.

## What firing would actually do there — it looks like a win, not a risk

The obvious worry is that the switch cuts short a fixture that currently passes
3/3. Inspecting a changed trajectory (`cddcd14b3cbb`) says otherwise:

```
step  9  write_file   <-- the ONLY write in the run
step 10..27  ~60 read_file / glob / grep / bash calls, no further writes
         steps 19-20 carry three consecutive REPEAT reads  <-- would fire here
single_mode_done: 72 tool calls, 185.9s, 498,730 prompt tokens
diff_stat: +187 additions
```

The entire 187-line diff comes from the single write at step 9. Everything
after it is verification that produces no new writes. Firing at step 20 would
be a CLEAN exit (`post_write_idle_exit` sets `aborted=False`) with the work
already on disk — the guard doing exactly its stated job, saving ~8 steps and a
large share of the 186s wall and 499k prompt tokens.

So the plausible effect on maintain_suite today is **same score, less wall and
fewer tokens**. That is worth measuring; it is also exactly the shape that
could go wrong, so it needs the bench rather than this argument.

## Correction to `REPORT.md`

That report says:

> A lone repeat can never arm the guard by itself — it would need two adjacent
> 0-byte/error calls, which did not co-occur in any of the 60 runs.

The second clause is right and was verified (0/60). The first is wrong as a
general statement: a repeat topping up an existing 2-streak reaches 3, which is
precisely the mechanism behind all 9 August cases (`baseline_streak=1 →
treatment_streak=3`). The report's verdict is unaffected — the switch was inert
in that sample — but the reasoning overstated it from "did not happen here" to
"cannot happen".

## Recommendation

1. **Do not run a SWE-bench A/B for this.** 0.8%, and 0% in the most recent
   month.
2. **Re-run the maintain_suite A/B instead**, now that the pattern is present —
   same 3-rep shape, ~50 minutes, and the affected fixture is known in advance
   so every firing can be inspected against a passing baseline.
3. Judge it on wall and tokens, not score: the expected win is an earlier clean
   exit on a fixture that already passes. A score change in either direction is
   the thing to be suspicious of.
