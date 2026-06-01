"""Interactive `luxe chat` REPL front-end.

Additive Claude-CLI-style terminal agent. Reuses `agents.single.run_single`,
the tool surface, and the prompt registry unchanged; conversation state lives
here (see chat.sdd). The deterministic benchmark path never imports this
package.
"""

from __future__ import annotations

__all__ = ["fold_history", "SUMMARIZER_VERSION", "run_chat_repl"]

from luxe.chat.summarize import SUMMARIZER_VERSION, fold_history


def run_chat_repl(*args, **kwargs):
    # Lazy import: the REPL pulls in Rich/agents; keep `import luxe.chat` cheap.
    from luxe.chat.repl import run_chat_repl as _impl

    return _impl(*args, **kwargs)
