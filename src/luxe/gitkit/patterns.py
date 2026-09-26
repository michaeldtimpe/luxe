"""Shared finding-shape patterns — ONE definition of a file reference, a
severity word, and a severity header, imported by `store` (min-severity filter
+ prior-findings slice), `diffscope` (hunk-overlap prior) and `deep`
(heuristic salvage + evidence keys).

Until 2026-09 each module carried its own copy and they had drifted: the
store's header regex only knew `## High` while real reports nest `### High`
under `## Bugs & security` (so `--min-severity` hid nothing and gitchange's
`<prior_findings>` was always empty); deep's severity regex had no leading
word boundary ("flow"/"allow"/"below" read as `low`); diffscope's ref regex
accepted any `word.word NN` ("requests.get 5"). Pure data shaping — no
prompt text lives here (gitkit.sdd Forbids).
"""

from __future__ import annotations

import re

SEVERITIES = ("critical", "high", "medium", "low")
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0, "": 0}
_SEV_ALT = "|".join(SEVERITIES)
_SHORTHAND = {"c": "critical", "h": "high", "m": "medium", "l": "low"}

#: A whole-word severity (never the tail of "flow" / "allow" / "below").
SEV_WORD_RE = re.compile(rf"\b({_SEV_ALT})\b", re.IGNORECASE)

# Source extensions a finding may cite. Anchored so `requests.get 5` or
# `e.g 3` never read as a file reference; longer spellings first so `.c`
# cannot shadow `.cpp`.
_EXTS = (
    "pyi", "py", "rs", "tsx", "ts", "jsx", "mjs", "cjs", "js", "go", "bash", "zsh",
    "sh", "yaml", "yml", "toml", "json", "cpp", "cxx", "cc", "hpp", "hh", "c", "h",
    "java", "kts", "kt", "scala", "rb", "php", "cs", "swift", "mm", "m", "lua",
    "pl", "sql", "html", "scss", "css", "vue", "svelte", "dart", "exs", "ex",
    "erl", "hs", "clj", "tfvars", "tf", "md", "cfg", "ini", "gradle", "r",
)
EXT_ALT = "|".join(_EXTS)
_PATH = rf"[\w./-]*\w\.(?:{EXT_ALT})(?![\w-])"

#: A file reference, with or without a line (`a.py`, `src/x.ts`).
FILE_REF_RE = re.compile(rf"(?<![\w/.-])(?P<path>{_PATH})", re.IGNORECASE)
#: A file:line reference — `a.py:12`, `a.py line 12`, `a.py: line 12`,
#: `a.py 12`, `a.py#L12`.
FILE_LINE_RE = re.compile(
    rf"(?<![\w/.-])(?P<path>{_PATH})(?:#L|:\s*(?:line\s+)?|\s+(?:line\s+)?)"
    r"(?P<line>\d+)\b", re.IGNORECASE)

#: A markdown heading at levels 2-4 that names a severity: `## High`,
#: `### Critical — …`, or the per-finding shorthand `### M — title`.
_SEV_HDR_RE = re.compile(
    rf"^(?P<hashes>#{{2,4}})\s*(?:(?P<word>{_SEV_ALT})\b"
    r"|(?P<abbr>[CHML])(?=\s*(?:[—–:-]|$)))(?P<rest>.*)$",
    re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s")
# What may trail a GROUP header's severity word without making it a
# per-finding header ("### High (3)", "### High severity", "### Low:").
_GROUP_TAIL_RE = re.compile(
    r"^[\s():\-—–\d]*(?:severity|findings?|issues?|priority)?[\s():\-—–\d]*$",
    re.IGNORECASE)


def heading_level(line: str) -> int:
    """`#`-count of a markdown heading line, 0 for anything else."""
    m = _HEADING_RE.match(line)
    return len(m.group(1)) if m else 0


def severity_header(line: str) -> tuple[int, str, bool] | None:
    """(level, severity, per_finding) for a severity heading, else None.

    `per_finding` is True when the heading IS one finding (`### M — title`,
    `### High — SQL injection in x`) rather than a group header (`### High`)
    whose findings are the list items beneath it."""
    m = _SEV_HDR_RE.match(line.rstrip())
    if not m:
        return None
    sev = (m.group("word") or _SHORTHAND[m.group("abbr").lower()]).lower()
    per_finding = bool(m.group("abbr")) or not _GROUP_TAIL_RE.match(m.group("rest"))
    return len(m.group("hashes")), sev, per_finding


def is_fence(line: str) -> bool:
    """A fenced-code delimiter (``` or ~~~) — headings inside fences are code
    comments (`# audio_meta.py:29`), never report structure."""
    s = line.lstrip()
    return s.startswith("```") or s.startswith("~~~")
