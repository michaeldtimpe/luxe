"""gitkit kinds — the per-kind table, report-title slicing, and the shared
activity display.

The bottom of the gitkit import graph: `runner`, `brief`, and every `deep/`
stage read these, and this module imports nothing from gitkit. They lived in
`runner.py` until 2026-10, which forced `deep/`, `brief`, and the salvage /
render stages to import them inside function bodies to dodge the cycle back
through `runner → deep`. `runner` re-exports every name for existing callers.
"""

from __future__ import annotations

import re
from collections import Counter

from luxe.agents import prompts
from luxe.cancel import raise_if_cancelled

# Per-run generation ceiling for the FINAL report (safety margin, not the fix —
# WS1's prompt discipline keeps reports well under this). The first test hit the
# chat role's 8192 cap and truncated mid-report. Applied via a per-run
# model_copy in run_git_report; never mutates the shared role / chat.yaml.
GITKIT_MAX_TOKENS = 16384

# kind -> (task_type overlay reused, goal ask, directive HINT). No new task
# types: each maps onto an existing overlay; the per-kind directive rides in the
# goal (gitkit.sdd / agents.sdd single-source rule).
KINDS: dict[str, tuple[str, str, str]] = {
    "gitaudit": (
        "review",
        prompts.GIT_AUDIT_ASK,
        prompts.GIT_AUDIT_HINT,
    ),
    "gitchange": (
        "review",
        prompts.GIT_CHANGE_ASK,
        prompts.GIT_CHANGE_HINT,
    ),
    # Internal kind only (never a CLI command): `gitaudit --base/--pr` switches
    # to it after resolving the diff scope. Reuses the existing `review`
    # task_type (gitkit.sdd: no new task types).
    "gitaudit-diff": (
        "review",
        prompts.GIT_AUDIT_DIFF_ASK,
        prompts.GIT_AUDIT_DIFF_HINT,
    ),
}

def with_evidence_boundary(goal: str) -> str:
    """Append the evidence-boundary clause when LUXE_GITKIT_EVIDENCE_BOUNDARY=1
    (exact string). Unset: `goal` is returned unchanged, byte for byte."""
    import os
    if os.environ.get("LUXE_GITKIT_EVIDENCE_BOUNDARY") != "1":
        return goal
    return f"{goal}\n\n{prompts.GITKIT_EVIDENCE_BOUNDARY}"


_TITLES = {
    "gitaudit": "Repository audit",
    "gitchange": "Change plan",
    "gitaudit-diff": "Diff audit",
}

_H1_RE = re.compile(r"^#\s", re.MULTILINE)


def extract_report(text: str, kind: str | None = None) -> str:
    """Slice the report from its header onward, dropping any leading monologue the
    model emits before it (WS1 safety net for the "treats the final turn as more
    reasoning" failure mode).

    When `kind` is given, rank ALL `# ` headers by case-insensitive word overlap
    with the kind's required title (`_TITLES[kind]`) and slice from the
    best-scoring one (tie → earliest) — an exact title scores highest, but a
    near-title ("# Repository audit — preliminary") still beats a monologue
    heading. All headers scoring zero → fall back to the first `# ` header, then
    to the unchanged text (never drop content)."""
    text = text or ""
    if kind and kind in _TITLES:
        want = {w.lower() for w in re.findall(r"[a-z]+", _TITLES[kind], re.I)}
        best_pos, best_score = -1, 0
        for m in re.finditer(r"^#\s+(.+)$", text, re.MULTILINE):
            words = {w.lower() for w in re.findall(r"[a-z]+", m.group(1), re.I)}
            score = len(want & words)
            if score > best_score:        # strict > keeps the EARLIEST on ties
                best_score, best_pos = score, m.start()
        if best_pos != -1:
            return text[best_pos:]
    m = _H1_RE.search(text)
    return text[m.start():] if m else text


def _activity_callbacks(update, cancel=None):
    """Build (on_tool_event, on_token) that coalesce tool calls into a phased
    status string passed to `update(text)`: `analyzing… read_file (31) · grep
    (12)` while tools fire, then `writing report…` once the model starts emitting
    prose after the last tool. When `cancel` is provided (interactive TUI), each
    callback raises `ChatCancelled` if cancellation was requested. Factored out so
    the coalescing/phasing is unit-testable without a TTY."""
    counts: Counter = Counter()
    state = {"writing": False}

    def _text() -> str:
        if not counts:
            return "analyzing…"
        top = " · ".join(f"{n} ({c})" for n, c in counts.most_common(4))
        return f"analyzing… {top}"

    def on_event(tc):
        counts[getattr(tc, "name", "?")] += 1
        state["writing"] = False  # more tools → back to analyzing
        update(_text())
        if cancel is not None:
            raise_if_cancelled(cancel)

    def on_token(_delta):
        if cancel is not None:
            raise_if_cancelled(cancel)
        # First prose after at least one tool call = the report being written.
        if counts and not state["writing"]:
            state["writing"] = True
            update("writing report…")

    return on_event, on_token


def run_with_activity(console, label: str, cancel, fn, *,
                      update_prefix: str = "", plain_note: str | None = None):
    """Call `fn(on_event, on_token)` under the phased activity display.

    On a terminal: a `console.status` spinner reading `label…`, updated with
    `update_prefix` + the coalesced tool counts. Off a terminal: no spinner —
    `plain_note` (if given) is printed once and the callbacks only honour
    `cancel`. Returns whatever `fn` returns; `ChatCancelled` /
    `KeyboardInterrupt` propagate to the caller, which owns the message."""
    if console.is_terminal:
        with console.status(f"[dim]{label}…[/]", spinner="dots") as status:
            on_event, on_token = _activity_callbacks(
                lambda t: status.update(f"[dim]{update_prefix}{t}[/]"),
                cancel=cancel)
            return fn(on_event, on_token)
    if plain_note is not None:
        console.print(plain_note)
    on_event, on_token = _activity_callbacks(lambda t: None, cancel=cancel)
    return fn(on_event, on_token)
