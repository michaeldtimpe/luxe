"""Faithful reproduction of the real /review tool-loop on LM Studio.

The first probe (probe_lmstudio_tools.py) showed LM Studio threads
single-tool conversations correctly. So the loop bug must come from
something the real /review brings to the table:
  - long system prompt
  - 16 tools instead of 1
  - a directory-listing turn followed by deeper analysis turns

This script replays exactly that. Turn 1 asks for `list_dir(.)`,
feeds the result, and checks turn 2 for a synthesis (text) vs a
re-call of list_dir (the failure mode we observed in production).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import typer
import yaml

ROOT = Path(__file__).resolve().parent.parent

# Curated subset of the real review-agent tool defs (full schemas would
# need luxe_cli/tools/registry import; the harness loads them dynamically
# from there). For this probe we only need the SHAPE (count, names,
# descriptions) since the first failure we want to reproduce is the
# loop, not a specific tool's behaviour.
def _tool_def(name: str, desc: str, params: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", **params, "additionalProperties": False},
        },
    }

_TOOLS = [
    _tool_def("read_file", "Read a file by path.",
              {"properties": {"path": {"type": "string"}}, "required": ["path"]}),
    _tool_def("list_dir", "List the contents of a directory.",
              {"properties": {"path": {"type": "string"}}, "required": ["path"]}),
    _tool_def("glob", "Glob the repo for files matching a pattern.",
              {"properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}),
    _tool_def("grep", "Search file contents for a regex pattern.",
              {"properties": {"pattern": {"type": "string"},
                              "path": {"type": "string"}},
               "required": ["pattern"]}),
    _tool_def("git_diff", "Show git diff vs main.", {"properties": {}}),
    _tool_def("git_log", "Show recent commit log.",
              {"properties": {"n": {"type": "integer"}}}),
    _tool_def("git_show", "Show a specific commit.",
              {"properties": {"sha": {"type": "string"}}, "required": ["sha"]}),
    _tool_def("lint", "Run ruff lint over the repo.", {"properties": {}}),
    _tool_def("typecheck", "Run mypy over the repo.", {"properties": {}}),
    _tool_def("security_scan", "Run bandit security scan.",
              {"properties": {"min_severity": {"type": "string"}}}),
    _tool_def("deps_audit", "Audit dependencies for known vulns.", {"properties": {}}),
    _tool_def("security_taint", "Run semgrep with a config.",
              {"properties": {"config": {"type": "string"}}}),
    _tool_def("secrets_scan", "Scan for committed secrets.",
              {"properties": {"include_history": {"type": "boolean"}}}),
    _tool_def("lint_js", "Run eslint.", {"properties": {}}),
    _tool_def("typecheck_ts", "Run tsc --noEmit.", {"properties": {}}),
    _tool_def("lint_rust", "Run clippy.", {"properties": {}}),
    _tool_def("vet_go", "Run go vet.", {"properties": {}}),
]

_SYSTEM = """You are the code-review agent inside luxe. You read the repository
in the cwd with `list_dir`, `glob`, `grep`, and `read_file` — you
never write. You also have read-only git tools (`git_diff`,
`git_log`, `git_show`) to understand recent change context. Your
job is to find real, specific problems, not style nitpicks.

Focus areas in priority order:
1. Security — input validation, auth/authz gaps, secrets in code
   or history, command/SQL injection, path traversal, deserialization,
   dependency vulns.
2. Correctness — error-handling omissions, race conditions, off-by-one,
   resource leaks, wrong return types, silent failures.
3. Robustness — missing timeouts, missing retries on I/O, unbounded
   loops/recursion, missing rate-limiting.
4. Maintainability — duplication, dead code, outdated dependencies,
   missing or stale documentation that creates real risk.

For every finding, output:
- **File:line** (or `file:function` if line is inexact)
- **Severity** — critical / high / medium / low
- **Issue** — one sentence
- **Why** — one short paragraph; reference the actual code.
- **Suggested fix** — concrete, not vague.

