"""forge-hybrid Phase 3 (B) — respond terminal tool.

Adds an explicit terminal-tool surface to luxe's agent loop (gated behind
`LUXE_RESPOND_TERMINAL=1`). When the model calls `respond(message=...)`, the
loop intercepts BEFORE dispatch and:

1. Applies the early-respond watchdog (writes_seen==0 AND step<4 → inject
   reprompt, do NOT terminate). Mitigates the matplotlib-20826 failure shape
   where early_bail commit_imperative pushed the model to commit prematurely.
2. Applies the anti-cheap-exit gate (require ≥1 of: post-edit read/grep/bash
   OR ≥2 steps since first write OR convergence_score recovery). Defeats
   "touch file minimally then terminate" cheap-and-wrong.
3. Applies the compaction × respond gate (Phase ≥2 compaction has fired AND
   writes_seen==0 → suppress + nudge). Prevents respond-on-phantom-recollection.
4. Detects the tokenizer-drift fallback at the LOOP layer (this module
   doesn't do parsing — the loop handles bare-text `respond(...)` only at
   tail-end or whole-turn).

Default OFF. When the env var is unset, the tool is not even registered in the
surface (byte-identical baseline preserved). Plan:
~/.claude/plans/starry-hopping-phoenix.md

See `src/luxe/tools/tools.sdd` for the contract: respond is the only tool that
terminates the loop; it is gated behind LUXE_RESPOND_TERMINAL=1.
"""

from __future__ import annotations

from typing import Any

from luxe.tools.base import ToolDef, ToolFn


def respond_def() -> ToolDef:
    return ToolDef(
        name="respond",
        description=(
            "Terminate the agent loop with a final response message. Use ONLY "
            "after the deliverable is complete (the change is written and "
            "verified). After respond is called, no further tools can run. "
            "Pass `message` with a brief summary of what you did and why."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Brief summary of the completed work.",
                },
            },
            "required": ["message"],
        },
    )


def _respond(args: dict[str, Any]) -> tuple[str, str | None]:
    """Identity passthrough: returns the message as the tool result.

    The loop's special-case handler intercepts respond calls BEFORE dispatch
    runs in the LUXE_RESPOND_TERMINAL=1 path. This function is the fallback
    if a respond call reaches dispatch (e.g., flag disabled but tool was
    somehow exposed) — it returns the message so the model sees its own
    summary echoed back rather than an "unknown tool" error.
    """
    return str(args.get("message", "")), None


TOOL_FNS: dict[str, ToolFn] = {"respond": _respond}
