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

import re
from dataclasses import dataclass, field

from luxe.agents.prompts import (
    GIT_WORKFLOW_HINT,
    NO_PROJECT_CHAT_HINT,
    NO_TOOLS_MODEL_HINT,
    PLANEPROXY_HINT,
    READ_ONLY_CHAT_HINT,
    TERSE_HINT,
)
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

#: Default effective window on a BILLABLE endpoint (2026-08-17, user decision).
#: Not a capability limit — a cost bound. A hosted model may offer a 1M window,
#: and the window is what decides how much conversation + tool output
#: compaction lets a session carry forward; carried prompt tokens are exactly
#: what a metered provider bills, on every step of every turn. So a cloud
#: session starts at 128K (roomy — 4x the local default) and goes higher only
#: when the user asks with `/ctx`. Local endpoints keep the role's `num_ctx`.
BILLABLE_DEFAULT_NUM_CTX = 131072

#: Absolute `/ctx` arguments: `1m`, `500k`, `32768` (case-insensitive).
_CTX_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([km]?)$")


def parse_ctx_size(text: str) -> int | None:
    """A `/ctx` argument as a token count, or None when it isn't one.

    Named tiers are resolved by the caller against `CTX_TIERS`; this covers
    the absolute form, which exists because the tier ladder tops out at 256K
    and a hosted model can offer 1M. `k`/`m` are the decimal multipliers the
    provider catalogs quote in, NOT the binary ones `_ctx_size()` renders with
    — `/ctx 1m` asks for 1,000,000 and the ceiling clamp then reports what it
    actually got. Zero and negatives are not sizes.
    """
    m = _CTX_SIZE_RE.match((text or "").strip().lower())
    if not m:
        return None
    value = float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    n = int(value)
    return n if n > 0 else None

#: Tiers that need more RAM than a typical dev box has, mapped to the floor
#: they want. THIS IS A STATEMENT ABOUT THE MACHINE HOLDING THE KV CACHE, and
#: it is meaningful only when that machine is this one. Against a hosted
#: endpoint the arithmetic below describes hardware that is not in the request
#: path at all — a provider serving a 1M-token model would be reported as
#: needing a box the user does not own to reach a window it is already
#: serving. Every caller must therefore gate it on the weights being local:
#: `cmd_toggles._ctx_on_local_ram` does that for `/ctx`, and
#: `ctx_suggestion(..., local_weights=...)` below does it for the automatic
#: suggestion (2026-08-24; noted in
#: `acceptance/chat_bigread_2026_08_24/PLAN.md` 1.4 — harmless on a 128 GB box,
#: wrong reasoning to leave unstated).
#:
#: `huge` (256K) is the model's NATIVE limit, not this machine's:
#: the Qwen3.6 KV cache runs ~80 KiB/token (40 layers x 2 KV heads x 256
#: head_dim x 2 for K+V x 2 bytes, and turboquant KV is off), so 256K is ~20
#: GiB of cache on top of 21-28 GB of weights. That clears a 128 GB box and
#: collides with the 36 GB wired cap on a 64 GB one. `num_ctx_max` is set from
#: what the MODEL supports; this is what the HOST supports. Measured 2026-08-11.
CTX_TIER_MIN_RAM_GB: dict[str, int] = {"huge": 96}


def host_ram_gb() -> float | None:
    """Physical RAM in GiB, or None where it can't be determined.

    `sysconf` over `sysctl`: no subprocess, and it answers on both macOS and
    Linux. None must read as "unknown" at the call site, never as "too small" —
    a warning nobody can act on is worse than no warning."""
    import os as _os
    try:
        return (_os.sysconf("SC_PAGE_SIZE") * _os.sysconf("SC_PHYS_PAGES")) / 1024 ** 3
    except (ValueError, OSError, AttributeError):
        return None


