"""Progress ledger — a tiny, persisted record of agent progress so multi-round
work (``continue work`` / ``/goal``) doesn't re-derive state from the filesystem
every turn. Stored per session at ``~/.luxe/sessions/<id>/ledger.json``.

The transcript evidence that motivated this (iteration-2): every ``continue
work`` turn re-read the 18.4KB ``plan.md`` plus every source file before doing
anything — the dominant token sink, and what pushed the 32K box to 88% context
pressure. The ledger lets the model keep an explicit, compact memory of what it
has *decided* and *done* instead of buying those facts back with tokens.

Two channels feed it:
  * deterministic — files written/edited, observed from the tool-event stream
    (``repl.py`` accumulates paths and calls :func:`record_files`). This works
    even if the model never touches the tool.
  * model-driven — the ``update_ledger`` tool (see :func:`make_update_ledger_tool`)
    lets the model record decisions, in-progress items, blockers, and
    completions in its own words. Decisions/commitments are often more valuable
    to retain than file summaries.

:func:`render` produces the compact block injected near the TOP of
``extra_context`` (B5); :func:`render_rich` shows it in verbose mode (B2).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from luxe.memory.session import session_dir

# Keep each list bounded so the injected block can never balloon the prompt —
# the whole point is to SHRINK per-turn tokens. Oldest entries fall off.
_LIST_CAP = 40
# Belt-and-suspenders cap on the rendered block so a pathological ledger can't
# dominate a small context window.
_RENDER_CHAR_BUDGET = 2000


@dataclass
class Ledger:
    """The persisted working state for one session."""

    goal: str = ""
    decided: list[str] = field(default_factory=list)   # commitments ("use click")
    completed: list[str] = field(default_factory=list)  # done items
    in_progress: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)      # auto: paths written/edited

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Ledger":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        # Coerce list fields defensively — a hand-edited / partial file mustn't crash.
        for k in ("decided", "completed", "in_progress", "blocked", "files"):
            v = known.get(k)
            if v is None:
                known[k] = []
            elif isinstance(v, str):
                known[k] = [v]
            elif not isinstance(v, list):
                known[k] = list(v)
        if not isinstance(known.get("goal", ""), str):
            known["goal"] = str(known.get("goal", ""))
        return cls(**known)

    def is_empty(self) -> bool:
        return not (self.goal or self.decided or self.completed
                    or self.in_progress or self.blocked or self.files)


def _ledger_path(session_id: str):
    return session_dir(session_id) / "ledger.json"


def _dedup_cap(seq: list[str], cap: int = _LIST_CAP) -> list[str]:
    """Order-preserving de-dup, then keep the most recent `cap` entries."""
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        s = (item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out[-cap:]


def load(session_id: str) -> Ledger:
    """Load the ledger for a session, or an empty one if absent/corrupt."""
    if not session_id:
        return Ledger()
    p = _ledger_path(session_id)
    if not p.is_file():
        return Ledger()
    try:
        return Ledger.from_dict(json.loads(p.read_text()))
    except (json.JSONDecodeError, OSError, TypeError):
        return Ledger()


def save(session_id: str, ledger: Ledger) -> None:
    """Atomically persist the ledger (mirrors memory.session._write_meta)."""
    if not session_id:
        return
    p = _ledger_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger.to_dict(), indent=2))
    tmp.replace(p)


def record_files(session_id: str, paths: list[str]) -> None:
    """Deterministic channel: note files the agent wrote/edited this turn."""
    if not paths:
        return
    led = load(session_id)
    led.files = _dedup_cap(led.files + [p for p in paths if p])
    save(session_id, led)


def _as_list(value) -> list[str]:
    """Accept a list, a comma/newline string, or None from a tool arg."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        # Allow "a, b" or newline-separated; a bare string is a single item.
        parts = [p.strip() for p in value.replace("\n", ",").split(",")]
        return [p for p in parts if p]
    return [str(value)]


