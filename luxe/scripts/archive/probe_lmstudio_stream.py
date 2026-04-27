"""Capture LM Studio's raw SSE chunks for a tool-emitting turn.

Hypothesis: LM Studio's streaming protocol places tool-call args in
a field name or shape that the harness's stream accumulator doesn't
recognize, so `slot["arguments"]` stays empty -> parsed args = {} ->
the model receives its own prior `list_dir({})` and re-issues.
"""

from __future__ import annotations

import json
import os

import httpx
import typer

_TOOLS = [{
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List the contents of a directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}]


def main(
    url: str = typer.Option("http://127.0.0.1:1234"),
    model: str = typer.Option("qwen2.5-32b-instruct"),
) -> None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a code-review agent. Use list_dir when asked."},
            {"role": "user", "content": "List the root directory '.'."},
        ],
        "tools": _TOOLS,
        "tool_choice": "auto",
        "max_tokens": 256,
        "temperature": 0.1,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    api_key = os.environ.get("OMLX_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    typer.echo(f"=== streaming POST {url}/v1/chat/completions ===\n")
    with httpx.Client(base_url=url, timeout=120.0) as c:
        with c.stream("POST", "/v1/chat/completions",
                      json=payload, headers=headers) as r:
            r.raise_for_status()
            n = 0
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    typer.echo("[DONE]")
                    break
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    typer.echo(f"  (could not parse: {raw[:120]!r})")
                    continue
                n += 1
                # Print the delta verbatim so we see the exact field shape.
                choices = evt.get("choices") or []
                delta = choices[0].get("delta") if choices else None
                finish = choices[0].get("finish_reason") if choices else None
                if delta is None and finish is None and not evt.get("usage"):
                    continue
                typer.echo(f"[{n:>3d}] finish={finish}  delta={json.dumps(delta) if delta else '(none)'}")
            typer.echo(f"\ntotal events: {n}")


if __name__ == "__main__":
    typer.run(main)
