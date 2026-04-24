"""Unified backend abstraction for local OpenAI-compatible model servers.

Both `mlx_lm.server` and `llama.cpp`'s `llama-server` expose an
OpenAI-compatible /v1/chat/completions endpoint. We talk to either through
the same `Backend` interface so benchmark runners and the agent loop stay
backend-agnostic.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

BackendKind = Literal["mlx", "llamacpp", "ollama"]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class GenerationTiming:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    time_to_first_token_s: float = 0.0
    total_s: float = 0.0

    @property
    def decode_tok_per_s(self) -> float:
        decode_time = max(self.total_s - self.time_to_first_token_s, 1e-6)
        return self.completion_tokens / decode_time


@dataclass
class Response:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    timing: GenerationTiming = field(default_factory=GenerationTiming)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Backend:
    kind: BackendKind
    base_url: str
    model_id: str
    # Read timeout for a single chat call. 32B at 16k num_ctx can
    # take ~15+ min end-to-end for one response (prefill + full
    # decode under ~8 tok/s). 20 min keeps us above the agent-level
    # per-subtask wall (15 min) so the wall check fires first
    # instead of a httpx ReadTimeout bubbling up.
    timeout_s: float = 1200.0

    def client(self) -> httpx.Client:
        # Per-axis timeouts: short connect/write so a dead backend
        # fails fast, long read to accommodate slow 32B decode.
        timeout = httpx.Timeout(
            connect=15.0,
            read=self.timeout_s,
            write=60.0,
            pool=30.0,
        )
        return httpx.Client(base_url=self.base_url, timeout=timeout)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        reraise=True,
    )
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolDef] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        stream: bool = True,
        extra_body: dict[str, Any] | None = None,
    ) -> Response:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"
        # Merge caller-supplied extras (e.g. `{"options": {"num_ctx": N}}`
        # for Ollama). Top-level fields like model/messages are reserved
        # — callers should only pass server-specific pass-throughs.
        if extra_body:
            payload.update(extra_body)

        if stream:
            # Ask the server to include a `usage` block in its final stream
            # event. mlx-lm honors this in recent versions; llama-server has
            # done so since early 2024.
            payload["stream_options"] = {"include_usage": True}
            return self._chat_stream(payload)
        return self._chat_nonstream(payload)

    def _chat_nonstream(self, payload: dict[str, Any]) -> Response:
        t0 = time.perf_counter()
        with self.client() as c:
            r = c.post("/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
        elapsed = time.perf_counter() - t0

        choice = data["choices"][0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        tool_calls = _parse_tool_calls(message.get("tool_calls") or [])
        usage = data.get("usage") or {}

        return Response(
            text=text,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            timing=GenerationTiming(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                time_to_first_token_s=0.0,
                total_s=elapsed,
            ),
            raw=data,
        )

    def _chat_stream(self, payload: dict[str, Any]) -> Response:
        t_start = time.perf_counter()
        ttft: float | None = None
        text_parts: list[str] = []
        tool_call_accum: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, Any] = {}

        with self.client() as c:
            with c.stream("POST", "/v1/chat/completions", json=payload) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    usage = event.get("usage") or usage
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}

                    if "content" in delta and delta["content"]:
                        if ttft is None:
                            ttft = time.perf_counter() - t_start
                        text_parts.append(delta["content"])

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_call_accum.setdefault(
                            idx,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

        elapsed = time.perf_counter() - t_start
        tool_calls = _parse_tool_calls(list(tool_call_accum.values()))
        text = "".join(text_parts)

        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        if completion_tokens == 0 and text:
            # Server didn't report usage — estimate. ~3.8 chars/token is a
            # reasonable average across Qwen/DeepSeek/Mistral tokenizers on
            # code. Directional, not precise.
            completion_tokens = max(1, len(text) // 4)

        return Response(
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            timing=GenerationTiming(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                time_to_first_token_s=ttft or 0.0,
                total_s=elapsed,
            ),
            raw={"usage": usage, "finish_reason": finish_reason},
        )


def _parse_tool_calls(items: list[dict[str, Any]]) -> list[ToolCall]:
    parsed: list[ToolCall] = []
    for i, tc in enumerate(items):
        fn = tc.get("function") or tc
        raw_args = fn.get("arguments") or "{}"
        if isinstance(raw_args, dict):
            args = raw_args
            raw_text = json.dumps(raw_args)
        else:
            raw_text = raw_args
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {"__parse_error__": True, "__raw__": raw_args}
        parsed.append(
            ToolCall(
                id=tc.get("id") or f"call_{i}",
                name=fn.get("name") or "",
                arguments=args,
                raw_arguments=raw_text,
            )
        )
    return parsed
