"""Test doubles shared across test modules.

Only doubles that were byte-for-byte identical in several files live here;
a file whose fake differs in behaviour keeps its own copy on purpose.
"""

from __future__ import annotations

from typing import Any

from luxe.backend import ChatResponse, GenerationTiming


class ScriptedBackend:
    """Backend stub that yields a pre-scripted sequence of ChatResponses,
    capturing the messages list passed in on each call so assertions can
    inspect the conversation post-hoc. Once the script runs out it answers
    an empty `stop` turn, which ends the loop."""

    def __init__(self, scripted: list[ChatResponse]) -> None:
        self._scripted = list(scripted)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages, **kwargs) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        if not self._scripted:
            return ChatResponse(
                text="", finish_reason="stop",
                timing=GenerationTiming(prompt_tokens=10, completion_tokens=10))
        return self._scripted.pop(0)
