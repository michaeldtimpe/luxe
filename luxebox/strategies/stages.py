"""Stage registry for compression-benchmark strategies.

Stages are keyed by string id (`preprocess`, `index`, `retrieve`,
`compress`, `prompt_assembly`). Each stage function mutates a shared
`Context`. The pipeline runner times `retrieve` and `compress` so the
benchmark can populate RunMetrics without each stage having to know
about metrics.
"""

from __future__ import annotations

import ast
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from shared.trace_hints import parse_trace_paths

StageFn = Callable[["Context", dict[str, Any]], None]

_REGISTRY: dict[str, StageFn] = {}


def register(stage_id: str) -> Callable[[StageFn], StageFn]:
    def deco(fn: StageFn) -> StageFn:
        _REGISTRY[stage_id] = fn
        return fn

    return deco


@dataclass
class Context:
    repo_root: Path
    task_description: str
    entry_instruction: str = ""
    goal: str = ""

    # Benchmark-supplied reference data (relevant_files for oracle
    # retrieval, validation command for stack-trace-guided retrieval).
    reference: dict[str, Any] = field(default_factory=dict)

    # Populated by stages.
    indexed_files: list[Path] = field(default_factory=list)
    files_in_scope: list[Path] = field(default_factory=list)
    compressed_text: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    # Set by prompt_assembly; read by the benchmark grader to know how
    # to interpret the model output. Values: "unified_diff" (default),
    # "whole_file".
    output_format: str = "unified_diff"

    # Optional backend handle for compress stages that call the model
    # (e.g. summarize). The benchmark sets this; unit tests and pure
    # retrieval strategies leave it None.
    backend: Any = None

    # Written by the pipeline runner.
    t_retrieval_s: float = 0.0
    t_compression_s: float = 0.0

    # Approximate prompt size after assembly (char-based estimate ÷ 4 if
    # the benchmark can't do a real tokenizer pass).
    assembled_prompt_chars: int = 0


_TIMED_STAGES = {"retrieve": "t_retrieval_s", "compress": "t_compression_s"}


def run_pipeline(strategy: dict[str, Any], ctx: Context) -> Context:
    for stage in strategy.get("stages", []):
        if not stage.get("enabled", True):
            continue
        sid = stage["id"]
        params = stage.get("params", {}) or {}
        fn = _REGISTRY.get(sid)
        if fn is None:
            raise KeyError(f"unknown stage id: {sid!r}")
        t0 = time.perf_counter()
        fn(ctx, params)
        elapsed = time.perf_counter() - t0
        if sid in _TIMED_STAGES:
            setattr(ctx, _TIMED_STAGES[sid], getattr(ctx, _TIMED_STAGES[sid]) + elapsed)
    return ctx


# --------------------------------------------------------------------------
# Built-in stage implementations.
# --------------------------------------------------------------------------


_TEXT_EXTS = {".py", ".md", ".txt", ".cfg", ".toml", ".ini", ".yaml", ".yml", ".rst"}
_IGNORE_DIRS = {".git", ".venv", "__pycache__", "node_modules", "dist", "build", ".pytest_cache"}


def _walk_text_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _IGNORE_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in _TEXT_EXTS:
            continue
        out.append(p)
    return out


@register("preprocess")
def preprocess(ctx: Context, params: dict[str, Any]) -> None:
    """No-op for the baseline strategy. Hook for future formatting/
    normalization passes."""
    return None


@register("index")
def index(ctx: Context, params: dict[str, Any]) -> None:
    """Enumerate candidate files in the repo. Future methods ("symbols",
    "embeddings") can attach richer metadata; for now we just list text
    files."""
    ctx.indexed_files = _walk_text_files(ctx.repo_root)


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _tokenize(text: str) -> set[str]:
    return {tok.lower() for tok in _TOKEN_RE.findall(text)}


@register("retrieve")
def retrieve(ctx: Context, params: dict[str, Any]) -> None:
    """Dispatch to a retrieval method by name. `keyword_topk` is the
    default baseline; `oracle`, `full`, and `none` are controls that
    isolate retrieval quality from model/prompt effects."""
    method = params.get("method", "keyword_topk")
    if method == "none":
        ctx.files_in_scope = []
        return
    if method == "full":
        if not ctx.indexed_files:
            ctx.indexed_files = _walk_text_files(ctx.repo_root)
        ctx.files_in_scope = list(ctx.indexed_files)
        return
    if method == "oracle":
        rel = list(ctx.reference.get("relevant_files", []))
        resolved: list[Path] = []
        for name in rel:
            p = (ctx.repo_root / name).resolve()
            if p.is_file():
                resolved.append(p)
        ctx.files_in_scope = resolved
        return
    if method == "keyword_topk":
        _retrieve_keyword_topk(ctx, params)
        return
    if method == "stack_trace":
        _retrieve_stack_trace(ctx, params)
        return
    raise KeyError(f"unknown retrieve method: {method!r}")


