"""Probe whether LM Studio honors OpenAI-format `role: tool` messages.

The hypothesis: LM Studio is dropping or remapping prior-turn tool
results, so the model on turn N+1 doesn't see what it learned on
turn N — and just re-issues the same tool call. The 256 stuck
tool_call_begin events in our LM Studio /review tasks (every subtask
calling the same tool 20 times in a row) match that pattern.

Test design — same 3-turn conversation against three backends in
parallel:

  Turn 1:  system + user("list the root, then say done")
  → Expected: assistant message with tool_calls=[list_dir({"path":"."})]

  Turn 2:  Turn 1 + assistant(tool_call) + tool(content="README.md\\nsrc/")
  → Expected: assistant message with content describing the listing
              and saying "done", finish_reason=stop, NO new tool calls.

If LM Studio re-calls the tool on turn 2, the OpenAI-compat shim is
not threading `role: tool` correctly. If it produces text but the
text says "I'll list the directory now", the chat template embedded
in the GGUF is dropping the tool result. Either way: actionable.

Run:

    OMLX_API_KEY=… uv run python scripts/probe_lmstudio_tools.py
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import typer

# Tool-call format the agent loop sends. Identical to what luxe_cli/agents/base.py
# uses on the wire, just with a fake tool. We use list_dir because that's
# the tool every blocked LM Studio /review subtask 01 was looping on.
_TOOLS = [{
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List the contents of a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "directory path"},
            },
            "required": ["path"],
        },
    },
}]

_SYSTEM = (
    "You are a code-review agent. Use the list_dir tool when asked. "
    "After receiving a directory listing, summarize it briefly and say done."
)
_USER = "List the root directory ('.') and then say done."

# A plausible tool result the agent loop would feed back.
_FAKE_TOOL_RESULT = "README.md\nsrc/\nLICENSE\nMakefile\n"

_BACKENDS = {
    "ollama":   ("http://127.0.0.1:11434", "qwen2.5:32b-instruct"),
    "omlx":     ("http://127.0.0.1:8000",  "Qwen2.5-32B-Instruct-4bit"),
    "lmstudio": ("http://127.0.0.1:1234",  "qwen2.5-32b-instruct"),
}


def _post(url: str, model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Send a single chat completion request mirroring the harness shape."""
    api_key = os.environ.get("OMLX_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": model,
        "messages": messages,
        "tools": _TOOLS,
        "tool_choice": "auto",
        "max_tokens": 512,
        "temperature": 0.1,
        "stream": False,
    }
    r = httpx.post(f"{url}/v1/chat/completions", headers=headers,
                   json=payload, timeout=120.0)
    r.raise_for_status()
    return r.json()


def _summarize_response(label: str, body: dict) -> dict:
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    finish = (body.get("choices") or [{}])[0].get("finish_reason")
    summary = {
        "finish_reason": finish,
        "n_tool_calls": len(tool_calls),
        "content_len": len(content),
        "content_head": content[:120],
        "tool_call_names": [
            (c.get("function") or {}).get("name") for c in tool_calls
        ],
        "tool_call_args_head": [
            ((c.get("function") or {}).get("arguments") or "")[:80]
            for c in tool_calls
        ],
    }
    print(f"  [{label}] {json.dumps(summary, indent=2)}")
    return summary


def _run_one(name: str, base_url: str, model: str) -> None:
    typer.echo(f"\n=== {name}  ({base_url}, model={model}) ===")

    # Turn 1: ask the model to use the tool.
    msgs1 = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER},
    ]
    typer.echo("Turn 1 — expect assistant with tool_calls=[list_dir(.)]")
    try:
        body1 = _post(base_url, model, msgs1)
    except Exception as e:  # noqa: BLE001
        typer.echo(f"  [{name}] turn 1 FAILED: {type(e).__name__}: {e}")
        return
    sum1 = _summarize_response("turn1", body1)
    if sum1["n_tool_calls"] == 0:
        typer.echo(f"  [{name}] no tool call on turn 1 — model isn't following "
                   "the system prompt with this backend. Bail.")
        return

    # Take the first tool call to thread back.
    msg1 = (body1.get("choices") or [{}])[0].get("message") or {}
    tool_call = (msg1.get("tool_calls") or [])[0]
    tc_id = tool_call.get("id") or "call_0"

    # Turn 2: feed the tool result back.
    msgs2 = msgs1 + [
        {
            "role": "assistant",
            "content": msg1.get("content") or "",
            "tool_calls": [tool_call],
        },
        {
            "role": "tool",
            "tool_call_id": tc_id,
            "content": _FAKE_TOOL_RESULT,
        },
    ]
    typer.echo("Turn 2 — expect assistant with content (no new tool_calls)")
    try:
        body2 = _post(base_url, model, msgs2)
    except Exception as e:  # noqa: BLE001
        typer.echo(f"  [{name}] turn 2 FAILED: {type(e).__name__}: {e}")
        return
    sum2 = _summarize_response("turn2", body2)

    # Verdict.
    if sum2["n_tool_calls"] > 0:
        # Did it re-issue list_dir specifically?
        names = sum2["tool_call_names"]
        repeats = [n for n in names if n == "list_dir"]
        if repeats:
            args = sum2["tool_call_args_head"]
            typer.echo(f"  [{name}] ❌ FAIL — model RE-CALLED list_dir on turn 2 "
                       f"({len(repeats)}× args={args}). Tool result not threaded.")
        else:
            typer.echo(f"  [{name}] ⚠ partial — tool_calls but not list_dir; "
                       f"may be acceptable.")
    elif sum2["content_len"] == 0:
        typer.echo(f"  [{name}] ❌ FAIL — empty content on turn 2.")
    else:
        # Did the response actually reference the tool's result?
        head = sum2["content_head"].lower()
        ack = any(t in head for t in ("readme", "src", "license", "makefile",
                                      "directory", "files", "listing"))
        ack_str = "✅" if ack else "⚠"
        typer.echo(f"  [{name}] {ack_str} content present; references tool "
                   f"output={ack}. Snippet: {sum2['content_head']!r}")


def main() -> None:
    for name, (url, model) in _BACKENDS.items():
        _run_one(name, url, model)


if __name__ == "__main__":
    typer.run(main)
