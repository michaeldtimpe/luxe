"""luxe BFCL v3 (Berkeley Function-Calling Leaderboard) adapter.

PRELIMINARY scaffolding (2026-05-03). Two run modes per
`~/.claude/plans/fancy-honking-lerdorf.md`:

- **Raw mode**: single-turn `backend.chat()` call per problem; parse the
  resulting tool call; AST-compare with reference. Comparable to
  published BFCL numbers; isolates raw-model ability.

- **Agent mode**: full `run_agent()` from `src/luxe/agents/loop.py` with
  the BFCL function specs converted to ad-hoc luxe ToolDefs. Captures
  the FIRST tool call emitted. Measures whether luxe's prompt
  scaffolding helps or hurts.

The DELTA (agent − raw) is the user's primary signal: does luxe's tool
surface design add or subtract value vs the raw model on tool-call
accuracy? Run pre/post SpecDD Lever 2/3 on identical problems to
quantify how much architectural changes shift this delta.

Categories targeted (Python-relevant): simple, multiple, parallel,
parallel_multiple, irrelevance, multi_turn. Skipped: java, javascript,
rest, sql, live (require external APIs or non-Python type systems).
"""
