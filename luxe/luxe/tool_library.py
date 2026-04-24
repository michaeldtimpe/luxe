"""User-writable tool library.

Idea: agents can save reusable computations as named Python tools, and
future tasks automatically get those tools injected when their names /
tags / description-keywords match the task.

Each tool lives at `~/.luxe/tools/<name>.py` as a Python file whose
module docstring carries a YAML front-matter block:

    \"\"\"
    ---
    name: ev_charging_time
    description: Estimate charging time + cost for an EV trip.
    tags: [ev, charging, trip]
    parameters:
      type: object
      properties:
        distance_mi: {type: number}
        ...
      required: [distance_mi, ...]
    ---
    \"\"\"

    def compute(**kwargs):
        ...
        return {...}

Safety: new tool code is AST-scanned before being saved. Imports, file
I/O, shell-outs, and dunder attribute access are rejected. Loading a
saved tool still runs `exec` (single user, local machine), so treat
this like any other persistent user script — review before executing
tools shared by third parties.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from harness.backends import ToolDef

TOOLS_ROOT = Path.home() / ".luxe" / "tools"
_HEADER_RE = re.compile(r'^"""\s*\n---\s*\n(.*?)\n---\s*\n"""', re.DOTALL)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


# ── Safety ────────────────────────────────────────────────────────────

_DISALLOWED_NODES = (
    ast.Import, ast.ImportFrom, ast.AsyncFunctionDef,
    ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith,
    ast.Try, ast.Raise,  # keep it pure — no exception surprises
)
_DISALLOWED_CALL_NAMES = {
    "exec", "eval", "open", "compile", "__import__",
    "input", "breakpoint", "getattr", "setattr", "delattr",
}


def is_safe_code(code: str) -> tuple[bool, str]:
    """Validate `code` for saving as a tool. Returns (ok, reason)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"syntax error: {e}"
    for node in ast.walk(tree):
        if isinstance(node, _DISALLOWED_NODES):
            return False, f"disallowed construct: {type(node).__name__}"
        if isinstance(node, ast.Attribute) and str(node.attr).startswith("__"):
            return False, f"disallowed dunder access: {node.attr}"
        if isinstance(node, ast.Name) and str(node.id).startswith("__"):
            return False, f"disallowed dunder name: {node.id}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _DISALLOWED_CALL_NAMES:
                return False, f"disallowed call: {node.func.id}"
    if "def compute" not in code:
        return False, "code must define a `def compute(...)` function"
    return True, ""


# ── Storage ───────────────────────────────────────────────────────────


def _parse_header(file_text: str) -> dict | None:
    m = _HEADER_RE.search(file_text)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def list_tools() -> list[dict]:
    """Return metadata for every tool in TOOLS_ROOT. `_path` is added
    so callers can find the source file."""
    if not TOOLS_ROOT.exists():
        return []
    out: list[dict] = []
    for f in sorted(TOOLS_ROOT.glob("*.py")):
        meta = _parse_header(f.read_text())
        if not meta or not meta.get("name"):
            continue
        meta["_path"] = str(f)
        out.append(meta)
    return out


