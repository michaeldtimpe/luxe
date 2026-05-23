"""BFCL problem → luxe Backend invocation adapter (raw + agent modes).

PRELIMINARY (2026-05-03). Loads BFCL v4 problems from the vendored data
dir (`~/.luxe/bfcl-data/`, see `_bfcl_data_dir`), runs them against the
luxe backend, returns (actual_tool_calls, timing) per problem. The grader
(`grade.py`) is pure-Python; `bfcl_eval` is NOT a dependency (do not install
it — tree_sitter conflict; see `_bfcl_data_dir`).

Two modes (per `~/.claude/plans/fancy-honking-lerdorf.md`):

- `raw`: single-turn `backend.chat()` with the BFCL function as a tool.
  Captures the model's first emitted tool calls. Comparable to public
  BFCL numbers (fair model-only baseline).
- `agent`: full `run_agent()` loop with the BFCL spec as the only ToolDef
  and a stub executor. Captures all tool calls from the loop. Measures
  whether luxe's prompt scaffolding helps or hurts.

For irrelevance category: tools are still passed but the model must
correctly NOT call them. Both modes apply.
"""

from __future__ import annotations

import importlib.resources
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from luxe.backend import Backend, ChatResponse
from luxe.config import RoleConfig
from luxe.spec import Requirement, Spec
from luxe.tools.base import ToolDef, dispatch_tool

from .schemas import bfcl_func_spec_to_tool_def, make_stub_executor
from .multi_turn.executor import build_tool_surface, to_call_string


# Category-aware system prompt overrides (v1.7 priority #2). The default
# prompt primes tool-eagerness which corrupts BFCL irrelevance, where the
# correct outcome is to decline. The irrelevance variant explicitly names
# the abstain branch without naming the benchmark — the model sees task-
# shape language, not "this is a benchmark for irrelevance".
_SYSTEM_PROMPTS: dict[str, str] = {
    # v1.8 Track 4 — tightened irrelevance variant. The v1.7 phrasing
    # ("If the user's request cannot be answered ... decline and briefly
    # explain why") recovered +4.59pp on its own but left 23/240 cases
    # where the model still emitted a tool call. The pre-dispatch spec
    # gate (Track 2) handles those at the runtime layer; this prompt
    # tightening is defense-in-depth that biases the model toward the
    # correct outcome BEFORE the runtime needs to intervene.
    "irrelevance": (
        "You have tools available, but you must NOT use them unless they "
        "can directly answer the user's request. If they cannot, your "
        "only valid response is to decline in prose, explaining briefly "
        "why the request is out of scope. Do not call any tool to gather "
        "information, verify, or attempt — declining is the correct "
        "action. Do not invent tool calls under any circumstance."
    ),
    # Multi-turn: the model completes each user turn by emitting tool calls that
    # are executed against live stateful instances; it must ACT (not narrate) and
    # use the provided tool definitions (not freeform python / markdown fences),
    # then stop when the current turn's request is satisfied. Generation-quality
    # lever — validated by the pre-Phase-4 transcript sanity check.
    "multi_turn_base": (
        "You are an assistant that completes the user's requests by calling the "
        "provided tools. Each user message may need one or more tool calls. Always "
        "act by emitting tool calls via the provided tool definitions — do NOT write "
        "code in prose or markdown, and do not merely describe what you would do. Use "
        "each tool's returned result to decide your next call. When the current "
        "request is fully satisfied, stop calling tools."
    ),
    "default": "You are an assistant that calls tools to answer questions.",
}


def _system_prompt_for(category: str) -> str:
    return _SYSTEM_PROMPTS.get(category, _SYSTEM_PROMPTS["default"])


