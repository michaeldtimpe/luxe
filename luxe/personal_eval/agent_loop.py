"""Minimal agent loop for Phase B2 (write replay).

Gives the model a small, safe tool surface (read/list/grep/write/shell) scoped
to a single repo checkout, runs until the model emits no further tool calls or
we hit the step budget, and records every turn for metrics.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from harness.backends import Backend, ToolCall, ToolDef
from harness.metrics import RunMetrics

_console = Console()

MAX_FILE_BYTES = 256 * 1024


@dataclass
class AgentConfig:
    max_steps: int = 12
    shell_allowlist: tuple[str, ...] = (
        "cargo",
        "pytest",
        "go",
        "python",
        "python3",
        "rustc",
        "ls",
        "pwd",
    )
    max_tokens_per_turn: int = 2048
    temperature: float = 0.2
    # Abort a task if a single response emits more than this many tool
    # calls — indicates the model is in a read_file/list_dir loop.
    max_tool_calls_per_turn: int = 20
    # Abort the task if total wall time exceeds this (seconds).
    max_wall_s: float = 600.0


@dataclass
class AgentResult:
    final_text: str
    tool_calls_total: int
    steps_taken: int
    files_touched: list[str]
    shell_commands: list[str] = field(default_factory=list)
    test_exit_code: int | None = None
    final_diff: str = ""
    first_turn_raw_text: str = ""
    first_turn_finish_reason: str = ""


def tools_for(repo_root: Path) -> list[ToolDef]:
    return [
        ToolDef(
            name="read_file",
            description="Read a UTF-8 text file. Paths are relative to the repo root.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        ToolDef(
            name="list_dir",
            description="List entries in a directory.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "required": [],
            },
        ),
        ToolDef(
            name="grep",
            description="Ripgrep over the repo. Returns first 100 matches.",
            parameters={
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"}},
                "required": ["pattern"],
            },
        ),
        ToolDef(
            name="write_file",
            description="Write UTF-8 text to a file (creates or overwrites).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        ),
        ToolDef(
            name="shell",
            description=(
                "Run an allowlisted shell command in the repo root. Allowed "
                "leading binaries: cargo, pytest, go, python, python3, rustc, ls, pwd."
            ),
            parameters={
                "type": "object",
                "properties": {"cmd": {"type": "string"}, "timeout_s": {"type": "integer", "default": 120}},
                "required": ["cmd"],
            },
        ),
    ]


def run_agent(
    backend: Backend,
    *,
    repo_root: Path,
    task_description: str,
    system_prompt: str,
    metrics: RunMetrics,
    config: AgentConfig | None = None,
) -> AgentResult:
    config = config or AgentConfig()
    tools = tools_for(repo_root)
    metrics.known_tool_names = {t.name for t in tools}

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_description},
    ]

    import time as _time

    files_touched: list[str] = []
    shell_commands: list[str] = []
    last_test_exit: int | None = None
    tool_calls_total = 0
    step = 0
    first_turn_raw = ""
    first_turn_finish = ""
    started_at = _time.monotonic()

    while step < config.max_steps:
        if _time.monotonic() - started_at > config.max_wall_s:
            _console.log(
                f"  [agent] wall budget {config.max_wall_s}s exceeded; aborting"
            )
            break
        response = backend.chat(
            messages,
            tools=tools,
            max_tokens=config.max_tokens_per_turn,
            temperature=config.temperature,
        )
        if step == 0:
            first_turn_raw = response.text[:2000]
            first_turn_finish = response.finish_reason

        # mlx-lm's OpenAI-compat server returns Qwen/Hermes-style tool
        # calls as raw JSON in the `content` field instead of structured
        # `tool_calls`. Rescue them here so the agent loop works.
        if not response.tool_calls and response.text:
            recovered = _parse_text_tool_calls(
                response.text, {t.name for t in tools}
            )
            if recovered:
                response.tool_calls = recovered
                response.text = ""

        tool_calls_total += len(response.tool_calls)
        _console.log(
            f"  [agent step {step}] tool_calls={len(response.tool_calls)} "
            f"text_chars={len(response.text)} finish={response.finish_reason}"
        )

        if len(response.tool_calls) > config.max_tool_calls_per_turn:
            _console.log(
                f"  [agent] runaway turn ({len(response.tool_calls)} calls "
                f"> {config.max_tool_calls_per_turn}); aborting task"
            )
            metrics.record_turn(step, response, had_recoverable_error=True)
            break

        turn_results: list[dict[str, Any]] = []
        had_error = False

        if response.text:
            messages.append({"role": "assistant", "content": response.text})

        if not response.tool_calls:
            metrics.record_turn(step, response)
            break

        messages.append(
            {
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.raw_arguments},
                    }
                    for tc in response.tool_calls
                ],
            }
        )

        for call in response.tool_calls:
            result, err = _dispatch(
                call.name,
                call.arguments,
                repo_root=repo_root,
                allowlist=config.shell_allowlist,
            )
            if call.name == "write_file":
                files_touched.append(call.arguments.get("path", ""))
            elif call.name == "shell":
                shell_commands.append(call.arguments.get("cmd", ""))
                if any(k in call.arguments.get("cmd", "") for k in ("test", "pytest")):
                    last_test_exit = result.get("exit_code") if isinstance(result, dict) else None
            if err:
                had_error = True
            turn_results.append({"tool": call.name, "result": result, "error": err})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": _trim(result if not err else f"ERROR: {err}"),
                }
            )

        metrics.record_turn(step, response, turn_results, had_recoverable_error=had_error)
        step += 1

    final_diff = _git_diff(repo_root)

    return AgentResult(
        final_text=response.text if step < config.max_steps else "",
        tool_calls_total=tool_calls_total,
        steps_taken=step,
        files_touched=files_touched,
        shell_commands=shell_commands,
        test_exit_code=last_test_exit,
        final_diff=final_diff,
        first_turn_raw_text=first_turn_raw,
        first_turn_finish_reason=first_turn_finish,
    )


def _dispatch(
    name: str,
    args: dict[str, Any],
    *,
    repo_root: Path,
    allowlist: tuple[str, ...],
) -> tuple[Any, str | None]:
    try:
        if name == "read_file":
            return _read_file(repo_root, args["path"]), None
        if name == "list_dir":
            return _list_dir(repo_root, args.get("path", ".")), None
        if name == "grep":
            return _grep(repo_root, args["pattern"], args.get("glob")), None
        if name == "write_file":
            return _write_file(repo_root, args["path"], args["content"]), None
        if name == "shell":
            return _shell(
                repo_root,
                args["cmd"],
                int(args.get("timeout_s", 120)),
                allowlist,
            ), None
        return None, f"unknown tool: {name}"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _safe_path(repo_root: Path, rel: str) -> Path:
    p = (repo_root / rel).resolve()
    if repo_root.resolve() not in p.parents and p != repo_root.resolve():
        raise PermissionError(f"path escapes repo root: {rel}")
    return p


def _read_file(repo_root: Path, rel: str) -> str:
    p = _safe_path(repo_root, rel)
    data = p.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        data = data[:MAX_FILE_BYTES]
    return data.decode("utf-8", errors="replace")


def _list_dir(repo_root: Path, rel: str) -> list[str]:
    p = _safe_path(repo_root, rel)
    return sorted(entry.name + ("/" if entry.is_dir() else "") for entry in p.iterdir())


def _grep(repo_root: Path, pattern: str, glob: str | None) -> list[str]:
    args = ["rg", "--no-heading", "--color=never", "-n", pattern]
    if glob:
        args += ["-g", glob]
    res = subprocess.run(  # noqa: S603
        args, cwd=repo_root, capture_output=True, text=True, timeout=30
    )
    lines = (res.stdout or "").splitlines()
    return lines[:100]


def _write_file(repo_root: Path, rel: str, content: str) -> str:
    p = _safe_path(repo_root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} bytes to {rel}"


def _shell(repo_root: Path, cmd: str, timeout_s: int, allowlist: tuple[str, ...]) -> dict[str, Any]:
    try:
        parts = shlex.split(cmd)
    except ValueError as e:
        raise ValueError(f"unparseable command: {e}") from e
    if not parts or parts[0] not in allowlist:
        raise PermissionError(
            f"command '{parts[0] if parts else ''}' not in allowlist {allowlist}"
        )
    res = subprocess.run(  # noqa: S603
        parts,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return {
        "exit_code": res.returncode,
        "stdout": _trim(res.stdout, 8000),
        "stderr": _trim(res.stderr, 8000),
    }


def _git_diff(repo_root: Path) -> str:
    res = subprocess.run(  # noqa: S603
        ["git", "diff"], cwd=repo_root, capture_output=True, text=True
    )
    return res.stdout


def _trim(value: Any, limit: int = 4000) -> str:
    s = value if isinstance(value, str) else str(value)
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated {len(s) - limit} bytes]"


_TOOL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_JSON_BLOCK_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_text_tool_calls(text: str, known_names: set[str]) -> list[ToolCall]:
    """Parse text-embedded tool calls for servers that don't honor OpenAI
    `tool_calls` response structure. Supports three common shapes:

    1. ``<tool_call>{"name": ..., "arguments": ...}</tool_call>`` (Qwen / Hermes)
    2. Raw JSON lines, one per call.
    3. Fenced ``` blocks containing tool-call JSON.
    """
    candidates: list[dict[str, Any]] = []

    for m in _TOOL_TAG_RE.finditer(text):
        obj = _safe_json(m.group(1))
        if obj:
            candidates.append(obj)

    if not candidates:
        for m in _JSON_BLOCK_RE.finditer(text):
            obj = _safe_json(m.group(1))
            if obj:
                candidates.append(obj)

    if not candidates:
        # Raw JSON lines: ignore commentary, parse any line that is a
        # standalone object with a `name` field.
        for line in text.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("{") and stripped.endswith("}")):
                continue
            obj = _safe_json(stripped)
            if obj and obj.get("name"):
                candidates.append(obj)

    calls: list[ToolCall] = []
    for i, obj in enumerate(candidates):
        name = obj.get("name")
        if name not in known_names:
            continue
        args = obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, str):
            args_obj = _safe_json(args) or {}
            raw_args = args
        else:
            args_obj = args
            raw_args = json.dumps(args)
        calls.append(
            ToolCall(
                id=f"text_{i}",
                name=name,
                arguments=args_obj,
                raw_arguments=raw_args,
            )
        )
    return calls


def _safe_json(s: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
