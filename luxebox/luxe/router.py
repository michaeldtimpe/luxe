"""Interpreter / router agent.

The router reads the user's prompt and chooses exactly one specialist to
hand off to. It can ask at most two clarifying questions first. Its
output is a single tool call — either `dispatch(agent, task)` or
`ask_user(question)`. Free-form text output is ignored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from harness.backends import Backend, ToolDef

from luxe.backend import make_backend
from luxe.registry import AgentConfig, LuxeConfig
from luxe.session import Session

MAX_CLARIFYING_ROUNDS = 2


@dataclass
class RouterDecision:
    agent: str
    task: str
    reasoning: str = ""
    clarifications: list[tuple[str, str]] = field(default_factory=list)  # (q, a) pairs


AskFn = Callable[[str], str]  # question -> user answer


def _build_tools(enabled_agents: list[str]) -> list[ToolDef]:
    return [
        ToolDef(
            name="dispatch",
            description=(
                "Route the task to one specialist agent. Call this exactly "
                "once when you know which specialist should handle the task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "enum": enabled_agents,
                        "description": "Which specialist to hand off to.",
                    },
                    "task": {
                        "type": "string",
                        "description": (
                            "The refined task description to send the "
                            "specialist. Incorporate any clarifications."
                        ),
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "One short sentence on why you picked this agent.",
                    },
                },
                "required": ["agent", "task", "reasoning"],
            },
        ),
        ToolDef(
            name="ask_user",
            description=(
                "Ask the user a single clarifying question. Only use when "
                "the request is genuinely ambiguous and you cannot pick a "
                "specialist without more information. You may only ask up "
                f"to {MAX_CLARIFYING_ROUNDS} clarifying questions total."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                },
                "required": ["question"],
            },
        ),
    ]


def _system_prompt(cfg: LuxeConfig) -> str:
    lines = [
        "You are the router for luxe, a local multi-agent CLI.",
        "",
        "Given a user prompt, decide which ONE specialist agent should handle it.",
        "Your ONLY output is a tool call: `dispatch` or `ask_user`.",
        "Do not answer the user yourself. Do not produce any free-form text.",
        "",
        "Available specialists:",
    ]
    descriptions = {
        "general": "Default for Q&A, explanations, definitions, chit-chat, simple factual or conceptual questions.",
        "research": "Use when the task genuinely needs fresh web info: current events, news, recent releases, deep investigation of a topic.",
        "writing": "Creative writing, editorial review, and document drafting: fiction, poetry, long-form essays, storytelling, brainstorming, plus reviewing, revising, or creating text documents in the local folder.",
        "image": "Generating an image or picture from a text description.",
        "code": "Writing, editing, or debugging code in the user's working directory. Running tests. Anything that requires reading or editing real files in a repo.",
    }
    for a in cfg.agents:
        if a.name == "router" or not a.enabled:
            continue
        desc = descriptions.get(a.name, a.display)
        lines.append(f"- {a.name}: {desc}")
    lines += [
        "",
        "Heuristics:",
        "- Abstract coding questions ('how does Python list comprehension work?') → general, not code.",
        "- 'Fix the bug in X' or 'edit this file' → code.",
        "- 'Review the documents', 'read my notes', 'what's in this folder',"
        " 'summarize these drafts' → writing (it has filesystem tools for"
        " prose/docs; code is only for source repos).",
        "- 'What happened last week in…' or 'latest version of…' → research.",
        "- Meta questions like 'can you read files here?' → dispatch to the"
        " agent that actually has those tools (writing for docs, code for"
        " source) so it can demonstrate; don't send to general.",
        "- If the request is vague ('help me'), use `ask_user` once to get specifics.",
        f"- You may ask at most {MAX_CLARIFYING_ROUNDS} clarifying questions before you MUST dispatch.",
        "- If still uncertain after clarifications, default to `general`.",
    ]
    return "\n".join(lines)


_FILE_HINT_RE = re.compile(
    r"\b(folder|directory|document|documents|file|files|notes?|draft|drafts|"
    r"manuscript|essay|chapter|letter|readme|\.md|\.txt|\.rst)\b",
    re.IGNORECASE,
)


def _fallback_agent(prompt: str, enabled: list[str]) -> str:
    """Pick a default when the router emits no tool call.

    If the prompt mentions files/folders/documents, prefer `writing` (it has
    the fs tool surface for prose review/drafting). Otherwise fall back to
    `general`. Only return agents that are actually enabled.
    """
    if _FILE_HINT_RE.search(prompt) and "writing" in enabled:
        return "writing"
    return "general" if "general" in enabled else enabled[0]


def route(
    prompt: str,
    cfg: LuxeConfig,
    *,
    ask_fn: AskFn,
    session: Session | None = None,
) -> RouterDecision:
    router_cfg = cfg.get("router")
    backend = make_backend(router_cfg.model, base_url=cfg.ollama_base_url)
    enabled = [a.name for a in cfg.agents if a.enabled and a.name != "router"]
    if not enabled:
        raise RuntimeError("no specialists enabled")

    tools = _build_tools(enabled)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(cfg)},
        {"role": "user", "content": prompt},
    ]
    if session:
        session.append({"role": "user", "agent": "router", "content": prompt})

    clarifications: list[tuple[str, str]] = []
    rounds = 0

    while True:
        if rounds >= MAX_CLARIFYING_ROUNDS:
            # Force a dispatch decision — strip ask_user from the tool set.
            tools_this_round = [t for t in tools if t.name == "dispatch"]
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "You have used all clarifying questions. Call dispatch now."
                    ),
                }
            )
        else:
            tools_this_round = tools

        try:
            response = backend.chat(
                messages,
                tools=tools_this_round,
                max_tokens=router_cfg.max_tokens_per_turn,
                temperature=router_cfg.temperature,
                stream=False,
            )
        except Exception as e:  # noqa: BLE001
            fallback = _fallback_agent(prompt, enabled)
            return RouterDecision(
                agent=fallback,
                task=prompt,
                reasoning=f"router error ({type(e).__name__}); defaulted to {fallback}",
                clarifications=clarifications,
            )

        if not response.tool_calls:
            # Model didn't call a tool. Fall back — pick writing if the prompt
            # hints at files/docs, otherwise general.
            fallback = _fallback_agent(prompt, enabled)
            if session:
                session.append(
                    {
                        "role": "router",
                        "content": f"no tool call; defaulting to {fallback}",
                        "raw": response.text[:500],
                    }
                )
            return RouterDecision(
                agent=fallback,
                task=prompt,
                reasoning=f"router emitted no tool call; fell back to {fallback}",
                clarifications=clarifications,
            )

        call = response.tool_calls[0]
        args = call.arguments or {}

        if call.name == "dispatch":
            agent = args.get("agent", "general")
            if agent not in enabled:
                agent = "general"
            task = args.get("task") or prompt
            reasoning = args.get("reasoning", "")
            if session:
                session.append(
                    {
                        "role": "router",
                        "decision": {"agent": agent, "task": task, "reasoning": reasoning},
                        "clarifications": clarifications,
                    }
                )
            return RouterDecision(
                agent=agent, task=task, reasoning=reasoning, clarifications=clarifications
            )

        if call.name == "ask_user":
            question = args.get("question", "").strip()
            if not question:
                # Model produced an empty clarifying question — bail out.
                fallback = _fallback_agent(prompt, enabled)
                return RouterDecision(
                    agent=fallback,
                    task=prompt,
                    reasoning="router produced empty clarification",
                    clarifications=clarifications,
                )
            answer = ask_fn(question)
            clarifications.append((question, answer))
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(args),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": answer,
                }
            )
            if session:
                session.append(
                    {"role": "router", "clarifying_question": question, "user_answer": answer}
                )
            rounds += 1
            continue

        # Unknown tool — give up.
        return RouterDecision(
            agent=_fallback_agent(prompt, enabled),
            task=prompt,
            reasoning=f"unknown router tool '{call.name}'",
            clarifications=clarifications,
        )
