"""Shared agent loop for specialists.

Adapted from personal_eval/agent_loop.py. Simplified:
- No RunMetrics (we log to Session instead)
- Tool surface and system prompt come from AgentConfig
- Tool dispatch delegated to luxe.tools (set via the tools_registry arg)
- Streaming text is rendered by the caller (REPL); we return the final text

This is the base loop every specialist (general/research/writing/image/code)
extends. Specialists mostly just pick their tool set.
"""

from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from harness.backends import Backend, ToolCall, ToolDef

from luxe.registry import AgentConfig
from luxe.session import Session

ToolFn = Callable[[dict[str, Any]], tuple[Any, str | None]]

_TOOL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_JSON_BLOCK_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.DOTALL)


def _preview_args(args: dict[str, Any]) -> str:
    """Compact, human-scannable render of a tool's arguments for live tail.
    Keeps the two most informative keys (path-like + pattern-like first)
    and truncates each value to 40 chars. Never blocks on large payloads."""
    if not args:
        return ""
    priority = ("path", "file", "pattern", "query", "url", "cmd", "command", "name")
    items: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for k in priority:
        if k in args and k not in seen:
            items.append((k, args[k]))
            seen.add(k)
    for k, v in args.items():
        if k in seen:
            continue
        items.append((k, v))
        seen.add(k)
        if len(items) >= 2:
            break
    parts: list[str] = []
    for k, v in items[:2]:
        s = str(v).replace("\n", " ")
        if len(s) > 40:
            s = s[:37] + "…"
        parts.append(f"{k}={s}")
    return " ".join(parts)
# Gemma 3's native function-call format: ```tool_code\n<python call>\n```.
# Some Gemma variants (or bare IT without tool_code priming) fall back to a
# ```python block instead — accept both.
_PYCODE_BLOCK_RE = re.compile(
    r"```(?:tool_code|python)\s*\n(.*?)\n```", re.DOTALL
)


def _parse_python_calls(block: str, known_names: set[str]) -> list[dict[str, Any]]:
    """Parse a Python snippet containing one or more top-level Call expressions
    (e.g. `list_dir(path=".")`) into {name, arguments} dicts. Uses ast so we
    evaluate literal args safely — no exec, no name lookups."""
    out: list[dict[str, Any]] = []
    try:
        tree = ast.parse(block)
    except SyntaxError:
        return out
    for node in tree.body:
        call: ast.Call | None = None
        # Top-level bare expression like `list_dir(path=".")`
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
        # `print(list_dir(...))` — unwrap one layer, common for Gemma.
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "print"
            and len(node.value.args) == 1
            and isinstance(node.value.args[0], ast.Call)
        ):
            call = node.value.args[0]
        if call is None or not isinstance(call.func, ast.Name):
            continue
        if call.func.id not in known_names:
            continue
        args: dict[str, Any] = {}
        for kw in call.keywords:
            if kw.arg is None:
                continue
            try:
                args[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, SyntaxError):
                pass  # drop non-literal kwarg
        out.append({"name": call.func.id, "arguments": args})
    return out


