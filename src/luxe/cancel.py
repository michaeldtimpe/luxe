"""Cooperative cancellation for a long agent turn.

Neutral tier: `gitkit` cancels its own runs and used to reach into
`chat.render` from inside a function body to do it (the import was written
function-local purely to dodge the cycle). `chat.render` re-exports both names,
so every existing `from luxe.chat.render import ...` keeps working.

`ChatCancelled` derives from `KeyboardInterrupt`, i.e. `BaseException` — the
agent loop's `except Exception` guards must NOT swallow a cancellation.
"""

from __future__ import annotations

from dataclasses import dataclass


class ChatCancelled(KeyboardInterrupt):
    """Raised at a tool boundary when the user requested cancellation."""


@dataclass
class CancelToken:
    requested: bool = False

    def reset(self) -> None:
        self.requested = False


def raise_if_cancelled(cancel: CancelToken) -> None:
    """Raise ChatCancelled if a Ctrl-C has set the token. Shared by the tool
    boundary and the streaming token callback (B1) so cancellation lands
    mid-generation, not only between tool calls."""
    if cancel.requested:
        raise ChatCancelled()