def ctx_tier_ram_warning(tier: str) -> str | None:
    """Warning text for a tier this host is too small for, else None."""
    need = CTX_TIER_MIN_RAM_GB.get(tier)
    if need is None:
        return None
    have = host_ram_gb()
    if have is None or have >= need:
        return None
    return (f"{tier} ({CTX_TIERS[tier]:,} tokens) wants a {need}+ GB machine — "
            f"this host has {have:.0f} GB. The KV cache runs ~80 KiB/token, so "
            f"a filled window is ~{CTX_TIERS[tier] * 80 / 1024 ** 2:.0f} GiB on "
            f"top of the weights, past what the GPU memory cap can hold. The "
            f"window is a ceiling and grows as you use it, so the failure lands "
            f"mid-session when it fills, not now.")


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


# --- Is more window actually the answer? (2026-08-24) -----------------------
#
# `CTX_SUGGEST_PRESSURE` alone answers "was it tight", never "would a bigger
# window have helped". On 2026-08-24 (session 168f1825a1fd) a turn opened a
# 257,988-byte file and a 23,775-byte one in ONE step, the request died, and
# the footer offered `/ctx huge` — on a 128K window, for a turn where the
# problem was that a single tool result was ~64k tokens on its own. The next
# tier up would have bought a larger window for the same unbounded read to
# overflow. See `acceptance/chat_bigread_2026_08_24/EVIDENCE.md` finding 5.
#
# These are DISPLAY gates and nothing else: `CTX_SUGGEST_PRESSURE` keeps its
# value, no compaction threshold moves, and what gets dispatched is unchanged.

#: A single tool result at or above this share of the window is a read-BUDGET
#: problem (`LUXE_TOOL_BUDGET_CTX`, `limit=`, `grep`), not a window problem:
#: doubling the window doubles what one unbounded read is allowed to eat.
#: 0.25 is deliberately generous — a quarter of the window in one result is
#: already past anything a healthy multi-step turn produces.
CTX_SINGLE_RESULT_SHARE = 0.25

#: Abort reasons a bigger window genuinely addresses — the server saying the
#: prompt did not fit. Everything else (max steps, transport failures, a
#: cancelled turn) is a different problem wearing the same peak-pressure
#: number, and on an aborted turn that number is an EXTRAPOLATED estimate of a
#: request the server never accepted (EVIDENCE.md finding 3: 102.5% reported
#: on a request that was likely ~65% of the window), which makes it the
#: weakest possible basis for a recommendation.
_WINDOW_SHAPED_ABORT = (
    "context length",
    "context window",
    "context_length_exceeded",
    "maximum context",
    "prompt too long",
    "too many tokens",
)


def largest_tool_result_tokens(result) -> int:
    """Estimated tokens in the single biggest tool result of a turn, 0 if none.

    chars/4 on `ToolCall.bytes_out` — the same estimator the pressure signal
    uses, so this compares like with like. Reads reporting fields only; no
    loop state.
    """
    best = 0
    for call in getattr(result, "tool_calls", None) or []:
        try:
            b = int(getattr(call, "bytes_out", 0) or 0)
        except (TypeError, ValueError):
            continue
        best = max(best, b)
    return best // 4


def single_result_dominated(result, num_ctx: int) -> bool:
    """True when ONE tool result accounts for `CTX_SINGLE_RESULT_SHARE`+ of
    the window — the signature of a budget problem, not a window problem."""
    if num_ctx <= 0:
        return False
    return largest_tool_result_tokens(result) >= num_ctx * CTX_SINGLE_RESULT_SHARE


def abort_addressable_by_window(result) -> bool:
    """For an ABORTED turn: did it fail for a reason more window would fix?

    True only when the reason text names a context/window overflow. A turn
    that did not abort is not this function's business — callers check
    `aborted` first.
    """
    reason = (getattr(result, "abort_reason", "") or "").lower()
    return any(marker in reason for marker in _WINDOW_SHAPED_ABORT)


