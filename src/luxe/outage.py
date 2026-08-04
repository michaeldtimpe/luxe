"""The offline emergency card: `OUTAGE.md` at the repo root.

One reader, two surfaces (`luxe outage` and `/outage` in a session). Imports
nothing that can touch the network or load a model — this is the module that
has to work when everything else does not.
"""

from __future__ import annotations

import re
from pathlib import Path

# repo root = .../src/luxe/outage.py -> up three
CARD_PATH = Path(__file__).resolve().parent.parent.parent / "OUTAGE.md"


def card_path() -> Path:
    return CARD_PATH


def load_card() -> str:
    """The card's text, or a short self-describing fallback if it is missing.

    Never raises: an operator reaching for this during an outage must get
    something actionable even from a damaged checkout.
    """
    try:
        return CARD_PATH.read_text()
    except OSError as e:
        return (f"# OUTAGE\n\nOUTAGE.md is unreadable ({e}).\n\n"
                "- `luxe ready` — host preflight\n"
                "- `luxe chat` / `luxe code` — local sessions\n"
                "- `luxe smoke` — generation drill\n"
                "- `~/.luxe/sessions/<id>/debug.log` — forensics\n")


_FENCE_RE = re.compile(r"```.*?\n(.*?)```", re.DOTALL)
_INLINE_RE = re.compile(r"`([^`\n]+)`")
_CMD_RE = re.compile(r"(?:^|\s)luxe\s+([a-z][a-z0-9-]*)")


def _code_spans(body: str) -> list[str]:
    """Fenced blocks + inline-backtick spans — i.e. the parts of the card that
    claim to be runnable. Prose ("if luxe itself is broken") is deliberately
    excluded: it isn't a command reference and must not be linted as one."""
    fences = _FENCE_RE.findall(body)
    prose = _FENCE_RE.sub("\n", body)
    return fences + _INLINE_RE.findall(prose)


def referenced_commands(text: str | None = None) -> set[str]:
    """Every `luxe <sub>` command name the card presents as runnable.

    The card-vs-CLI consistency test asserts each of these is a registered
    command (or alias), which is what keeps the card from rotting as the CLI
    moves. Flag-only mentions (`luxe --help`) and the shell wrappers
    (`luxe-chat`) don't match by construction.
    """
    body = load_card() if text is None else text
    found: set[str] = set()
    for span in _code_spans(body):
        found.update(_CMD_RE.findall(span))
    return found