def _retrieve_stack_trace(ctx: Context, params: dict[str, Any]) -> None:
    """Run the task's validation command, parse pytest's traceback
    for `path.py:LINE` references, and seed retrieval from those.

    Models how a human debugger would start — look at what's actually
    failing, read those files first. Falls back to keyword_topk if
    the validation command is missing or yields no paths."""
    top_k = int(params.get("top_k_files", 8))
    cmd = (ctx.reference.get("validation") or {}).get("command") or []
    if not cmd:
        _retrieve_keyword_topk(ctx, params)
        return

    try:
        proc = subprocess.run(
            list(cmd), cwd=ctx.repo_root, capture_output=True, text=True, timeout=60
        )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _retrieve_keyword_topk(ctx, params)
        return

    # Path extraction is shared with luxe's orchestrator — keep the
    # regex + dedupe logic in one place.
    ordered = parse_trace_paths(output, ctx.repo_root)
    seen = set(ordered)

    if not ordered:
        _retrieve_keyword_topk(ctx, params)
        return

    # Top-up with keyword-matched files using symbols pulled from the
    # traceback — e.g. `def test_slugify_...` gives us "slugify", which
    # matches strings.py. Useful when the traceback is shallow and
    # doesn't directly name the buggy module.
    if not ctx.indexed_files:
        ctx.indexed_files = _walk_text_files(ctx.repo_root)
    symbols = _tokenize(output)
    if len(ordered) < top_k:
        scored: list[tuple[int, Path]] = []
        for path in ctx.indexed_files:
            if path in seen:
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            overlap = len(symbols & (_tokenize(text) | _tokenize(path.name)))
            if overlap > 0:
                scored.append((overlap, path))
        scored.sort(key=lambda x: (-x[0], str(x[1])))
        for _, p in scored:
            if len(ordered) >= top_k:
                break
            ordered.append(p)

    ctx.files_in_scope = ordered[:top_k]


def _retrieve_keyword_topk(ctx: Context, params: dict[str, Any]) -> None:
    """Naive top-k file retrieval by keyword overlap with the task
    description. Deliberately crude — it's the baseline you measure
    smarter strategies against."""
    top_k = int(params.get("top_k_files", 12))
    query_tokens = _tokenize(ctx.task_description + " " + ctx.goal)
    if not ctx.indexed_files:
        ctx.indexed_files = _walk_text_files(ctx.repo_root)

    scored: list[tuple[int, Path]] = []
    for path in ctx.indexed_files:
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        file_tokens = _tokenize(text) | _tokenize(path.name)
        overlap = len(query_tokens & file_tokens)
        if overlap > 0:
            scored.append((overlap, path))

    scored.sort(key=lambda pair: (-pair[0], str(pair[1])))
    ctx.files_in_scope = [p for _, p in scored[:top_k]]

    # Fall back to the whole index if nothing overlapped — better than
    # giving the model zero context.
    if not ctx.files_in_scope:
        ctx.files_in_scope = ctx.indexed_files[:top_k]


@register("compress")
def compress(ctx: Context, params: dict[str, Any]) -> None:
    """Dispatch to a compressor by name.

    Methods:
      - "none" (default): no-op.
      - "outlines": replace Python file bodies with module docstring +
        top-level def/class signatures + their first-line docstrings.
        Minimal token cost; tests whether the model needs contents or
        just the map.
      - "summarize": call the benchmark's backend (if set on ctx) to
        rewrite each file as bullet points relevant to the task. First
        real use of LLM-driven compression."""
    method = params.get("method", "none")
    if method == "none":
        return
    if method == "outlines":
        _compress_outlines(ctx, params)
        return
    if method == "summarize":
        _compress_summarize(ctx, params)
        return
    raise KeyError(f"unknown compress method: {method!r}")


