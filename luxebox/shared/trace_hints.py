"""Parse file:line references out of tracebacks / error output.

Tiny pure-stdlib utility. Used by:
- the compression benchmark's `stack_trace_guided` retrieval strategy
  (`luxebox/strategies/stages.py`), to seed retrieval from the files
  pytest mentions when it fails; and
- luxe's task orchestrator, to pre-read files the user has pasted
  traceback output for so the dispatched agent doesn't spend its
  first few tool calls rediscovering them.

Covers two common Python formats:
- pytest short-form: `path.py:42` / `path.py:42:`
- Python long-form:  `File "path.py", line 42, in funcname`

Restricted to `.py` because non-Python frames in a traceback are
usually system libs we don't want to surface.
"""

from __future__ import annotations

import re
from pathlib import Path

# pytest / short-form: path.py:LINE
_SHORT_RE = re.compile(r"([A-Za-z0-9_./\-]+\.py):\d+")
# Python traceback long-form: File "path.py", line LINE
_LONG_RE = re.compile(r'"([A-Za-z0-9_./\-]+\.py)",\s*line\s+\d+')

# Exported for callers that want to recognise the short form
# directly (e.g. finding the specific line number for a known path).
TRACE_PATH_RE = _SHORT_RE


def parse_trace_paths(text: str, repo_root: Path) -> list[Path]:
    """Extract `.py` file references from `text` and resolve them
    against `repo_root`. Returns existing files in first-seen order,
    deduped, with any path that escapes the repo root dropped.

    Empty/None text returns []. Missing files are silently skipped —
    the caller decides what to do with an empty result."""
    if not text:
        return []

    # Collect (match-start, captured-path) pairs from both formats so
    # the output is ordered by first mention regardless of which form
    # appeared first.
    hits: list[tuple[int, str]] = []
    for pat in (_SHORT_RE, _LONG_RE):
        for m in pat.finditer(text):
            hits.append((m.start(), m.group(1)))
    hits.sort()

    repo_real = repo_root.resolve()
    out: list[Path] = []
    seen: set[Path] = set()
    for _, rel in hits:
        candidate = (repo_root / rel).resolve()
        try:
            candidate.relative_to(repo_real)
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out