def _spec_from_problem(
    problem: dict[str, Any],
    category: str,
    ground_truth: list[Any] | None = None,
) -> Spec | None:
    """Derive a SpecDD Lever 1 Spec from a BFCL problem's structure.

    Three category groups, three shapes:
      - irrelevance: spec demands zero tool calls (expects_zero_calls)
      - parallel / parallel_multiple with GT length >= 2: spec demands at
        least len(GT) tool calls (min_tool_calls)
      - everything else: no spec (single-call problems already grade
        cleanly under the existing loop without Lever 1)

    Fairness note: this leaks the *number* of expected calls (via GT
    length), not the values. RESUME.md v1.7 priority #2 explicitly
    endorses this design ("derive a per-problem Spec from the
    expected-calls structure"). Future raw-vs-agent comparisons must
    label v1.7+ agent runs as "loop + Lever 1 hints" rather than "loop
    alone" to avoid attributing improvements to loop scaffolding alone.
    """
    if category == "irrelevance":
        return Spec(
            goal="The user's request cannot be served by these tools.",
            requirements=[
                Requirement(
                    id="R1",
                    must="Do not call any tool.",
                    done_when="Zero tool calls emitted.",
                    kind="expects_zero_calls",
                )
            ],
        )
    if category in ("parallel", "parallel_multiple"):
        gt = ground_truth or []
        n = len(gt) if isinstance(gt, list) else 1
        if n >= 2:
            return Spec(
                goal=f"User's request implies {n} tool calls.",
                requirements=[
                    Requirement(
                        id="R1",
                        must=f"Make at least {n} tool calls covering the user's request.",
                        done_when=f"len(tool_calls) >= {n}.",
                        kind="min_tool_calls",
                        min_matches=n,
                    )
                ],
            )
    return None


# Categories we run. Subset chosen for Python-relevance and Mode-B parity.
SUPPORTED_CATEGORIES = (
    "simple_python",
    "multiple",
    "parallel",
    "parallel_multiple",
    "irrelevance",
    # `multi_turn_base` is supported via the dedicated stateful driver
    # (`run_problem_multi_turn`) + grader (`grade.grade_multi_turn`); run.py routes
    # it on `category.startswith("multi_turn")`. It is intentionally NOT in this
    # default set (slower + its own clean-baseline semantics) — request it explicitly
    # via `--categories multi_turn_base`.
)


def _bfcl_data_dir() -> Path:
    """Resolve the BFCL v4 data dir (data only — the grader is self-contained).

    Resolution order:
      1. `LUXE_BFCL_DATA_DIR` env var (explicit override).
      2. `~/.luxe/bfcl-data/` (default vendored location populated by
         `scripts/fetch_bfcl_data.sh`).

    This function resolves the problem/answer JSON ONLY. luxe's BFCL grader
    (`benchmarks/bfcl/grade.py`) is pure-Python (function-name + arg-allowed-set
    matching) and never imports `bfcl_eval` — there is no grading-path dependency
    on it.

    WARNING: do NOT `pip install bfcl_eval` to "fix" a missing data dir. `bfcl_eval`
    pins `tree_sitter==0.21.3`, which conflicts with the `tree_sitter_language_pack`
    that `src/luxe/symbols.py` imports (v1.10.1 substrate) and would poison the venv.
    Vendoring the data (`scripts/fetch_bfcl_data.sh`) is the only supported path.
    """
    override = os.environ.get("LUXE_BFCL_DATA_DIR")
    if override:
        return Path(override).expanduser()
    vendored = Path.home() / ".luxe" / "bfcl-data"
    if vendored.is_dir():
        return vendored
    raise FileNotFoundError(
        f"BFCL data not found at {vendored}. "
        "Run scripts/fetch_bfcl_data.sh to populate the default vendored location, "
        "or set LUXE_BFCL_DATA_DIR to an existing BFCL v4 data directory. "
        "Do NOT install bfcl_eval (it pins tree_sitter==0.21.3 and breaks "
        "src/luxe/symbols.py's tree_sitter_language_pack import)."
    )


def _category_filename(category: str) -> str:
    return f"BFCL_v4_{category}.json"