def apply_update(session_id: str, delta: dict) -> str:
    """Model-driven channel: merge a partial update into the ledger.

    `delta` may carry any of: goal (str), decided/completed/in_progress/blocked
    (list[str] or comma string). Items newly marked completed are removed from
    in_progress so the two lists don't drift. Returns a short human summary that
    becomes the tool's result text.
    """
    led = load(session_id)
    if isinstance(delta.get("goal"), str) and delta["goal"].strip():
        led.goal = delta["goal"].strip()

    new_completed = _as_list(delta.get("completed"))
    led.completed = _dedup_cap(led.completed + new_completed)
    led.decided = _dedup_cap(led.decided + _as_list(delta.get("decided")))
    led.blocked = _dedup_cap(led.blocked + _as_list(delta.get("blocked")))

    # in_progress: add new, then drop anything that just got completed.
    completed_set = {c.lower() for c in led.completed}
    merged_ip = _dedup_cap(led.in_progress + _as_list(delta.get("in_progress")))
    led.in_progress = [x for x in merged_ip if x.lower() not in completed_set]

    save(session_id, led)
    counts = (f"goal={'set' if led.goal else 'unset'} "
              f"decided={len(led.decided)} completed={len(led.completed)} "
              f"in_progress={len(led.in_progress)} blocked={len(led.blocked)}")
    return f"Ledger updated. {counts}"


# ---------------------------------------------------------------- render ----

def _section(title: str, items: list[str], bullet: str = "- ") -> list[str]:
    if not items:
        return []
    return [f"{title}:"] + [f"{bullet}{i}" for i in items]


def render(ledger: Ledger, *, char_budget: int = _RENDER_CHAR_BUDGET) -> str:
    """Compact plaintext block for injection into extra_context (B5).

    Returns "" when the ledger is empty so first turns stay byte-clean.
    """
    if ledger.is_empty():
        return ""
    lines: list[str] = []
    if ledger.goal:
        lines.append(f"Goal: {ledger.goal}")
    lines += _section("Decided", ledger.decided)
    lines += _section("Completed", ledger.completed)
    lines += _section("In progress", ledger.in_progress)
    lines += _section("Blocked", ledger.blocked)
    if ledger.files:
        lines.append("Files already written/edited: " + ", ".join(ledger.files))
    body = "\n".join(lines)
    if len(body) > char_budget:
        body = body[: char_budget - 1] + "…"
    hint = ("This is your working state from earlier turns. Trust it: do NOT "
            "re-read files or re-derive facts already captured here — only read "
            "what is missing, changed, or explicitly in progress. Keep it current "
            "with the update_ledger tool as you make decisions and finish work.")
    return f"<working_state>\n{hint}\n\n{body}\n</working_state>"


def render_rich(ledger: Ledger) -> str:
    """Rich-markup view for verbose mode (B2)."""
    if ledger.is_empty():
        return "[dim]· ledger empty[/]"
    out: list[str] = ["[bold]· progress ledger[/]"]
    if ledger.goal:
        out.append(f"  [cyan]goal[/] {ledger.goal}")
    for title, items, style in (
        ("decided", ledger.decided, "magenta"),
        ("completed", ledger.completed, "green"),
        ("in_progress", ledger.in_progress, "yellow"),
        ("blocked", ledger.blocked, "red"),
    ):
        for it in items:
            out.append(f"  [{style}]{title}[/] {it}")
    if ledger.files:
        out.append(f"  [dim]files[/] {', '.join(ledger.files)}")
    return "\n".join(out)


# ------------------------------------------------------ update_ledger tool ----

_UPDATE_LEDGER_PARAMS = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "description": "Overall objective (set once)."},
        "decided": {"type": "array", "items": {"type": "string"},
                    "description": "Decisions/commitments made (e.g. 'use click')."},
        "completed": {"type": "array", "items": {"type": "string"},
                      "description": "Items finished this turn."},
        "in_progress": {"type": "array", "items": {"type": "string"},
                        "description": "Work currently underway."},
        "blocked": {"type": "array", "items": {"type": "string"},
                    "description": "Blockers / open questions."},
    },
    "required": [],
}

_UPDATE_LEDGER_DESC = (
    "Record durable progress to your working-state ledger so future turns don't "
    "re-read files to rediscover it. Pass only the fields that changed. Use this "
    "when you make a design decision, finish a unit of work, or hit a blocker."
)


def make_update_ledger_tool(session_id: str):
    """Build the (ToolDef, ToolFn) pair bound to a session — wired in via the
    existing extra_tool_defs/extra_tool_fns seam (like chat's bash override)."""
    from luxe.tools.base import ToolDef

    def _fn(args: dict) -> tuple[str, str | None]:
        try:
            summary = apply_update(session_id, args or {})
            return summary, None
        except Exception as e:  # never let a bookkeeping tool crash the loop
            return "", f"{type(e).__name__}: {e}"

    defn = ToolDef(
        name="update_ledger",
        description=_UPDATE_LEDGER_DESC,
        parameters=_UPDATE_LEDGER_PARAMS,
    )
    return defn, _fn