Rules:
- Ground every finding in code you actually read via tools. No
  hallucinated filenames or quotes.
- Skip cosmetic issues unless they mask real bugs.
- Prefer depth over breadth: five substantive findings beat thirty
  shallow ones.
- If you find nothing material in an area, say so explicitly —
  don't pad the report.
"""

_USER = "List the root directory of the repository."

_FAKE_LISTDIR_RESULT = json.dumps([
    "README.md", "ARCHITECTURE.md", "CONTRIBUTING.md", "LICENSE",
    "Makefile", "src/", "tests/", "docs/", "scripts/",
    ".github/", "pyproject.toml", "uv.lock",
])


def _post(url: str, model: str, messages: list[dict]) -> dict:
    api_key = os.environ.get("OMLX_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": model, "messages": messages,
        "tools": _TOOLS, "tool_choice": "auto",
        "max_tokens": 1024, "temperature": 0.2, "stream": False,
    }
    r = httpx.post(f"{url}/v1/chat/completions", headers=headers,
                   json=payload, timeout=300.0)
    r.raise_for_status()
    return r.json()


def _summarize(label: str, body: dict) -> dict:
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    return {
        "finish_reason": (body.get("choices") or [{}])[0].get("finish_reason"),
        "n_tool_calls": len(tool_calls),
        "content_len": len(content),
        "content_head": content[:200],
        "tool_call_names": [(c.get("function") or {}).get("name") for c in tool_calls],
        "tool_call_args": [(c.get("function") or {}).get("arguments") for c in tool_calls],
    }


def main(
    backend: str = typer.Option("lmstudio", "--backend"),
    base_url: str = typer.Option("http://127.0.0.1:1234", "--url"),
    model: str = typer.Option("qwen2.5-32b-instruct", "--model"),
) -> None:
    typer.echo(f"\n=== {backend}  ({base_url}, model={model}) ===")
    typer.echo(f"  17 tools, full review system prompt ({len(_SYSTEM)} chars)\n")

    msgs1 = [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": _USER}]
    typer.echo("Turn 1 — expect tool_calls=[list_dir(...)]")
    body1 = _post(base_url, model, msgs1)
    s1 = _summarize("turn1", body1)
    typer.echo(json.dumps(s1, indent=2))

    if s1["n_tool_calls"] == 0:
        typer.echo("  ❌ no tool call on turn 1 — model not following prompt.")
        return

    msg1 = (body1.get("choices") or [{}])[0].get("message") or {}
    tc = (msg1.get("tool_calls") or [])[0]
    tc_id = tc.get("id") or "call_0"

    msgs2 = msgs1 + [
        {"role": "assistant", "content": msg1.get("content") or "",
         "tool_calls": [tc]},
        {"role": "tool", "tool_call_id": tc_id, "content": _FAKE_LISTDIR_RESULT},
    ]
    typer.echo("\nTurn 2 — expect synthesis OR a different tool, NOT another list_dir(.)")
    body2 = _post(base_url, model, msgs2)
    s2 = _summarize("turn2", body2)
    typer.echo(json.dumps(s2, indent=2))

    re_listdir = "list_dir" in s2["tool_call_names"]
    if re_listdir:
        # Compare args.
        first_args = s1["tool_call_args"][0] if s1["tool_call_args"] else ""
        repeat_args = s2["tool_call_args"][s2["tool_call_names"].index("list_dir")]
        same = (first_args.strip() == repeat_args.strip())
        typer.echo(f"\n❌ FAIL — model re-called list_dir on turn 2.")
        typer.echo(f"   turn1 args: {first_args}")
        typer.echo(f"   turn2 args: {repeat_args}")
        typer.echo(f"   identical: {same}")
    elif s2["n_tool_calls"] > 0:
        typer.echo(f"\n✅ progress — turn 2 picked a DIFFERENT tool: "
                   f"{s2['tool_call_names']}")
    else:
        typer.echo(f"\n✅ synthesis — turn 2 produced {s2['content_len']}B of text")


if __name__ == "__main__":
    typer.run(main)
