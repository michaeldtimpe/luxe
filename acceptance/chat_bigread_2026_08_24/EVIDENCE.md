# Chat-side evidence: one oversized read ends the turn (2026-08-24)

Forensics from four `luxe chat` sessions on 2026-08-24, 18:17–18:50. Two
backends, one failure shape. This is the **chat-side evidence**
`tools/tools.sdd` § "`LUXE_TOOL_BUDGET_CTX` wiring" records as not existing
("chat UX around large files is a different question", "no chat-side evidence
exists"). It exists now.

## The shape

A turn opens two files in ONE step. The step's tool results alone exceed what
the window can carry. The request is dispatched anyway, fails, and is retried
verbatim until attempts are exhausted. The turn is lost; the user re-asks; the
model makes the identical calls and loses the identical turn.

## Case 1 — openrouter / `moonshotai/kimi-k3`, 128K window

Session `168f1825a1fd`, repo `~/Downloads/yapping`.

| step | call | bytes_out |
|---|---|---|
| 1 | `list_dir .` | 385 |
| 2 | `read_file self.md` | **257,988** |
| 2 | `read_file questions.md` | 23,775 |
| 3 | — | request fails |

`~/.luxe/runs/168f1825a1fd-2/events.jsonl`:

```
compaction_phase_reached  phase_reached=3  tokens_before=71616  tokens_after=71616  tool_results_dropped=0
compaction_phase_at_resolve  max_phase_reached=3  tool_results_dropped_total=0  total_tokens_dropped=0  aborted=True
```

Phase 3 — the most aggressive tier — fired and dropped **nothing**.
`TieredCompact._find_eligible_end` (`src/luxe/context.py:226`) returns 2
("nothing eligible — protect the whole thing") when fewer than
`keep_recent=3` assistant messages exist. At step 3 of a chat turn there are
two. Compaction is structurally incapable of acting in the first three steps
of a turn, which is exactly where an interactive turn blows its window.
`keep_recent=3` is correct for the 12–30-step SWE-bench trajectories it was
tuned on; it is dead weight for a 3-step chat turn.

`debug.log`, both attempts:

```
18:47:38 WARNING backend moonshotai/kimi-k3 exception=RemoteProtocolError decision=RetryDecision(retry=True,  reason='transient-RemoteProtocolError', delay_s=1.0)
18:48:02 WARNING backend moonshotai/kimi-k3 exception=RemoteProtocolError decision=RetryDecision(retry=True,  reason='transient-RemoteProtocolError', delay_s=4.0)
18:48:25 WARNING backend moonshotai/kimi-k3 exception=RemoteProtocolError decision=RetryDecision(retry=False, reason='exhausted-attempts')
18:48:25 ERROR   turn aborted: Backend error: oMLX stream failed: RemoteProtocolError … (exhausted-attempts)
```

Cost of the two lost turns: 81.5s + 76s wall, three identical dispatches each,
billed. The user re-sent the same message at 18:49; the model ran the identical
`list_dir` + same two reads and hit the identical wall.

## Case 2 — local / `Qwen3.6-35B-A3B-4bit`, 32K window

Session `eb0b2923a3eb`, repo `~/Downloads/luxe`. Same shape, no cloud involved:

```
18:18:04 read_file src/luxe/cli.py                 bytes_out=72181
18:18:04 read_file tests/test_chat_backends.py     bytes_out=65181
18:18:04 step=3 ctx_pressure=271.8% (est=110.0% cal=2.47x) num_ctx=32768
18:20:04 turn interrupted   observed_tool_calls=4 partial_chars=99
```

Two minutes of silence, then the user ctrl+c'd. Then did it again
(`cli.py` + `modelstore.py`, 201.8%, interrupted at 18:21:41). **Four turns
lost across the two sessions.** `num_ctx` is an accounting number only —
nothing clamps what is actually dispatched.

## What the budget would have done

`budget_for_ctx()` is already implemented and already DEFAULT-ON for
maintain/bench. Had chat's opt-in been on:

| session | window | file | actual | budget | outcome |
|---|---|---|---|---|---|
| `168f1825a1fd` | 131072 | `self.md` | 257,988 B | **54,613 B** | clipped, `offset=` to resume |
| `eb0b2923a3eb` | 32768 | `cli.py` | 72,181 B | **13,653 B** | clipped, `offset=` to resume |

Both turns survive. The mechanism needed no new code — only the default.

## Collateral findings in the same corpus

1. **Wrong engine named.** `backend.py:850` hardcodes `"oMLX stream failed"`.
   The backend was openrouter (`engine: openrouter`). `BackendEntry.engine_label()`
   already exists (`config.py:164`); `Backend` never receives it.
2. **Wrong fix prescribed.** The footer printed `openrouter OpenRouter
   unreachable — try /backend local`. OpenRouter was demonstrably reachable —
   turns succeeded at 18:42 and 18:45 in the same session and at 18:40 in
   session `5a007539f164`. `slots.unreachable_hint()` (`slots.py:416`) fires on
   any backend error when ≥2 backends are configured; it never probes. The
   advised `local` has a 32K window and had already failed this way (Case 2).
3. **Pressure over-reported at the moment it matters.** The calibration ratio
   is measured on the previous response. At step 1 that prompt is dominated by
   the system prompt + tool JSON schemas, which tokenize far worse than
   chars/4: observed step-1 ratios 2.64x, 2.16x, 1.91x, 1.79x. After a prose
   file landed, the same sessions recalibrated to **1.27x and 1.21x**. The
   102.5% figure is `71,616 est × 1.88`. At the prose-true ~1.2 the request was
   ~86k tokens — **65% of a 128K window**. agents.sdd already records this
   drift ("3.69x at step 1 down to 2.38x by step 13"); what it does not do is
   damp the extrapolation when prompt composition changes by 20x in one step.
4. **The footer contradicts itself.** One block printed `ctx: 2% of 128K` (from
   `last_prompt_tokens` — the last *accepted* step) beside
   `context pressure 103%` (peak estimate of the step that failed).
5. **`/ctx huge` offered on peak pressure alone** (`repl.py:1084`,
   `tui.py:752`), with no knowledge of whether more window would help. Here it
   would not have.
6. **`list_dir` could not warn.** `_oversize_note` (`fs.py:428`) annotates only
   files past the *refusal* cap (262,144). `self.md` at 257,988 B — 0.98× the
   cap — listed as a bare name. Its own docstring says the point is that the
   model should not learn size by failing; the threshold is the wrong one.
7. **Session notes lost.** `luxe.chat.notes: session notes skipped:
   UnicodeDecodeError: 'utf-8' codec can't decode byte 0x8b in position 1`
   (session `5a007539f164`). `0x1f8b` is gzip magic hitting the bare
   `read_text(encoding="utf-8")` at `notes.py:188`. Silent-skip is by design;
   the notes were still lost.
8. **A slash command was silently eaten.** The user typed `` `/ctx xlarge ``
   mid-failure. `commands.py:128` requires `startswith("/")`, so the leading
   backtick sent it to the model as prose and the next turn ran at 32K anyway.