def load_problems(category: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Load problems for a category, optionally capped at `limit`."""
    data_dir = _bfcl_data_dir()
    path = data_dir / _category_filename(category)
    if not path.is_file():
        raise FileNotFoundError(f"BFCL category data not found: {path}")
    out: list[dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
            if limit and len(out) >= limit:
                break
    return out


def load_ground_truth(category: str) -> dict[str, list[Any]]:
    """Load ground-truth for a category as a {problem_id: gt_list} dict.
    Returns empty dict for irrelevance (no gt).
    """
    if category == "irrelevance":
        return {}
    data_dir = _bfcl_data_dir()
    path = data_dir / "possible_answer" / _category_filename(category)
    if not path.is_file():
        return {}
    out: dict[str, list[Any]] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            out[entry["id"]] = entry.get("ground_truth", [])
    return out


def _problem_messages(problem: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert BFCL `question` (list-of-list-of-messages) to a flat
    OpenAI-style message list. Single-turn problems wrap the user
    message in `[[{...}]]`; we take the first turn.
    """
    question = problem.get("question") or []
    if not question:
        return [{"role": "user", "content": ""}]
    # First turn's messages.
    first_turn = question[0]
    if not isinstance(first_turn, list):
        first_turn = [first_turn]
    msgs: list[dict[str, Any]] = []
    for m in first_turn:
        if isinstance(m, dict) and "role" in m and "content" in m:
            msgs.append({"role": m["role"], "content": m["content"]})
    if not msgs:
        msgs = [{"role": "user", "content": str(question)}]
    return msgs


def _problem_tools(problem: dict[str, Any]) -> list[ToolDef]:
    """Extract function specs from a BFCL problem and convert to ToolDefs."""
    funcs = problem.get("function") or []
    if not isinstance(funcs, list):
        funcs = [funcs]
    return [bfcl_func_spec_to_tool_def(f) for f in funcs]


@dataclass
class BfclInvocationResult:
    problem_id: str
    actual_calls: list[tuple[str, dict[str, Any]]]
    wall_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""
    # Multi-turn only: authoritative grader input `list[turn][step][call_str]`,
    # plus the full message transcript for debugging. Empty for single-turn modes.
    decoded_turns: list[list[list[str]]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)


def run_problem_raw(
    backend: Backend,
    problem: dict[str, Any],
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> BfclInvocationResult:
    """Raw mode: single chat call, capture emitted tool calls."""
    pid = problem.get("id", "unknown")
    messages = _problem_messages(problem)
    tools = _problem_tools(problem)
    openai_tools = [t.to_openai() for t in tools] if tools else None

    t0 = time.monotonic()
    try:
        resp: ChatResponse = backend.chat(
            messages=messages,
            tools=openai_tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as e:  # noqa: BLE001 — surface backend errors per problem
        return BfclInvocationResult(
            problem_id=pid,
            actual_calls=[],
            wall_s=time.monotonic() - t0,
            error=f"{type(e).__name__}: {e}",
        )

    calls = [(tc.name, tc.arguments) for tc in resp.tool_calls]
    return BfclInvocationResult(
        problem_id=pid,
        actual_calls=calls,
        wall_s=time.monotonic() - t0,
        prompt_tokens=resp.timing.prompt_tokens,
        completion_tokens=resp.timing.completion_tokens,
    )


def run_problem_agent(
    backend: Backend,
    role_cfg: RoleConfig,
    problem: dict[str, Any],
    *,
    category: str = "default",
    system_prompt: str | None = None,
    ground_truth: list[Any] | None = None,
) -> BfclInvocationResult:
    """Agent mode: full run_agent() loop with the BFCL spec as the only
    ToolDef and stub executor. Captures all tool calls from the loop.

    `category` selects both the category-aware system prompt (abstain
    gradient for irrelevance) and the SpecDD Lever 1 Spec derivation
    (zero-call expectation for irrelevance, min-call count for parallel*).
    Pass an explicit `system_prompt` to override the category default for
    A/B testing.
    """
    from luxe.agents.loop import run_agent

    pid = problem.get("id", "unknown")
    messages_seed = _problem_messages(problem)
    user_text = "\n\n".join(m["content"] for m in messages_seed if m.get("role") == "user")
    tool_defs = _problem_tools(problem)
    tool_fns = {td.name: make_stub_executor({"name": td.name}) for td in tool_defs}

    if system_prompt is None:
        system_prompt = _system_prompt_for(category)
    spec = _spec_from_problem(problem, category, ground_truth)

    t0 = time.monotonic()
    try:
        result = run_agent(
            backend=backend,
            role_cfg=role_cfg,
            system_prompt=system_prompt,
            task_prompt=user_text,
            tool_defs=tool_defs,
            tool_fns=tool_fns,
            spec=spec,
        )
    except Exception as e:  # noqa: BLE001
        return BfclInvocationResult(
            problem_id=pid,
            actual_calls=[],
            wall_s=time.monotonic() - t0,
            error=f"{type(e).__name__}: {e}",
        )

    calls = [(tc.name, tc.arguments) for tc in result.tool_calls
             if not tc.duplicate and not tc.error]
    return BfclInvocationResult(
        problem_id=pid,
        actual_calls=calls,
        wall_s=result.wall_s or (time.monotonic() - t0),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )


# Multi-turn loop guardrails (named per plan). A turn ends when the model emits no
# tool calls (it's done) or hits the per-turn step cap; the per-problem call cap +
# repeated-payload detection bound pathological loops (malformed output, degeneracy).
_MAX_STEPS_PER_TURN = 15
_MAX_CALLS_PER_PROBLEM = 50

# Per-involved-class generation guidance (OPT-IN via LUXE_MT_CLASS_GUIDANCE=1; default
# OFF → byte-identical to the clean baseline). SCOPED: appended only when the named
# class is in a problem's involved_classes, so problems without that class are
# untouched (exact A/B by construction). Tuned to the GorillaFileSystem failure
# diagnosis: path-semantics confusion + uncertainty-collapse on WRITES (the grader does
# not penalize extra reads — state=final-state, response=GT⊆model — so the guidance is
# writes-focused and does NOT mandate pwd/ls probing). Map is extensible per class.
_CLASS_GUIDANCE: dict[str, str] = {
    "GorillaFileSystem": (
        "\n\nFile-system tips: your working directory PERSISTS across calls. Refer to "
        "files and directories by their plain name in the current directory (e.g. "
        "touch(file_name='photo.jpg')); do not prefix names with './', '../', or an "
        "absolute path unless you have already cd'd into a subdirectory. Assume each "
        "operation succeeded unless its result says otherwise — never repeat an "
        "operation a different way or with an alternate path. Do exactly what is asked; "
        "do not create extra files or directories. Track the current directory yourself; "
        "only call pwd/ls if a tool result is unexpected."
    ),
}


def run_problem_multi_turn(
    backend: Backend,
    problem: dict[str, Any],
    *,
    system_prompt: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    num_ctx: int = 32768,
) -> BfclInvocationResult:
    """Clean multi-turn driver on `backend.chat` + `dispatch_tool` (no `run_agent`,
    no luxe interventions — a clean capability baseline).

    For each user turn: append its message(s); loop ≤ `_MAX_STEPS_PER_TURN` calling
    `backend.chat`; execute emitted calls against the problem's LIVE persistent
    involved-class instances (so the model sees real results) and record each step's
    BFCL call-strings into `decoded_turns` (`list[turn][step][call_str]`, the grader
    input). Assistant + tool messages are replayed in the EXACT OpenAI shape luxe's
    own loop uses (`loop.py`), so multi-turn context is preserved verbatim. Grading is
    separate — `grade.grade_multi_turn` re-executes the recorded call-strings on fresh
    vendored instances.
    """
    pid = problem.get("id", "unknown")
    involved = problem.get("involved_classes", [])
    initial_config = problem.get("initial_config", {})
    turns = problem.get("question", []) or []
    if system_prompt is None:
        system_prompt = _system_prompt_for("multi_turn_base")
    # Opt-in scoped per-class guidance (default off → byte-identical to clean baseline).
    if os.environ.get("LUXE_MT_CLASS_GUIDANCE") == "1":
        extra = "".join(_CLASS_GUIDANCE[c] for c in involved if c in _CLASS_GUIDANCE)
        system_prompt = system_prompt + extra

    t0 = time.monotonic()
    try:
        tool_defs, tool_fns, _instances = build_tool_surface(involved, initial_config)
    except Exception as e:  # noqa: BLE001 — surface setup errors per problem
        return BfclInvocationResult(
            problem_id=pid, actual_calls=[], wall_s=time.monotonic() - t0,
            error=f"build_tool_surface: {type(e).__name__}: {e}",
        )
    openai_tools = [td.to_openai() for td in tool_defs] if tool_defs else None

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    decoded_turns: list[list[list[str]]] = []
    flat_calls: list[tuple[str, dict[str, Any]]] = []
    prompt_tokens = 0
    completion_tokens = 0
    total_calls = 0
    seen_payloads: set[str] = set()
    error = ""

    try:
        for turn_idx, turn in enumerate(turns):
            turn_msgs = turn if isinstance(turn, list) else [turn]
            for m in turn_msgs:
                if isinstance(m, dict) and "role" in m and "content" in m:
                    messages.append({"role": m["role"], "content": m["content"]})

            turn_steps: list[list[str]] = []
            for step in range(_MAX_STEPS_PER_TURN):
                resp = backend.chat(
                    messages=messages, tools=openai_tools,
                    max_tokens=max_tokens, temperature=temperature, num_ctx=num_ctx,
                )
                prompt_tokens += resp.timing.prompt_tokens
                completion_tokens += resp.timing.completion_tokens

                if not resp.tool_calls:
                    turn_steps.append([])  # no-op step → turn done (keeps alignment)
                    if resp.text:
                        messages.append({"role": "assistant", "content": resp.text})
                    break

                # Mirror loop.py's assistant-message shape verbatim (id + JSON args).
                messages.append({
                    "role": "assistant", "content": resp.text or "",
                    "tool_calls": [
                        {"id": tc.id or f"call_{turn_idx}_{step}_{i}", "type": "function",
                         "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                        for i, tc in enumerate(resp.tool_calls)
                    ],
                })
                payload_key = json.dumps(
                    [(tc.name, tc.arguments) for tc in resp.tool_calls], sort_keys=True,
                )

                step_calls: list[str] = []
                for i, tc in enumerate(resp.tool_calls):
                    try:
                        executed = dispatch_tool(tc.name, tc.arguments, tool_fns)
                        content = executed.error or executed.result
                    except Exception as e:  # noqa: BLE001 — never panic mid-sequence
                        content = f"{type(e).__name__}: {e}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id or f"call_{turn_idx}_{step}_{i}",
                        "name": tc.name, "content": content,
                    })
                    step_calls.append(to_call_string(tc.name, tc.arguments))
                    flat_calls.append((tc.name, tc.arguments))
                    total_calls += 1

                turn_steps.append(step_calls)

                if payload_key in seen_payloads:
                    break  # repeated identical tool_call payload → cycle, end turn
                seen_payloads.add(payload_key)
                if total_calls >= _MAX_CALLS_PER_PROBLEM:
                    break

            decoded_turns.append(turn_steps)
            if total_calls >= _MAX_CALLS_PER_PROBLEM:
                break
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"

    return BfclInvocationResult(
        problem_id=pid,
        actual_calls=flat_calls,
        wall_s=time.monotonic() - t0,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        error=error,
        decoded_turns=decoded_turns,
        transcript=messages,
    )
