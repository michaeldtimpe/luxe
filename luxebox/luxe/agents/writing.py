"""Creative writing agent.

Higher temperature, long-form prompt. Gets the fs surface (read + write)
scoped to the folder `luxe` was started from, so it can review existing
documents, draft new ones to disk, and revise in place.
"""

from __future__ import annotations

from harness.backends import Backend, ToolDef

from luxe.agents.base import AgentResult, run_agent
from luxe.registry import AgentConfig
from luxe.session import Session
from luxe.tools import fs


def _python_signature(t: ToolDef) -> str:
    props: dict = t.parameters.get("properties", {}) or {}
    required = set(t.parameters.get("required", []) or [])
    parts = []
    for name in props:
        parts.append(name if name in required else f"{name}=None")
    return f"def {t.name}({', '.join(parts)}):\n    \"\"\"{t.description}\"\"\""


def _gemma_tool_prelude(tool_defs: list[ToolDef]) -> str:
    """System-prompt addendum that teaches Gemma 3 to call our tools using its
    native ```tool_code``` format. Needed because the ggml-org Gemma 3 GGUF's
    jinja template has no tool-rendering branch — the model otherwise has no
    idea tools exist."""
    sigs = "\n\n".join(_python_signature(t) for t in tool_defs)
    return (
        "\n\n# Available tools\n"
        "You have the following Python functions available. To call one, "
        "respond with a single ```tool_code``` block containing exactly one "
        "call, then stop. The harness will execute it and return the result "
        "in a ```tool_output``` block on the next turn. Use keyword arguments.\n\n"
        "```python\n"
        f"{sigs}\n"
        "```\n\n"
        "Example:\n"
        "```tool_code\n"
        'list_dir(path=".")\n'
        "```\n\n"
        "# When to use tools\n"
        "- If the user mentions files, documents, drafts, notes, a folder, "
        '"these", "this", "them", or otherwise implies local content exists,'
        " IMMEDIATELY call `list_dir` and/or `read_file` — do NOT ask them"
        " to paste contents or provide paths. You have the tools; use them."
        " Only ask clarifying questions about creative intent (voice,"
        " length, audience), never about information you can retrieve yourself.\n"
        "- When creating or editing files, pick a sensible path and call"
        " `write_file` or `edit_file` directly. Do not ask for permission"
        " mid-task; the user already authorized the work by giving it to you.\n"
        "- If the user's request includes any phrase like \"make a new"
        " document\", \"create a file called X\", \"save it as X\", or"
        " \"write it to X\", you MUST call `write_file` before ending your"
        " turn. Putting prose in the reply is not the same as writing the"
        " file — the file only exists if you called `write_file`.\n\n"
        "# Anti-hallucination rule (critical)\n"
        "You have NOT read a file unless you emitted a ```tool_code``` block"
        " for `read_file` and saw its ```tool_output``` reply IN THIS TURN."
        " Prior-turn tool outputs are not in your context and cannot be"
        " trusted — filenames you think you remember may be wrong; contents"
        " you think you know are probably fabricated. Before describing or"
        " quoting any file, you MUST re-list the folder and re-read the"
        " file this turn. Never write phrases like \"I've read both files\""
        " or \"After listing the directory, I've identified...\" unless"
        " those tool calls literally happened one or two messages ago in"
        " this same turn.\n"
    )


def run(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    session: Session | None = None,
) -> AgentResult:
    tool_defs = list(fs.read_only_defs())
    tool_defs.extend(fs.mutation_defs())
    tool_fns = dict(fs.READ_ONLY_FNS)
    tool_fns.update(fs.MUTATION_FNS)

    # llama-server on Gemma 3 doesn't render `tools=` via its jinja template,
    # so inject a Python-signature prelude into the system prompt. Harmless
    # for other endpoints (extra duplicated info) but only needed here.
    if cfg.endpoint:
        cfg = cfg.model_copy(
            update={"system_prompt": cfg.system_prompt + _gemma_tool_prelude(tool_defs)}
        )

    return run_agent(
        backend,
        cfg,
        task=task,
        tool_defs=tool_defs,
        tool_fns=tool_fns,
        session=session,
        tool_style="gemma_pycode" if cfg.endpoint else "openai",
    )
