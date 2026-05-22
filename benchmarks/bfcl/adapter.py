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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from luxe.backend import Backend, ChatResponse
from luxe.config import RoleConfig
from luxe.spec import Requirement, Spec
from luxe.tools.base import ToolDef

from .schemas import bfcl_func_spec_to_tool_def, make_stub_executor


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
    # multi_turn deferred — needs state-tracking grader.
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
