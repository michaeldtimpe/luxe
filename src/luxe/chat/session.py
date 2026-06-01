"""Chat conversation state + context assembly.

`ChatSession` accumulates `(user, assistant)` turns and builds, for each new
turn, the tagged `extra_context` block passed to `run_single`. The assembly
encodes the documented precedence (chat.sdd):

    current user turn  >  project memory  >  conversation summary

Structurally: `Goal:` (in run_single) carries the current message at the TOP;
`extra_context` then carries `<project_memory>`, `<conversation_history>`, and
a trailing `<current_request>` echo so the model's LAST-seen text is the ask,
not a fact dump.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from luxe.chat.summarize import SUMMARIZER_VERSION, fold_history
from luxe.memory import project as project_mem


@dataclass
class ChatTurn:
    user: str
    assistant: str = ""
    slot: str = "chat"
    model: str = ""
    run_id: str = ""


@dataclass
class ChatSession:
    repo_path: str = ""
    session_id: str = ""
    project_hash: str = ""
    languages: frozenset = field(default_factory=frozenset)
    write_enabled: bool = False
    pinned_slot: str | None = None  # set by /use; consumed on the next turn
    turns: list[ChatTurn] = field(default_factory=list)

    # -- history --------------------------------------------------------------

    def history_pairs(self) -> list[tuple[str, str]]:
        return [(t.user, t.assistant) for t in self.turns]

    def fold(self, *, budget_chars: int = 4000) -> tuple[str, str]:
        """Return (folded_history_text, summarizer_version) for the prior turns."""
        return fold_history(self.history_pairs(), budget_chars=budget_chars), SUMMARIZER_VERSION

    # -- context assembly -----------------------------------------------------

    def build_extra_context(self, current_user_message: str, *, budget_chars: int = 4000) -> tuple[str, str]:
        """Assemble the tagged `extra_context` block + record the fold version.

        Returns (extra_context, fold_version). `extra_context` is "" only on a
        clean first turn with no project memory — keeping that path's prompt as
        close to legacy as possible (the current message is already the Goal).
        """
        memory_block = ""
        if self.repo_path:
            memory_block = project_mem.render_block(project_mem.load_memory(self.repo_path))

        history_text, fold_version = self.fold(budget_chars=budget_chars)

        parts: list[str] = []
        if memory_block:
            parts.append(memory_block)
        if history_text:
            parts.append(f"<conversation_history>\n{history_text}\n</conversation_history>")
        if not parts:
            # First turn, no memory: nothing to disambiguate; the Goal carries it.
            return "", fold_version
        # Something precedes the request — echo it last for recency.
        parts.append(f"<current_request>\n{current_user_message.strip()}\n</current_request>")
        return "\n\n" + "\n\n".join(parts), fold_version

    def add_turn(self, turn: ChatTurn) -> None:
        self.turns.append(turn)
