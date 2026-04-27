"""Pre-flight probe: validate gemma-3-27b-it-4bit on oMLX with tool calls.

The writing agent currently uses tool_style="gemma_pycode" against
llama-server because llama-server's Gemma 3 jinja template doesn't render
`tools=`. The injected Python-signature prelude teaches the model to emit
```tool_code``` blocks.

We test the same approach on oMLX. Decision rule:
  - Model must produce a parseable ```tool_code``` block calling
    list_dir(path=".") or similar within 1 turn.
  - Falling back to free prose, returning empty, or HTTP 4xx = stay on
    llama-server.

Two attempts:
  1. With the existing _gemma_tool_prelude pattern (Python signature +
     ```tool_code``` format).
  2. With OpenAI-style `tools=[...]` parameter (in case oMLX renders it).
"""

from __future__ import annotations

import json
import os
import re
import sys

import httpx


BASE = "http://127.0.0.1:8000"
MODEL = "gemma-3-27b-it-4bit"

_PRELUDE = (
    "# Available tools\n"
    "You have the following Python functions available. To call one, "
    "respond with a single ```tool_code``` block containing exactly one "
    "call, then stop. The harness will execute it and return the result "
    "in a ```tool_output``` block on the next turn. Use keyword arguments.\n\n"
    "```python\n"
    "def list_dir(path):\n    \"\"\"List files in a directory.\"\"\"\n\n"
    "def read_file(path):\n    \"\"\"Read a file's contents.\"\"\"\n"
    "```\n\n"
    "Example:\n"
    "```tool_code\n"
    "list_dir(path=\".\")\n"
    "```\n"
)


def call(messages, tools=None, api_key="") -> tuple[int, str, str]:
    body = {"model": MODEL, "messages": messages, "max_tokens": 256, "temperature": 0.2, "stream": False}
    if tools:
        body["tools"] = tools
    r = httpx.post(
        f"{BASE}/v1/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120.0,
    )
    if r.status_code != 200:
        return r.status_code, "", r.text[:300]
    data = r.json()
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    return 200, content, json.dumps(tool_calls) if tool_calls else ""


def main() -> int:
    key = os.environ.get("OMLX_API_KEY", "").strip()
    if not key:
        print("OMLX_API_KEY unset", file=sys.stderr)
        return 2

    print("=== Test 1: Python-signature prelude (gemma_pycode style) ===")
    sys_prompt = (
        "You are a creative-writing assistant with filesystem tools.\n\n"
        + _PRELUDE
    )
    user = "I want to know what's in the current folder. Use a tool to find out."
    code, content, tcalls = call(
        [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
        api_key=key,
    )
    print(f"HTTP {code}")
    print(f"content (first 400): {content[:400]!r}")
    print(f"tool_calls: {tcalls!r}")
    pycode_ok = code == 200 and bool(re.search(r"```tool_code\s*\n.*list_dir\s*\(", content, re.DOTALL))
    print(f"  -> pycode parseable: {pycode_ok}")

    print("\n=== Test 2: OpenAI tools= parameter ===")
    tools = [{
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files in a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }]
    code, content, tcalls = call(
        [
            {"role": "system", "content": "You are a creative-writing assistant with filesystem tools."},
            {"role": "user", "content": user},
        ],
        tools=tools,
        api_key=key,
    )
    print(f"HTTP {code}")
    print(f"content (first 400): {content[:400]!r}")
    print(f"tool_calls: {tcalls!r}")
    openai_ok = code == 200 and bool(tcalls)
    print(f"  -> openai tool_calls parseable: {openai_ok}")

    print("\n=== Verdict ===")
    if openai_ok:
        print("Gemma on oMLX supports OpenAI tool_calls natively. Use tool_style=openai, no prelude.")
    elif pycode_ok:
        print("Gemma on oMLX needs the gemma_pycode prelude (same as llama-server).")
    else:
        print("Gemma on oMLX did NOT produce parseable tool calls in either mode.")
        print("Recommendation: writing stays on llama-server.")
    return 0 if (openai_ok or pycode_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
