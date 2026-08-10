# `lpe-rope-calc-implement-strict-flag` fails 6/6 — truncated turn read as a finished answer

Found during the 2026-08-10 `LUXE_POST_WRITE_IDLE_REPEATS` A/B (see
`REPORT.md`), independent of that switch — the fixture fails identically in
both arms. Diagnosis only; **no fix shipped**.

## Symptom

Score 1/5, 0/3 in both arms, 6/6 runs, with the same grader line every time:

```
luxe produced no diff vs base_sha — regex_present outcome NOT credited
diff_produced: false · diff_files: 0 · gates_triggered: []
```

No gate fired. `aborted: false`. The harness records a clean, completed run
that happens to have produced nothing.

## It is perfectly deterministic

All six runs are identical to the token:

| | value, all 6 runs |
|---|---|
| tool calls | 3 (`list_dir` 82B, `bm25_search` 45B, `read_file` 22,379B) |
| prompt tokens | 14,045 |
| completion tokens | **8,326** |
| `final_text_chars` | **36,838** |
| aborted | false |

This is not substrate noise. It is the same trajectory six times.

## Mechanism

1. The fixture repo is **three files** (`pe_scan.py`, `README.md`,
   `AGENTS.md`). `list_dir` + one `read_file` puts the entire repo in context
   by call 3. `bm25_search` returns 45 bytes — nothing to work with, and
   nothing it needs.
2. The model then produces a **36,838-character planning monologue** instead
   of editing. It re-plans the same refactor at least three times ("The
   cleanest approach is…" recurs), and drifts into narrating its own reasoning
   stream: *"complete the partial thought from the current rewritten
   thinking… Looking at the next thinking"*.
3. `max_tokens_per_turn` is **8192** (`configs/single_64gb.yaml`). Steps 1–2
   cost ~134 tokens, so the final turn generated **exactly 8,192** and was cut
   off mid-word — the text ends at ``construct a `PEInfo``.
4. That truncated response contains no tool calls. `loop.py:1019`
   (`if not tool_calls:`) takes the terminal path and the run ends
   successfully with no diff.

## Root gap: the loop never looks at `finish_reason`

```
$ grep -n "finish_reason" src/luxe/agents/loop.py
(no matches)
```

A response truncated at the token cap (`finish_reason == "length"`) is
**indistinguishable** to the loop from a model that finished and chose to
answer. Both arrive as "assistant message, no tool calls" and both are
terminal. So a run that was cut off mid-sentence is reported as a clean
completion, `aborted=False`, with no gate triggered.

The text-fallback recovery at `loop.py:1003` does run, but the prose contains
markdown code fences, not tool-call syntax, so it recovers nothing (and emits
no `textfallback_drop` — there were no candidates to drop).

## Both existing guards are structurally unable to fire

| guard | requires | this run |
|---|---|---|
| `WritePressureGuard` | `step ≥ 5` **and** `tool_calls_total ≥ 10` | 3 steps, 3 calls ✗ |
| prose-burst | `tool_calls_total == 0` | 3 calls ✗ (and default-OFF) |

The trajectory sits in the gap: too *few* tool calls for write-pressure, too
*many* for prose-burst. `LUXE_WRITE_PRESSURE=1` is the bench default and is
exactly the lever for prose-heavy no-write failures — it simply cannot reach
this shape.

## Versus the bake-off, where this fixture passed

`acceptance/m5max_moe/mono__qwen3.6-35b-a3b-6bit/` for the same fixture,
2026-05-10:

| | 2026-05-10 (pass, 4/5) | today (fail, 1/5) |
|---|---|---|
| tool calls | **15** | **3** |
| prompt tokens | 161,568 | 14,045 |
| completion tokens | 4,439 | 8,326 |
| `final_text_chars` | **0** | **36,838** |
| `post_write_idle_exit` | fired | — |
| diff | 2 files, +32 | none |

The model used to **act** — 15 tool calls, zero final prose, a diff, and the
post-write idle guard firing after the work landed. It now narrates instead.
The behavioural shift itself is not pinned here: three months of prompt and
substrate change separate the runs, and bisecting that is its own cycle. What
IS pinned is that the substrate cannot *detect* the new shape.

## Breadth

Across all 60 A/B runs, 7 hit the token cap:

- `lpe-rope-calc-implement-strict-flag` — **6/6**
- `nothing-ever-happens-document-config` — 1/6 (still passed 3/3; on a
  `document` task the prose IS the deliverable, so truncation costs less)

So the cap is hit rarely, but on an `implement` task it is fatal and here it
is deterministic.

## Recommendation

The smallest high-value change is to **make the loop see `finish_reason`**. A
turn that is truncated at the cap AND carries no tool calls should not be
terminal — at minimum it should be recorded as a failure rather than a clean
completion, and plausibly it should continue (re-prompt to emit the edit)
rather than end the run.

That is a benchmark-path change: it can only turn some currently-"successful"
empty runs into continuations or explicit failures, which will move scores. It
therefore needs its own maintain_suite A/B before any default changes, per
CLAUDE.md rule 3. Not shipped here.

Secondary, cheaper observation: `max_tokens_per_turn: 8192` against
`num_ctx: 32768` leaves plenty of context headroom (peak pressure this run was
0.19). Raising the per-turn cap would let this particular monologue finish and
possibly reach a tool call — but that treats the symptom, and a model that
needs 8k+ tokens to plan a one-flag change has a conclusion problem, not a
budget problem. Prefer the `finish_reason` fix.
