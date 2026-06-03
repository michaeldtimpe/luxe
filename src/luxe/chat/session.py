"""Chat conversation state + context assembly.

`ChatSession` accumulates `(user, assistant)` turns and builds, for each new
turn, the tagged `extra_context` block passed to `run_single`. The assembly
encodes the documented precedence (chat.sdd):

    current user turn  >  system_constraints  >  project memory  >  conversation summary

Structurally: `Goal:` (in run_single) carries the current message at the TOP;
`extra_context` then carries `<system_constraints>` (if any), `<project_memory>`,
`<conversation_history>`, and a trailing `<current_request>` echo so the model's
LAST-seen text is the ask, not a fact dump.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from luxe.agents.prompts import READ_ONLY_CHAT_HINT, TERSE_HINT
from luxe.chat.summarize import SUMMARIZER_VERSION, fold_history
from luxe.memory import project as project_mem

# Context-window size tiers for the `/ctx` flag (chat-only). The actual window
# applied each turn is clamped to the role's `num_ctx_max` (config.py) so a tier
# request can never exceed what the box/model can hold. medium = the shipped
# default (configs/chat.yaml num_ctx). xlarge = the BFCL-proven 128K window.
# huge (256K, C4) is the new ceiling — reachable where the config raises
# num_ctx_max to ≥262144; load-test before relying on it (RAM/KV + gen latency
# scale with the window, and iter-4 128K runs sat at only 5–20% pressure).
CTX_TIERS: dict[str, int] = {
    "small": 8192,
    "medium": 32768,
    "large": 65536,
    "xlarge": 131072,
    "huge": 262144,
}

# Suggest bumping the window up once a turn's peak context pressure crosses this.
CTX_SUGGEST_PRESSURE = 0.85


def tier_label(num_ctx: int) -> str:
    """Name for an exact tier value, else `custom(<n>)`."""
    for name, n in CTX_TIERS.items():
        if n == num_ctx:
            return name
    return f"custom({num_ctx})"


def next_tier_up(num_ctx: int, ceiling: int) -> tuple[str, int] | None:
    """Smallest tier strictly larger than `num_ctx` and within `ceiling`,
    as (name, value), or None when already at/above the headroom."""
    for name, n in CTX_TIERS.items():
        if n > num_ctx and n <= ceiling:
            return name, n
    return None


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
    index_head: str = ""  # repo HEAD when BM25/symbol indices were built (staleness check)
    languages: frozenset = field(default_factory=frozenset)
    write_enabled: bool = False
    unrestricted_bash: bool = False  # set by /bash; only effective in write mode
    pinned_slot: str | None = None  # set by /use; consumed on the next turn
    num_ctx_override: int | None = None  # set by /ctx; clamped per-turn to num_ctx_max
    turns: list[ChatTurn] = field(default_factory=list)
    system_constraints: list[str] = field(default_factory=list)  # set by /sys; injected every turn

    # -- observability (B2): tool-IO depth + reasoning stream are independent --
    verbose_level: str = "off"   # off | diff | full — set by /verbose
    show_reasoning: bool = False  # set by /reasoning; streams model prose live
    terse: bool = True           # set by /terse; injects TERSE_HINT to cut prose

    # -- /plan mode (B5) ------------------------------------------------------
    plan_pending: str | None = None  # objective awaiting a planning turn
    plan_text: str = ""              # last drafted plan (run provenance)

    # -- goal auto-runner (B4) ------------------------------------------------
    goal: str = ""               # objective for the autonomous runner
    goal_active: bool = False    # supervisor loop drives turns while True
    goal_round: int = 0          # rounds issued so far this goal
    goal_max_rounds: int = 20    # hard budget
    consecutive_crashes: int = 0  # reset to 0 on any clean round

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
        clean first turn with no project memory AND write mode on — keeping that
        path's prompt as close to legacy as possible (the current message is
        already the Goal). In read-only mode a low-precedence `<session_mode>`
        hint is always present so the model points the user at /write rather than
        claiming luxe can't create or edit files.
        """
        memory_block = ""
        if self.repo_path:
            memory_block = project_mem.render_block(project_mem.load_memory(self.repo_path))

        history_text, fold_version = self.fold(budget_chars=budget_chars)

        # B5 working-state fold: a compact record of decided/done/remaining so
        # `continue work` / `/goal` rounds consult known state instead of
        # re-reading plan.md + every source each turn. Empty on a fresh session.
        ledger_block = ""
        if self.session_id:
            from luxe.state import ledger as ledger_mod
            ledger_block = ledger_mod.render(ledger_mod.load(self.session_id))

        parts: list[str] = []
        # Lowest precedence: session-mode framing comes first so user/memory text
        # always reads as higher-priority. String lives in the prompt registry.
        if not self.write_enabled:
            parts.append(f"<session_mode>\n{READ_ONLY_CHAT_HINT}\n</session_mode>")
        if memory_block:
            parts.append(memory_block)
        if history_text:
            parts.append(f"<conversation_history>\n{history_text}\n</conversation_history>")
        # Working state sits just below the user's explicit constraints but above
        # memory/history precedence-wise — it's high-signal, low-token recall.
        if ledger_block:
            parts.insert(0, ledger_block)
        # Plan provenance (B5): while a /plan-seeded goal executes, the drafted
        # plan rides along so the agent keeps following what it committed to.
        if self.plan_text and self.goal_active:
            parts.insert(0, f"<plan>\n{self.plan_text.strip()}\n</plan>")
        # Terse output style (B2) — default on; cuts wordy prose. Behavioral, so
        # it rides above memory/history but below the user's explicit constraints.
        if self.terse:
            parts.insert(0, f"<response_style>\n{TERSE_HINT}\n</response_style>")
        # System constraints sit above project memory and history — the user's
        # explicit rules should override anything the model infers from context.
        if self.system_constraints:
            numbered = "\n".join(f"{i+1}. {c}" for i, c in enumerate(self.system_constraints))
            parts.insert(0, f"<system_constraints>\nYou MUST follow these constraints for every turn in this session:\n{numbered}\n</system_constraints>")
        if not parts:
            # First turn, no memory, write mode on: nothing to disambiguate.
            return "", fold_version
        # Something precedes the request — echo it last for recency.
        parts.append(f"<current_request>\n{current_user_message.strip()}\n</current_request>")
        return "\n\n" + "\n\n".join(parts), fold_version

    def add_turn(self, turn: ChatTurn) -> None:
        self.turns.append(turn)
