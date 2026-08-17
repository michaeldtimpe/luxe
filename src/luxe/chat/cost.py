"""Session spend accounting for billable chat backends (chat-only).

Every local engine in the fleet is free at the point of use, so luxe never had
to count. The OpenRouter carve-out (2026-08-17, luxe.sdd + chat.sdd) is metered
on every token, which turns two things into requirements rather than niceties:

  VISIBILITY — what this session has spent has to be readable without asking,
  so the running total rides the status bar and the turn footer and `/status`
  and `/usage`. `GenerationTiming.cost_usd` is per REQUEST and a turn is many
  requests, so `AgentResult.cost_usd` sums them and this module sums those.

  A HARD CAP — `budget_usd` on the backend entry is refused-before-dispatch,
  never interrupted mid-turn (killing a turn that has already been billed for
  wastes the money it just spent) and never silently raised. `/usage budget
  <usd>` raises it, for this session only, as a deliberate act.

Everything here is guarded and degrades to "no cost surface at all" when the
active endpoint is not billable — a local session must not grow a $0.00 chip,
and a backend luxe cannot read must not raise into a render path.
"""

from __future__ import annotations


def entry_for(slots):
    """The active `backends:` entry, or None. Never raises."""
    getter = getattr(slots, "active_entry", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def is_billable(slots) -> bool:
    """True when tokens on the ACTIVE endpoint cost money."""
    entry = entry_for(slots)
    if entry is None:
        return False
    try:
        return bool(entry.is_billable())
    except Exception:
        return False


def spent(session) -> float:
    """USD this session has been billed so far (0.0 when nothing reported)."""
    return float(getattr(session, "session_cost_usd", 0.0) or 0.0)


def cap_usd(session, slots) -> float | None:
    """The effective hard cap in USD, or None when uncapped.

    The session override (`/usage budget <usd>`) wins over the configured
    `budget_usd`; an entry with no `budget_usd` and no override is uncapped,
    which is a supported configuration — deliberately unwatched.
    """
    override = getattr(session, "budget_override_usd", None)
    if override is not None:
        return float(override)
    entry = entry_for(slots)
    if entry is None:
        return None
    cap = getattr(entry, "budget_usd", None)
    return float(cap) if cap is not None else None


def remaining_usd(session, slots) -> float | None:
    """Headroom under the cap, or None when uncapped. Never negative."""
    cap = cap_usd(session, slots)
    if cap is None:
        return None
    return max(0.0, cap - spent(session))


def fmt(usd: float) -> str:
    """Compact USD for a status bar: sub-cent spend still has to be legible,
    and a session that has spent $12 should not read as $12.0000."""
    if usd < 0.01:
        return f"${usd:.4f}"
    if usd < 1:
        return f"${usd:.3f}"
    return f"${usd:.2f}"


def refusal(session, slots) -> str | None:
    """The message refusing the NEXT turn, or None to let it run.

    Called before dispatch by both front-ends. Names the three things a
    refusal has to name (chat.sdd: every refusal names its unlock): what has
    been spent, what the cap is, and the command that raises it.
    """
    if not is_billable(slots):
        return None
    cap = cap_usd(session, slots)
    if cap is None:
        return None
    used = spent(session)
    if used < cap:
        return None
    return (f"spend cap reached — this session has billed {fmt(used)} against "
            f"a {fmt(cap)} cap on backend '{getattr(slots, 'backend_name', '?')}'. "
            f"Raise it with `/usage budget <usd>` (e.g. `/usage budget "
            f"{cap * 2:.2f}`), or `/backend <local>` to keep working for free.")


def record_turn(session, result, status=None) -> float:
    """Fold one completed turn's cost into the session. Returns the turn cost.

    Zero-cost turns are NOT recorded: a local endpoint reports no cost at all,
    and a list of 0.0 entries would make `/usage` claim a per-turn history it
    does not have.
    """
    turn_cost = float(getattr(result, "cost_usd", 0.0) or 0.0)
    if turn_cost <= 0:
        return 0.0
    session.session_cost_usd = spent(session) + turn_cost
    session.turn_costs.append(turn_cost)
    if status is not None and hasattr(status, "session_cost_usd"):
        status.session_cost_usd = session.session_cost_usd
    return turn_cost


def credits(slots) -> tuple[float | None, float | None]:
    """(key spend limit, key usage) in USD, or (None, None).

    On-demand only (`/usage`) — an account balance is not something to poll
    from a render path, and this is the one place in luxe that reads
    `Backend.credits()` (OpenRouter `/v1/key`: the current key's own
    `limit`/`usage`; the account-wide `/v1/credits` route is management-key
    only and 403s for inference keys). `limit` may be null on an uncapped
    key — that renders as usage with no limit, not as zero. None means
    "couldn't ask": offline, no such route on this engine, or a key the
    endpoint rejected. The caller says so out loud rather than printing a
    number it did not get.
    """
    getter = getattr(getattr(slots, "backend", None), "credits", None)
    if getter is None:
        return None, None
    try:
        data = getter()
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None

    def _num(key):
        raw = data.get(key)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return _num("limit"), _num("usage")