def _ast_outline(source: str) -> str | None:
    """Return a compact Python-syntax outline: module docstring + each
    top-level def/class signature plus its first-line docstring.
    Returns None if parsing fails (caller falls back to raw content)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    lines: list[str] = []
    mod_doc = ast.get_docstring(tree, clean=False)
    if mod_doc:
        first_line = mod_doc.splitlines()[0].strip()
        lines.append(f'"""{first_line}"""')
        lines.append("")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(_fn_signature(node) + ":")
            doc = ast.get_docstring(node, clean=False)
            if doc:
                lines.append(f'    """{doc.splitlines()[0].strip()}"""')
            lines.append("    ...")
            lines.append("")
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(_expr_src(b) for b in node.bases)
            header = f"class {node.name}" + (f"({bases})" if bases else "") + ":"
            lines.append(header)
            doc = ast.get_docstring(node, clean=False)
            if doc:
                lines.append(f'    """{doc.splitlines()[0].strip()}"""')
            # Include method signatures inside the class.
            had_member = False
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append("    " + _fn_signature(sub) + ": ...")
                    had_member = True
            if not had_member and not doc:
                lines.append("    ...")
            lines.append("")
        elif isinstance(node, ast.Assign):
            # Keep top-level constants, skipping private ones.
            targets = [t.id for t in node.targets if isinstance(t, ast.Name) and not t.id.startswith("_")]
            if targets:
                lines.append(f"{targets[0]} = ...")

    return "\n".join(lines).rstrip() + "\n"


def _fn_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = _args_src(node.args)
    ret = _expr_src(node.returns) if node.returns else None
    suffix = f" -> {ret}" if ret else ""
    return f"{prefix} {node.name}({args}){suffix}"


def _args_src(args: ast.arguments) -> str:
    parts: list[str] = []
    for a in args.args:
        ann = f": {_expr_src(a.annotation)}" if a.annotation else ""
        parts.append(a.arg + ann)
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    for a in args.kwonlyargs:
        ann = f": {_expr_src(a.annotation)}" if a.annotation else ""
        parts.append(a.arg + ann)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return ", ".join(parts)


def _expr_src(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "..."


def _compress_outlines(ctx: Context, params: dict[str, Any]) -> None:
    blocks: list[str] = []
    for path in ctx.files_in_scope:
        try:
            body = path.read_text(errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(ctx.repo_root)
        if path.suffix == ".py":
            outline = _ast_outline(body)
            if outline:
                blocks.append(f"--- {rel} (outline) ---\n{outline}")
                continue
        # Non-Python or parse failed: include first N lines.
        head = "\n".join(body.splitlines()[:20])
        blocks.append(f"--- {rel} (head) ---\n{head}")
    ctx.compressed_text = "\n\n".join(blocks)


def _compress_summarize(ctx: Context, params: dict[str, Any]) -> None:
    """Use the benchmark's backend (set on ctx via ctx.backend) to
    summarise the selected files as bullets relevant to the task.

    Gracefully degrades to outlines if the backend is not available,
    so tests can exercise the stage without a live server."""
    backend = getattr(ctx, "backend", None)
    if backend is None:
        _compress_outlines(ctx, params)
        return

    bullets: list[str] = []
    max_per_file = int(params.get("max_tokens_per_file", 180))
    for path in ctx.files_in_scope:
        try:
            body = path.read_text(errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(ctx.repo_root)
        prompt = (
            f"Summarise this file as terse bullet points that preserve every "
            f"detail relevant to this task:\n\nTASK: {ctx.task_description}\n\n"
            f"FILE ({rel}):\n{body}\n\nRespond with only the bullets."
        )
        try:
            resp = backend.chat(
                [{"role": "user", "content": prompt}],
                max_tokens=max_per_file,
                temperature=0.2,
            )
            bullets.append(f"--- {rel} (summary) ---\n{resp.text.strip()}")
        except Exception as e:  # noqa: BLE001
            bullets.append(f"--- {rel} (summary-failed: {type(e).__name__}) ---")
    ctx.compressed_text = "\n\n".join(bullets)


@register("prompt_assembly")
def prompt_assembly(ctx: Context, params: dict[str, Any]) -> None:
    """Build the final chat messages list from the selected context.

    `params.output_format` selects how the model is asked to respond:
      - "unified_diff" (default): ```diff block, applied by patcher.
      - "whole_file": one ```python block per changed file, each led
        by a `# FILE: <path>` header line. Sidesteps the hunk-header
        failure mode where local models produce valid diffs with
        wrong @@ counts."""
    output_format = params.get("output_format", "unified_diff")
    ctx.output_format = output_format

    parts: list[str] = []
    if ctx.entry_instruction:
        parts.append(ctx.entry_instruction)
    if ctx.goal:
        parts.append(f"Goal: {ctx.goal}")
    parts.append(f"Task: {ctx.task_description}")

    if ctx.compressed_text:
        parts.append("Relevant context:\n" + ctx.compressed_text)
    elif ctx.files_in_scope:
        parts.append("Relevant files:")
        for path in ctx.files_in_scope:
            try:
                body = path.read_text(errors="ignore")
            except OSError:
                continue
            rel = path.relative_to(ctx.repo_root)
            parts.append(f"\n--- {rel} ---\n{body}")

    if output_format == "whole_file":
        parts.append(
            "For EACH file you need to change, emit one fenced code block "
            "whose FIRST line is `# FILE: <relative path>` (exactly) and "
            "whose remaining lines are the COMPLETE fixed file body — not "
            "a diff, not a snippet. Do not include files you aren't "
            "changing. Prose outside the blocks is ignored."
        )
    else:
        parts.append(
            "Respond with a unified diff inside a ```diff block that, when applied "
            "from the repository root, makes the goal's validation command succeed."
        )

    user_content = "\n\n".join(parts)
    ctx.messages = [
        {
            "role": "system",
            "content": (
                "You are a precise software engineer. Produce minimal, correct "
                "patches grounded in the provided files."
            ),
        },
        {"role": "user", "content": user_content},
    ]
    ctx.assembled_prompt_chars = sum(len(m["content"]) for m in ctx.messages)
