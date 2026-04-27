"""LM Studio BackendProvider — thin OpenAICompatProvider subclass.

LM Studio exposes the OpenAI-standard /v1/models with extra metadata
fields (loaded_context_length, max_context_length) on each model.
Auth is OFF by default in the local server but the env vars
LM_API_TOKEN / LMSTUDIO_API_KEY are honored when set.
"""

from __future__ import annotations

from typing import Iterator

from luxe_cli.providers.openai_compat import OpenAICompatProvider


class LMStudioProvider(OpenAICompatProvider):
    name = "lmstudio"
    auth_env_vars = ("LM_API_TOKEN", "LMSTUDIO_API_KEY")

    def __init__(self, base_url: str = "http://127.0.0.1:1234") -> None:
        super().__init__(base_url)

    def pull_stream(self, model: str) -> Iterator[dict]:
        raise NotImplementedError(
            "LM Studio downloads are managed in the GUI; no /pull endpoint."
        )
