"""oMLX BackendProvider — thin OpenAICompatProvider subclass.

oMLX (the MLX-native server we run at :8000) is OpenAI-compatible. Its
/v1/models endpoint returns minimal metadata ({id, object, created}),
so context_length / parameter_size return None — the REPL banner
falls back to whatever AgentConfig declares.
"""

from __future__ import annotations

from luxe_cli.providers.openai_compat import OpenAICompatProvider


class OMLXProvider(OpenAICompatProvider):
    name = "omlx"
    auth_env_vars = ("OMLX_API_KEY",)

    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        super().__init__(base_url)