def ctx_suggestion(result, num_ctx: int, ceiling: int, *,
                   local_weights: bool = True) -> tuple[str, int] | None:
    """The `/ctx` tier to offer after a turn, or None to stay quiet.

    `local_weights=False` for a hosted endpoint: `CTX_TIER_MIN_RAM_GB` is
    about the machine holding the KV cache, and on a cloud backend that is not
    this one (see its note above). Defaults True — the pre-2026-08-24 answer,
    and the safe one when the origin lookup cannot tell.
    """
    peak = float(getattr(result, "peak_context_pressure", 0.0) or 0.0)
    if peak < CTX_SUGGEST_PRESSURE:
        return None
    if getattr(result, "aborted", False) and not abort_addressable_by_window(result):
        return None
    if single_result_dominated(result, num_ctx):
        return None
    nxt = next_tier_up(num_ctx, ceiling)
    if nxt is None:
        return None
    if local_weights and ctx_tier_ram_warning(nxt[0]):
        # Never RECOMMEND a tier this host cannot hold. `/ctx <tier>` typed by
        # hand still warns and proceeds — an explicit request is an
        # instruction; an unprompted suggestion that luxe already knows will
        # run out of memory mid-session is just bad advice.
        return None
    return nxt


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def aborted_ctx_line(result, num_ctx: int) -> str | None:
    """One qualified context line for an ABORTED turn, or None.

    The footer's `ctx:` is `last_prompt_tokens` — the last step the server
    ACCEPTED — while the peak-pressure figure describes the step that FAILED.
    Printed side by side without qualifiers they read as a contradiction: on
    2026-08-24 one turn rendered `ctx: 2% of 128K` directly above `context
    pressure 103%` and neither number was wrong (EVIDENCE.md finding 4). This
    line names which step each number is about; display only.
    """
    if not getattr(result, "aborted", False):
        return None
    accepted = int(getattr(result, "last_prompt_tokens", 0) or 0)
    peak = float(getattr(result, "peak_context_pressure", 0.0) or 0.0)
    bits: list[str] = []
    if accepted > 0:
        window = f"/{num_ctx // 1024}K" if num_ctx > 0 else ""
        bits.append(f"last accepted {_fmt_tokens(accepted)}{window}")
    if peak > 0:
        # `peak x num_ctx` lands back in SERVER-TRUTH tokens, not chars/4:
        # pressure is measured against `calibrated_ctx_limit` (= num_ctx / the
        # measured ratio), so the product is est x ratio. In session
        # 168f1825a1fd that was 71,616 est x 1.88 ~ 135k against a 128K
        # window — which is exactly why it reported 103%. Still an ESTIMATE of
        # a request the server never accepted, hence the label.
        est = f" ~{_fmt_tokens(int(peak * num_ctx))} est" if num_ctx > 0 else ""
        bits.append(f"attempted{est} ({peak:.0%}, estimated)")
    if not bits:
        return None
    return "context: " + " · ".join(bits)


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
    # What this session is about: "git" | "dir" | "none" (chat/project.py).
    # "none" = started somewhere that isn't a codebase: no index is built, and
    # the index-backed tools are withheld rather than failing per call.
    project_kind: str = "git"
    # Set per-turn by prepare_turn when the routed model's chat template can't
    # do tool calls (chat/modelcaps.py): the whole tool surface is withheld, so
    # the frame must say so or the model will narrate tool use it never did.
    tools_withheld: bool = False
    # Set once at session build (repl/tui) when the planeproxy binary exists:
    # injects PLANEPROXY_HINT so the model reaches for planeproxy_diag instead
    # of shell forensics. Defaults False so tests stay machine-independent.
    planeproxy_present: bool = False
    languages: frozenset = field(default_factory=frozenset)
    write_enabled: bool = False
    unrestricted_bash: bool = False  # set by /bash; only effective in write mode
    # set by /web: exposes web_fetch (+ web_search when a provider key
    # resolves). Default OFF — luxe is the OFFLINE fallback kit, so network
    # egress from a tool is opt-in per session, independent of write mode
    # (reading a web page mutates nothing locally).
    web_enabled: bool = False
    pinned_slot: str | None = None  # set by /use; consumed on the next turn
    num_ctx_override: int | None = None  # set by /ctx; clamped per-turn to num_ctx_max
    turns: list[ChatTurn] = field(default_factory=list)

    # -- spend (billable backends only; chat/cost.py) -------------------------
    # USD this session has been billed, summed from every turn's
    # AgentResult.cost_usd. Stays 0.0 on every local engine, which reports no
    # cost at all — and "no cost reported" must not render as a confident
    # $0.00, so the cost surfaces key on the backend being billable, not on
    # this being zero.
    session_cost_usd: float = 0.0
    turn_costs: list[float] = field(default_factory=list)
    # Session-scoped raise of the entry's `budget_usd` hard cap, set by
    # `/usage budget <usd>`. None = use the configured cap. Never persisted:
    # raising a spend cap is a decision about THIS session.
    budget_override_usd: float | None = None
    system_constraints: list[str] = field(default_factory=list)  # set by /sys; injected every turn
    # /attach staging: [{path, content, size, sha256, truncated}] read+capped by
    # the command; injected as <attached_files> into the NEXT turn only
    # (one-shot — build_extra_context clears it on consumption).
    attachments: list[dict] = field(default_factory=list)

    # -- observability (B2): tool-IO depth + reasoning stream are independent --
    verbose_level: str = "off"   # off | diff | full — set by /verbose
    show_reasoning: bool = False  # set by /reasoning; streams model prose live
    terse: bool = True           # set by /terse; injects TERSE_HINT to cut prose
    compact: bool = False        # set by /compact; tighter on-screen output ceiling

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
        mode_hints: list[str] = []
        if self.tools_withheld:
            mode_hints.append(NO_TOOLS_MODEL_HINT)
        if self.project_kind == "none":
            # No index here: say so once, in the frame, instead of letting the
            # model discover it by calling a tool that isn't on the list.
            mode_hints.append(NO_PROJECT_CHAT_HINT)
        if not self.write_enabled:
            mode_hints.append(READ_ONLY_CHAT_HINT)
        elif self.project_kind == "git":
            # Write mode in a git repo is the one state where the model can
            # actually run git; carry the user's workflow discipline (one
            # command per call, rebase not merge, no unrequested pushes).
            mode_hints.append(GIT_WORKFLOW_HINT)
        if self.planeproxy_present:
            # This machine runs the user's SSH-tunnel tool: point the model
            # at planeproxy_diag (and its no-CA/no-bypass doctrine) up front.
            mode_hints.append(PLANEPROXY_HINT)
        if mode_hints:
            parts.append("<session_mode>\n" + "\n\n".join(mode_hints)
                         + "\n</session_mode>")
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
        # /attach payload rides just BELOW <system_constraints> in the precedence
        # ladder (the user's explicit rules still outrank pasted file content)
        # and above everything else. ONE-SHOT: cleared here on consumption so it
        # feeds exactly the next turn; later turns recall it via history.
        if self.attachments:
            file_blocks = "\n".join(
                f'<file path="{a["path"]}">\n{a["content"]}\n</file>'
                for a in self.attachments
            )
            parts.insert(
                1 if self.system_constraints else 0,
                "<attached_files>\nThe user attached these files for this "
                f"turn:\n{file_blocks}\n</attached_files>",
            )
            self.attachments = []
        if not parts:
            # First turn, no memory, write mode on: nothing to disambiguate.
            return "", fold_version
        # Something precedes the request — echo it last for recency.
        parts.append(f"<current_request>\n{current_user_message.strip()}\n</current_request>")
        return "\n\n" + "\n\n".join(parts), fold_version

    def add_turn(self, turn: ChatTurn) -> None:
        self.turns.append(turn)
