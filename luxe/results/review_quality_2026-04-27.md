# /review report-quality investigation

**Status:** parked, not yet acted on. The ctx-denominator bug from the
same investigation landed; this is the deferred half.

**Trigger:** `/review https://github.com/michaeldtimpe/neon-rain` on
commit `cb0f752` produced
`luxe/local-cache/neon-rain/REVIEW-T-20260427T205124-gga059.md`. Spot-
checks against the actual repo show the report is mostly fabricated.

## Fabricated findings (verbatim)

The report's Robustness section emitted three Medium-severity
findings. All three failed verification when the cited files/lines
were re-read:

1. **`src/game.js:56` — unbounded `while (true)` loop.**
   `game.js:56` is an event-listener wiring line. There is no
   `while(true)` anywhere in the file. The closest match is
   `_executeNextServerAction()` at line 718, which uses recursive
   `setTimeout`, not a `while` loop.

2. **`src/utils.js:34` — unbounded `for` loop.**
   `utils.js` is 62 lines. Line 34 is `randPick()`, a one-liner:
   `arr[Math.floor(...)]`. No loop. There's a Fisher-Yates shuffle
   on lines 46-49 with a clear `i > 0` termination — that's the
   only loop in the file and it's correctly bounded.

3. **`src/network.js:23` — missing fetch timeout.**
   `network.js` does not exist in the repo. The agent's own
   `read_file` call against this path returned `FileNotFoundError`
   during sub 05; the orchestrator's grounding check flagged it; the
   report shipped the finding anyway.

The Maintainability section emits five complexity findings citing
specific scores ("complexity 15", "complexity 12", etc.) at lines
50, 30, 100, 50, 150 in `game.js` and `utils.js`. The agent did call
`lint` during sub 06 — but `lint` for JS routes through eslint, not
ruff, and eslint complexity is reported per-rule, not as a numeric
score the agent could quote. These numbers appear to be invented.

## Real issues the report missed

A 15-minute manual pass over the same repo turned up issues the
report does not mention:

- **Async hazard in nested `setTimeout`**: `game.js:729, 763` schedule
  callbacks that read `this.state` after a delay without re-checking
  `state?.jobActive`. If the game resets between schedule and fire,
  the callback dereferences stale state.
- **No `clearTimeout` on game-state reset**: the same nested timeouts
  are never tracked or cancelled. Long sessions can orphan dozens of
  pending timers.
- **EventBus listener exception swallowing**: `game.js:102+` dispatches
  to listeners without try/except. One bad listener takes the whole
  bus down silently.
- **Unbounded log growth**: `_addLogEntry` and the debug log append
  forever with no rotation or size cap. Long games leak memory.
- **Missing null-checks on DOM lookups**: `cardDetailOverlay`
  references at lines 75, 586 assume the element exists; no fallback
  if the page renders without it.
- **Unvalidated zone access in `_findCardById`** (line 505): assumes
  every zone exists; doesn't handle a missing zone gracefully.

These are the kinds of issues a competent human reviewer would flag
on a 11k-LOC vanilla-JS card-game repo. None of them appear in the
report.

## Three candidate root causes

### 1. Subtask scope (planner emits one subtask per category)

The planner (`luxe/luxe_cli/tasks/planner.py:117-191`) is LLM-driven.
The goal text in `luxe/luxe_cli/review.py:build_review_goal` lists
four inspection categories under "systematically look for"; the
planner faithfully decomposes them into four inspection subtasks
(security, correctness, robustness, maintainability). The system
prompt caps cardinality at 1-8 and prefers 3-5
(`planner.py:34-99`).

What the agent does with one of these subtasks: treat "Check for
security issues" as "run all the security analyzers I have, then
summarize." The transcript bears this out — sub 03 only called
`security_taint`, `security_scan`, `secrets_scan`, `deps_audit`
before the orchestrator's shallow-inspection retry kicked in. After
the retry, the agent still didn't grep or read source files in any
focused way.

**Hypothesis:** splitting into per-axis subtasks (e.g.
"Check for SQL injection" / "Check for path traversal" /
"Check for prototype pollution" / "Check for XSS sinks" /
"Check for unsanitized DOM mutation") would force the agent to
focus each subtask on a specific search pattern rather than running
the full analyzer suite once.

