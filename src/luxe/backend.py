"""oMLX backend — OpenAI-compatible chat completions client."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class GenerationTiming:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_s: float = 0.0
    time_to_first_token_s: float = 0.0

    @property
    def decode_tok_per_s(self) -> float:
        if self.total_s <= 0 or self.completion_tokens <= 0:
            return 0.0
        return self.completion_tokens / self.total_s


@dataclass
class ToolCallResponse:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    text: str = ""
    tool_calls: list[ToolCallResponse] = field(default_factory=list)
    finish_reason: str = ""
    timing: GenerationTiming = field(default_factory=GenerationTiming)


class Backend:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        model: str = "",
        timeout_s: float = 600.0,
        api_key: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=30.0),
            headers=headers,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        num_ctx: int | None = None,
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if num_ctx is not None:
            body.setdefault("extra_body", {})["num_ctx"] = num_ctx

        t0 = time.monotonic()
        resp = self._client.post("/v1/chat/completions", json=body)
        wall = time.monotonic() - t0
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage", {})

        timing = GenerationTiming(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_s=wall,
        )

        tc_list: list[ToolCallResponse] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            tc_list.append(ToolCallResponse(
                id=tc.get("id", ""),
                name=fn["name"],
                arguments=args,
            ))

        return ChatResponse(
            text=msg.get("content") or "",
            tool_calls=tc_list,
            finish_reason=choice.get("finish_reason", ""),
            timing=timing,
        )

    def health(self) -> bool:
        try:
            r = self._client.get("/v1/models")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def list_models(self) -> list[str]:
        r = self._client.get("/v1/models")
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]
