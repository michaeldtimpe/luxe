"""Diff-aware citation linter — verifies file:line tokens in the final report.

Per plan §6: this is a build-breaking gate for v1.0. Zero unresolved citations
across all acceptance fixtures is a release requirement.

Diff-aware: when workers edit a file, the original line numbers shift. Strict
line-existence checking would fail those runs spuriously. Instead we use the
validator's `snippet` field — workers and the synthesizer carry the snippet
verbatim alongside `path:line`, and the linter does a fuzzy snippet match
within ±20 lines of the cited line in the post-edit file.

Resolution outcomes per citation:
  - resolved        — file unchanged, line exists, snippet (if any) matches
  - resolved_shifted — file edited, snippet matches within ±20 lines of cited line
  - resolved_by_deletion — file deleted in diff (intentional fix, OK)
  - missing_file    — file does not exist in current state, not deleted in diff
  - out_of_range    — file unchanged but cited line is past EOF
  - content_mismatch — file unchanged but the line at cited line doesn't match snippet
  - shifted_unverified — file edited, snippet not found within ±20 lines
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from luxe.agents.validator import ValidatorEnvelope, ValidatorFinding


_CITATION_RE = re.compile(
    r"`?(?P<path>[\w./_-]+\.[\w]+):(?P<line>\d+)(?:-(?P<line_end>\d+))?`?"
)
_FUZZY_WINDOW = 20


@dataclass
class Citation:
    path: str
    line: int
    line_end: int | None = None
    raw: str = ""


@dataclass
class CitationResult:
    citation: Citation
    status: str  # see module docstring
    detail: str = ""
    matched_line: int | None = None  # post-edit line where snippet was found


@dataclass
class LintResult:
    citations: list[CitationResult] = field(default_factory=list)
    repo_root: Path | None = None
    base_sha: str = ""

    @property
    def unresolved(self) -> list[CitationResult]:
        bad = {"missing_file", "out_of_range", "content_mismatch", "shifted_unverified"}
        return [c for c in self.citations if c.status in bad]

    @property
    def is_blocking(self) -> bool:
        return len(self.unresolved) > 0

    def summary(self) -> str:
        from collections import Counter
        c = Counter(r.status for r in self.citations)
        return ", ".join(f"{k}={v}" for k, v in sorted(c.items()))


def extract_citations(text: str) -> list[Citation]:
    """Extract every `path:line` (or `path:line-line`) from `text`."""
    out: list[Citation] = []
    seen: set[tuple[str, int, int | None]] = set()
    for m in _CITATION_RE.finditer(text or ""):
        path = m.group("path")
        try:
            line = int(m.group("line"))
        except ValueError:
            continue
        line_end_raw = m.group("line_end")
        line_end = int(line_end_raw) if line_end_raw else None
        # Skip obvious non-citations: requires an extension we treat as source-y
        # (the regex already enforces a .ext suffix, but version strings like
        # 1.2.3:4 would never match because they lack a letter in the extension).
        if "." not in path:
            continue
        key = (path, line, line_end)
        if key in seen:
            continue
        seen.add(key)
        out.append(Citation(path=path, line=line, line_end=line_end, raw=m.group(0)))
    return out


def _git_changed_files(repo_root: Path, base_sha: str) -> set[str]:
    """Files changed (added/modified/deleted) since base_sha."""
    if not base_sha:
        return set()
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", base_sha, "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            return set()
        return {line.strip() for line in out.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


def _git_deleted_files(repo_root: Path, base_sha: str) -> set[str]:
    """Files deleted between base_sha and HEAD."""
    if not base_sha:
        return set()
    try:
        out = subprocess.run(
            ["git", "diff", "--diff-filter=D", "--name-only", base_sha, "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if out.returncode != 0:
            return set()
        return {line.strip() for line in out.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


def _normalize(s: str) -> str:
    """Normalize for fuzzy snippet match: collapse whitespace, lowercase trivial markup."""
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _read_lines(p: Path) -> list[str]:
    try:
        return p.read_text(errors="replace").splitlines()
    except OSError:
        return []


def _snippet_matches(file_lines: list[str], near_line: int, snippet: str,
                     window: int = _FUZZY_WINDOW) -> int | None:
    """Return the post-edit line number where `snippet` first matches within
    ±window of near_line, or None if no match.

    `near_line` is 1-indexed (matches editor convention).
    """
    if not snippet:
        return None
    snippet_lines = [_normalize(s) for s in snippet.splitlines() if s.strip()]
    if not snippet_lines:
        return None

    n = len(file_lines)
    lo = max(0, near_line - 1 - window)
    hi = min(n, near_line - 1 + window + len(snippet_lines))
    target = " ".join(snippet_lines)

    # Sliding window of len(snippet_lines) over file_lines
    span = max(len(snippet_lines), 1)
    for i in range(lo, max(lo + 1, hi - span + 1)):
        chunk = " ".join(_normalize(line) for line in file_lines[i:i + span])
        if target in chunk:
            return i + 1
    return None


def _check_one(citation: Citation, finding: ValidatorFinding | None,
               repo_root: Path, changed: set[str], deleted: set[str]) -> CitationResult:
    path = citation.path
    line = citation.line
    snippet = finding.snippet if finding else ""

    if path in deleted:
        return CitationResult(citation, "resolved_by_deletion",
                              detail="file deleted in diff (intentional fix)")

    abs_path = (repo_root / path)
    if not abs_path.is_file():
        return CitationResult(citation, "missing_file",
                              detail=f"{path} does not exist post-edit")

    file_lines = _read_lines(abs_path)
    if line < 1:
        return CitationResult(citation, "out_of_range",
                              detail=f"line {line} is invalid")

    if path in changed:
        # File was edited — use fuzzy snippet match.
        if not snippet:
            # No snippet to verify against; if cited line is in range, accept.
            if line <= len(file_lines):
                return CitationResult(citation, "resolved_shifted",
                                      detail=f"file edited; line {line} in range, no snippet to verify",
                                      matched_line=line)
            return CitationResult(citation, "shifted_unverified",
                                  detail=f"file edited; line {line} past EOF and no snippet provided")
        matched = _snippet_matches(file_lines, line, snippet)
        if matched is not None:
            return CitationResult(citation, "resolved_shifted",
                                  detail=f"snippet matched at line {matched}",
                                  matched_line=matched)
        return CitationResult(citation, "shifted_unverified",
                              detail=f"snippet not found within ±{_FUZZY_WINDOW} lines of {line}")
    else:
        # File unchanged — strict line-existence check.
        if line > len(file_lines):
            return CitationResult(citation, "out_of_range",
                                  detail=f"line {line} > file length {len(file_lines)}")
        if snippet:
            matched = _snippet_matches(file_lines, line, snippet)
            if matched is None:
                return CitationResult(citation, "content_mismatch",
                                      detail=f"snippet does not match within ±{_FUZZY_WINDOW} of line {line}")
            return CitationResult(citation, "resolved",
                                  detail=f"unchanged file; snippet matches at line {matched}",
                                  matched_line=matched)
        return CitationResult(citation, "resolved",
                              detail=f"unchanged file; line {line} in range, no snippet to verify",
                              matched_line=line)


def lint_report(
    report_text: str,
    repo_root: Path | str,
    base_sha: str = "",
    envelope: ValidatorEnvelope | None = None,
) -> LintResult:
    """Verify every citation in `report_text` against the current repo state.

    `envelope` provides the original validator findings (with snippets); the
    linter uses these to forgive line-shift after worker edits.
    """
    root = Path(repo_root).resolve()
    citations = extract_citations(report_text)

    by_path_line: dict[tuple[str, int], ValidatorFinding] = {}
    if envelope is not None:
        for f in envelope.verified:
            by_path_line[(f.path, f.line)] = f

    changed = _git_changed_files(root, base_sha)
    deleted = _git_deleted_files(root, base_sha)

    results = [
        _check_one(c, by_path_line.get((c.path, c.line)), root, changed, deleted)
        for c in citations
    ]
    return LintResult(citations=results, repo_root=root, base_sha=base_sha)