**Counter-hypothesis:** the agent might simply rubber-stamp 6
analyzer runs instead of 1 — still no real reading.

**Test:** instrument a per-axis goal and rerun on neon-rain; count
`grep`/`read_file` calls per inspection subtask. Target: ≥3
non-analyzer reads per subtask.

### 2. Grounding-check policy (`orchestrator.py:343, 789`)

The orchestrator's grounding check catches unverified claims
correctly — it identified all three of `network.js:23`,
`while(true)` in game.js, and the absent file from sub 05. The
policy: `_annotate_grounding_issues` **prepends** a warning block
listing the bad citations. The original confident finding text is
left unchanged in the body of the subtask result.

In the saved report, this means the user sees:

```
## Robustness

⚠ Grounding check: 3 unverified claims (see details).

[Then the original finding — confident, with severity, suggested fix]
File: src/network.js:23
Severity: Medium
Issue: Missing timeout for fetch …
```

The warning is easy to miss next to the confident finding text.

**Hypothesis:** if `_annotate_grounding_issues` instead **drops**
unverified claims (or strikethrough-formats them), the report would
not ship fabrications — only verified findings reach the final
output.

**Cost:** drops are destructive. If the grounding check has false
positives (real claim flagged as unverified), the user loses the
finding. False-positive rate of the grounding check is currently
unmeasured; we'd want to instrument that before flipping the
policy.

**Test:** add a `--strict-grounding` flag to the orchestrator that
drops rather than annotates. Rerun the same neon-rain `/review`.
Compare bodies of the two reports. Manually rate finding quality.

### 3. System prompt doesn't hard-require code reads

The `review` agent system prompt
(`luxe/configs/agents.yaml:485+`) advises tool-use discipline
("Inspection subtasks require real inspection. `list_dir` alone
is not inspection") and gives concrete grep starting points. But
it doesn't enforce: it says "look like" and "should" rather than
"must, or report `Insufficient evidence`."

**Hypothesis:** rewriting the prompt to require at least one
`read_file` or `grep` call per High/Medium finding, with the
escape hatch of "if analyzers returned nothing and you have no
grep evidence, report `Inspected via X; no findings`", would
suppress the fabrication channel.

**Cost:** more verbose prompts can degrade the model's other
behavior. Agent might over-correct and refuse to emit any
findings even when justified.

**Test:** A/B the prompt with and without the strict requirement
on a fixed corpus of 5 small repos (mix of Python + JS); compare
finding-rate × verified-rate.

## Open follow-ups

- **Try Qwen3-Coder-30B-A3B for `/review`.** Weights are still in
  `~/.omlx/models/Qwen3-Coder-30B-A3B-Instruct-4bit/`. Coder
  variants are trained on agentic coding loops; might handle the
  read-then-reason pattern differently than Instruct-2507's
  long-context fabrication mode. Smoke test on neon-rain first.
- **Per-subtask fresh context.** The orchestrator currently grows
  the agent's conversation history across all turns of one
  subtask. Resetting context each turn (with a small handoff
  summary) would cap the per-turn ctx and prevent the long-context
  fabrication mode the MoE evaluation surfaced. Would also reduce
  per-turn prefill cost on Qwen2.5-32B.
- **Measure grounding-check false-positive rate.** Before flipping
  to drop-on-unverified, instrument 10 known-good `/review` runs
  to see how often the check incorrectly flags a real finding.
- **Consider analyzer-only mode.** For repos where the agent's
  reading is mostly fabricated, a stripped-down `/review` mode
  that runs only the analyzer suite + grep panel and skips the
  LLM-generated findings entirely might produce a more honest
  report. Loses the "synthesis" upside but eliminates fabrication.

## Resume notes

- Plan file (this round's exit): `~/.claude/plans/logical-launching-puffin.md`
- Memory: `~/.claude/projects/-Users-michaeltimpe-Downloads-luxe/memory/project_qwen3_migration.md` has related context on long-context fabrication.
- LESSONS.md "One repo isn't a measurement either" section covers a related observation about the MoE on neon-rain.