def _safe_json(s: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_bare_json_objects(text: str) -> list[dict[str, Any]]:
    """Walk `text` and pull out every top-level JSON object it contains,
    regardless of whether it's inside a code fence or not. Uses
    JSONDecoder.raw_decode so multi-line objects get captured — the
    line-by-line fallback only matched single-line JSON, which missed
    the pretty-printed calls Qwen emits on longer prompts."""
    decoder = json.JSONDecoder()
    out: list[dict[str, Any]] = []
    i, n = 0, len(text)
    while i < n:
        j = text.find("{", i)
        if j < 0:
            break
        try:
            obj, consumed = decoder.raw_decode(text[j:])
        except json.JSONDecodeError:
            i = j + 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        i = j + consumed
    return out


def _parse_text_tool_calls(text: str, known_names: set[str]) -> list[ToolCall]:
    """Recover tool calls that a model emitted as text content instead of
    structured tool_calls. Supports three patterns:
      1. Qwen/Hermes: <tool_call>{...}</tool_call> or fenced ```json {...} ```
      2. Bare-JSON lines ({"name": ..., "arguments": ...})
      3. Gemma 3: ```tool_code\nfunc(arg=...)\n``` (Python call syntax)
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
        for m in _PYCODE_BLOCK_RE.finditer(text):
            candidates.extend(_parse_python_calls(m.group(1), known_names))
    if not candidates:
        # Bare JSON anywhere in the text (multi-line via raw_decode).
        # Filter by known_names so unrelated JSON in prose isn't
        # misinterpreted as a call.
        for obj in _extract_bare_json_objects(text):
            if isinstance(obj.get("name"), str) and obj["name"] in known_names:
                candidates.append(obj)

    calls: list[ToolCall] = []
    for i, obj in enumerate(candidates):
        name = obj.get("name")
        if name not in known_names:
            continue
        args = obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, str):
            args_obj = _safe_json(args) or {}
            raw = args
        else:
            args_obj = args
            raw = json.dumps(args)
        calls.append(
            ToolCall(id=f"text_{i}", name=name, arguments=args_obj, raw_arguments=raw)
        )
    return calls


@dataclass
class AgentResult:
    final_text: str
    steps_taken: int
    tool_calls_total: int
    aborted: bool = False
    abort_reason: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0
    # Sum of `backend.chat(...)` durations across all agent steps. Always
    # ≤ wall_s. The gap between the two is time the model wasn't running
    # — tool execution, web round-trips, session I/O. Used so tok/s
    # reflects actual decode rate rather than "output tokens divided by
    # total wall, including HTTP waits."
    model_wall_s: float = 0.0
    tool_calls: list[ToolCall] = field(default_factory=list)  # structured across all steps
    # Count of turns whose completion_tokens hit ≥80% of
    # cfg.max_tokens_per_turn — a signal that the per-turn cap is
    # probably truncating real output and the YAML budget should go up.
    near_cap_turns: int = 0


def run_agent(
    backend: Backend,
    cfg: AgentConfig,
    *,
    task: str,
    tool_defs: list[ToolDef],
    tool_fns: dict[str, ToolFn],
    session: Session | None = None,
    history: list[dict[str, Any]] | None = None,
    tool_style: str = "openai",
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    """Run a specialist agent until it stops calling tools or hits a budget.

    Args:
        backend: OpenAI-compat Backend pointed at Ollama
        cfg: agent config (model, prompt, budgets)
        task: user's task description (already cleaned up by router)
        tool_defs: ToolDef specs the model sees
        tool_fns: name -> callable mapping; each returns (result, error or None)
        session: optional Session to log every turn into
        history: optional prior messages to prepend (for session resume)
        tool_style: "openai" for structured `tool_calls` + `role=tool` messages
            (Qwen/Mistral/Llama via Ollama). "gemma_pycode" for Gemma 3 via
            llama-server: emit the assistant's `tool_code` block as plain
            content and wrap results as a user message with a `tool_output`
            block, keeping the strict user/assistant alternation Gemma's
            jinja template requires.
    """

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": cfg.system_prompt}
    ]
    if history is not None:
        messages.extend(history)
    elif session is not None:
        keep = getattr(cfg, "history_keep_last", 4)
        if tool_style == "gemma_pycode":
            # Replay tool_code + tool_output pairs too, so Gemma sees real
            # file data across turns and doesn't have to re-read or invent.
            messages.extend(
                _build_gemma_history_from_session(
                    session, cfg.name, keep_last_messages=max(keep * 3, 12)
                )
            )
        else:
            messages.extend(
                _build_history_from_session(session, cfg.name, keep_last=keep)
            )
    messages.append({"role": "user", "content": task})

    if session:
        session.append({"role": "user", "agent": cfg.name, "content": task})

    started = time.monotonic()
    tool_calls_total = 0
    tool_calls_accum: list[ToolCall] = []
    step = 0
    final_text = ""
    prompt_tokens = 0
    completion_tokens = 0
    model_wall_s = 0.0  # sum of backend.chat() durations; excludes tool exec
    near_cap_turns = 0
    # Extras for Ollama pass-throughs. `num_ctx` lets agents override the
    # loaded server context per-agent without touching modelfiles — most
    # useful for coder models where large contexts trade throughput for
    # history depth.
    extra_body: dict[str, Any] | None = None
    if cfg.num_ctx:
        extra_body = {"options": {"num_ctx": cfg.num_ctx}}

    def _result(aborted: bool = False, reason: str = "") -> AgentResult:
        return AgentResult(
            final_text=final_text,
            steps_taken=step,
            tool_calls_total=tool_calls_total,
            aborted=aborted,
            abort_reason=reason,
            transcript=messages,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            wall_s=time.monotonic() - started,
            model_wall_s=model_wall_s,
            tool_calls=list(tool_calls_accum),
            near_cap_turns=near_cap_turns,
        )

    while step < cfg.max_steps:
        if time.monotonic() - started > cfg.max_wall_s:
            return _result(True, f"wall budget {cfg.max_wall_s}s exceeded")

        try:
            response = backend.chat(
                messages,
                # Gemma's jinja template ignores `tools=` anyway; sending it
                # is harmless on llama-server but skip for clarity.
                tools=None if tool_style == "gemma_pycode" else (tool_defs or None),
                max_tokens=cfg.max_tokens_per_turn,
                temperature=cfg.temperature,
                stream=False,  # REPL does its own streaming layer in a later phase
                extra_body=extra_body,
            )
        except KeyboardInterrupt:
            return _result(True, "interrupted (Ctrl-C)")
        prompt_tokens += response.timing.prompt_tokens
        completion_tokens += response.timing.completion_tokens
        model_wall_s += response.timing.total_s
        if (
            cfg.max_tokens_per_turn
            and response.timing.completion_tokens
            >= 0.8 * cfg.max_tokens_per_turn
        ):
            near_cap_turns += 1

        # Ollama emits most qwen/hermes tool calls in `tool_calls` already,
        # but qwen2.5-coder sometimes falls back to text JSON and Gemma 3
        # always emits ```tool_code``` blocks. Recover only the first call —
        # models that dump a whole speculative plan would otherwise blow
        # past the per-turn cap in a single step.
        if not response.tool_calls and response.text and tool_defs:
            known = {t.name for t in tool_defs}
            recovered = _parse_text_tool_calls(response.text, known)
            if recovered:
                response.tool_calls = recovered[:1]
                # For gemma_pycode we need the original text to round-trip
                # into the assistant turn; for openai-style json recovery we
                # drop the noise so the transcript stays clean.
                if tool_style != "gemma_pycode":
                    response.text = ""

        final_text = response.text or final_text

        # Cap check runs BEFORE accumulation so an abort keeps the
        # transcript/tool-call list clean (no half-committed bogus calls).
        if len(response.tool_calls) > cfg.max_tool_calls_per_turn:
            return _result(
                True,
                f"runaway turn: {len(response.tool_calls)} tool calls "
                f"> cap {cfg.max_tool_calls_per_turn}",
            )

        tool_calls_total += len(response.tool_calls)
        tool_calls_accum.extend(response.tool_calls)

        if not response.tool_calls:
            # Model thinks it's done. Enforce min_tool_calls by nudging it
            # back into tool use if it hasn't investigated enough yet.
            if tool_calls_total < cfg.min_tool_calls:
                messages.append({"role": "assistant", "content": response.text or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"You must use tools to ground your answer. So far "
                            f"you've made {tool_calls_total} tool call(s); this "
                            f"task requires at least {cfg.min_tool_calls}. "
                            f"Continue investigating with tools before finalizing."
                        ),
                    }
                )
                step += 1
                continue
            # Final answer
            final_text = response.text
            step += 1
            if response.text:
                messages.append({"role": "assistant", "content": response.text})
                if session:
                    session.append(
                        {"role": "assistant", "agent": cfg.name, "content": response.text}
                    )
            return _result()

        # Model wants tools. Append an assistant turn and the result(s).
        if tool_style == "gemma_pycode":
            # Assistant message is plain text — the `tool_code` block is
            # already embedded in response.text (our parser stripped nothing
            # from the raw content before returning). Fall back to
            # reconstructing a block if the upstream loop cleared it.
            assistant_text = response.text or "\n".join(
                f"```tool_code\n{c.name}({_kwargs_to_py(c.arguments)})\n```"
                for c in response.tool_calls
            )
            messages.append({"role": "assistant", "content": assistant_text})
        else:
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

        tool_results: list[str] = []
        for call in response.tool_calls:
            fn = tool_fns.get(call.name)
            if on_tool_event:
                try:
                    on_tool_event({
                        "event": "tool_call_begin",
                        "name": call.name,
                        "args_preview": _preview_args(call.arguments),
                    })
                except Exception:  # noqa: BLE001
                    pass
            tool_start = time.monotonic()
            if not fn:
                result: Any = None
                err: str | None = f"unknown tool: {call.name}"
            else:
                try:
                    result, err = fn(call.arguments)
                except Exception as e:  # noqa: BLE001
                    result, err = None, f"{type(e).__name__}: {e}"

            tool_content = _trim(result if err is None else f"ERROR: {err}")
            # Stamp telemetry on the ToolCall so downstream (Subtask,
            # state.json, /tasks analyze) can break down time by tool.
            call.wall_s = time.monotonic() - tool_start
            call.ok = err is None
            call.bytes_out = len(tool_content)
            if on_tool_event:
                try:
                    on_tool_event({
                        "event": "tool_call_end",
                        "name": call.name,
                        "ok": call.ok,
                        "wall_s": round(call.wall_s, 2),
                        "bytes_out": call.bytes_out,
                        "error": None if call.ok else (err or ""),
                    })
                except Exception:  # noqa: BLE001
                    pass
            if tool_style == "gemma_pycode":
                tool_results.append(
                    f"```tool_output\n# {call.name}\n{tool_content}\n```"
                )
            else:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": tool_content,
                    }
                )
            if session:
                session.append(
                    {
                        "role": "tool",
                        "agent": cfg.name,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "result": tool_content,
                        "error": err,
                    }
                )

        if tool_style == "gemma_pycode" and tool_results:
            # Gemma 3 requires strict user/assistant alternation — tool
            # outputs ride inside a single user message.
            messages.append({"role": "user", "content": "\n\n".join(tool_results)})

        step += 1

    return _result(True, f"step budget {cfg.max_steps} exhausted")


def _trim(value: Any, limit: int = 4000) -> str:
    s = value if isinstance(value, str) else str(value)
    return s if len(s) <= limit else s[:limit] + f"\n... [truncated {len(s) - limit} bytes]"


def _build_history_from_session(
    session: Session, agent_name: str, *, keep_last: int = 4
) -> list[dict[str, Any]]:
    """Reconstruct a conversational history from prior session events.

    Only user + final-assistant messages for this agent are replayed —
    tool-calling rounds are collapsed into the assistant's subsequent
    summary, which is what the session logs anyway. `keep_last` limits
    how many messages we replay so context stays bounded.
    """
    messages: list[dict[str, Any]] = []
    for ev in session.read_all():
        if ev.get("agent") != agent_name:
            continue
        role = ev.get("role")
        if role not in ("user", "assistant"):
            continue
        content = (ev.get("content") or "").strip()
        if not content:
            continue
        if messages and messages[-1]["role"] == role and messages[-1]["content"] == content:
            continue  # drop exact-duplicate adjacents
        messages.append({"role": role, "content": content})
    # Enforce user/assistant alternation starting from user, the format
    # both OpenAI tool flow and Gemma's jinja template expect.
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        if not cleaned and msg["role"] != "user":
            continue  # must start with user
        if cleaned and cleaned[-1]["role"] == msg["role"]:
            cleaned[-1] = msg  # collapse consecutive same-role into latest
            continue
        cleaned.append(msg)
    return cleaned[-keep_last:]


def _build_gemma_history_from_session(
    session: Session, agent_name: str, *, keep_last_messages: int = 20
) -> list[dict[str, Any]]:
    """Reconstruct a Gemma-style history including synthesized tool_code +
    tool_output pairs so the model sees real file data across turns, not
    just its own prose. This anchors multi-turn work on grounded data and
    prevents the "I already read that file" hallucination pattern.
    """
    def _append(msgs: list[dict[str, Any]], role: str, content: str) -> None:
        if not content.strip():
            return
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] = msgs[-1]["content"].rstrip() + "\n\n" + content
        else:
            msgs.append({"role": role, "content": content})

    messages: list[dict[str, Any]] = []
    for ev in session.read_all():
        if ev.get("agent") != agent_name:
            continue
        role = ev.get("role")
        if role == "user":
            _append(messages, "user", (ev.get("content") or "").strip())
        elif role == "tool":
            name = ev.get("tool") or "?"
            args = ev.get("arguments") or {}
            err = ev.get("error")
            result = ev.get("result") or (f"ERROR: {err}" if err else "")
            _append(messages, "assistant", f"```tool_code\n{name}({_kwargs_to_py(args)})\n```")
            _append(messages, "user", f"```tool_output\n# {name}\n{result}\n```")
        elif role == "assistant":
            _append(messages, "assistant", (ev.get("content") or "").strip())

    # Must start with a user message; drop leading assistants.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    return messages[-keep_last_messages:]


def _kwargs_to_py(args: dict[str, Any]) -> str:
    """Render {"path": "."} → path="." for rebuilding a tool_code block."""
    return ", ".join(f"{k}={v!r}" for k, v in args.items())
