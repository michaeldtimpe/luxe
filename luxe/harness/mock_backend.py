"""Deterministic mock backend for smoke testing without a real model.

Handy for verifying benchmark plumbing, JSONL logging, and report generation
before any model downloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harness.backends import Backend, GenerationTiming, Response


@dataclass
class MockBackend(Backend):
    canned_text: str = "```python\nreturn 0\n```"
    tokens_per_turn: int = 48
    latency_s: float = 0.1

    def __init__(self, canned_text: str = "```python\nreturn 0\n```") -> None:
        super().__init__(kind="mlx", base_url="mock://local", model_id="mock")
        self.canned_text = canned_text

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        stream: bool = True,
    ) -> Response:
        return Response(
            text=self.canned_text,
            tool_calls=[],
            finish_reason="stop",
            timing=GenerationTiming(
                prompt_tokens=sum(len(m.get("content", "")) // 4 for m in messages),
                completion_tokens=self.tokens_per_turn,
                time_to_first_token_s=self.latency_s,
                total_s=self.latency_s + self.tokens_per_turn / 200,
            ),
            raw={"mock": True},
        )