def save_tool(
    name: str,
    description: str,
    parameters: dict,
    python_code: str,
    tags: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate + persist a new tool. Returns (ok, path-or-error-message)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return False, "name must be snake_case, lowercase, 1–48 chars"
    ok, err = is_safe_code(python_code)
    if not ok:
        return False, err
    if not isinstance(parameters, dict):
        return False, "parameters must be a JSON-schema object"

    TOOLS_ROOT.mkdir(parents=True, exist_ok=True)
    path = TOOLS_ROOT / f"{name}.py"
    header = {
        "name": name,
        "description": description,
        "tags": tags or [],
        "parameters": parameters,
    }
    body = (
        '"""\n---\n'
        + yaml.safe_dump(header, sort_keys=False).strip()
        + '\n---\n"""\n\n'
        + python_code.rstrip()
        + "\n"
    )
    path.write_text(body)
    return True, str(path)


# ── Loading + invocation ──────────────────────────────────────────────


def load_callable(meta: dict) -> Callable | None:
    """Exec the tool's source in an isolated namespace and return the
    `compute` function, or None on failure."""
    path = Path(meta["_path"])
    ns: dict[str, Any] = {}
    try:
        exec(compile(path.read_text(), str(path), "exec"), ns)  # noqa: S102
    except Exception:  # noqa: BLE001
        return None
    fn = ns.get("compute")
    return fn if callable(fn) else None


def tool_def_from_meta(meta: dict) -> ToolDef:
    """Project metadata into the ToolDef shape the agent loop expects."""
    return ToolDef(
        name=meta["name"],
        description=meta.get("description", ""),
        parameters=meta.get("parameters") or {"type": "object", "properties": {}},
    )


def tool_fn_from_meta(meta: dict) -> Callable[[dict[str, Any]], tuple[Any, str | None]]:
    """Wrap the tool's `compute` function in luxe's `(args) -> (result, err)`
    tool-dispatch shape."""
    def _call(args: dict[str, Any]) -> tuple[Any, str | None]:
        fn = load_callable(meta)
        if fn is None:
            return None, f"tool `{meta.get('name', '?')}` failed to load"
        try:
            result = fn(**(args or {}))
        except TypeError as e:
            return None, f"bad arguments: {e}"
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"
        return str(result), None
    return _call


# ── Matching ──────────────────────────────────────────────────────────


def match_tools(task_text: str, limit: int = 5) -> list[dict]:
    """Return tools whose name/tags/description overlap with the task
    text. Simple keyword scoring — good enough without embeddings."""
    if not task_text:
        return []
    task = task_text.lower()
    scored: list[tuple[int, dict]] = []
    for meta in list_tools():
        score = 0
        for tag in meta.get("tags") or []:
            if isinstance(tag, str) and tag.lower() in task:
                score += 3
        name = (meta.get("name") or "").lower()
        if name and name in task:
            score += 4
        for word in (meta.get("description") or "").lower().split():
            if len(word) > 4 and word in task:
                score += 1
        if score > 0:
            scored.append((score, meta))
    scored.sort(key=lambda p: p[0], reverse=True)
    return [m for _, m in scored[:limit]]


# ── `create_tool` agent-callable tool ────────────────────────────────

CREATE_TOOL_DEF = ToolDef(
    name="create_tool",
    description=(
        "Save a reusable computation as a named Python tool for future "
        "tasks. The saved tool runs in a safe namespace — no imports, no "
        "I/O, no exec/eval. Once saved, tools whose name/tags match a "
        "future task get auto-injected into the agent that would handle "
        "it, so you only write each computation once. Only use when the "
        "formula is clearly reusable; don't save trivial one-liners."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "snake_case lowercase identifier, e.g. ev_charging_time",
            },
            "description": {
                "type": "string",
                "description": "one-line explanation of what the tool computes",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "keywords for matching to future tasks (e.g. ['ev', 'charging'])",
            },
            "parameters": {
                "type": "object",
                "description": "JSON-schema object describing the compute() kwargs",
            },
            "python_code": {
                "type": "string",
                "description": (
                    "Full Python source defining `def compute(**kwargs): ...`. "
                    "Must return a dict, string, or number. No imports, no I/O."
                ),
            },
        },
        "required": ["name", "description", "parameters", "python_code"],
    },
)


def create_tool_fn(args: dict[str, Any]) -> tuple[Any, str | None]:
    ok, result = save_tool(
        name=str(args.get("name", "")).strip(),
        description=str(args.get("description", "")).strip(),
        parameters=args.get("parameters") or {},
        python_code=str(args.get("python_code") or ""),
        tags=list(args.get("tags") or []),
    )
    if ok:
        return f"saved tool to {result}", None
    return None, result
