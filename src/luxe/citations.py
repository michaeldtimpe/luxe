"""Diff-aware citation linter — verifies file:line tokens in the final report.

Per plan §6: this is a build-breaking gate for v1.0. Zero unresolved citations
across all acceptance fixtures is a release requirement.

Diff-aware: when workers edit a file, the original line numbers shift. Strict
line-existence checking would fail those runs spuriously. Instead we use the
ValidatorEnvelope `snippet` field — workers and the synthesizer carry the snippet
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

from luxe.sdd import SddParseError
from luxe.spec_resolver import _glob_matches, find_all_sdd


@dataclass
class ValidatorFinding:
    path: str
    line: int
    snippet: str
    severity: str = "info"
    description: str = ""


@dataclass
class ValidatorRemoved:
    original: str
    reason: str = ""


@dataclass
class ValidatorEnvelope:
    status: str = "cleared"  # cleared | verified | ambiguous
    verified: list[ValidatorFinding] = field(default_factory=list)
    removed: list[ValidatorRemoved] = field(default_factory=list)
    summary: str = ""

    @property
    def is_ambiguous(self) -> bool:
        return self.status == "ambiguous"

    @property
    def is_cleared(self) -> bool:
        return self.status == "cleared"


_CITATION_RE = re.compile(
    r"`?(?P<path>[\w./_-]+\.[\w]+):(?P<line>\d+)(?:-(?P<line_end>\d+))?`?"
)
# Reject IPv4-shaped paths — `127.0.0.1:8000` matches the citation regex
# (path=127.0.0.1 because `.1` looks like a `.ext` suffix, line=8000) but is
# almost always a host:port reference in deployment docs, not a file:line
# citation. Without this guard, isomer-quickstart's synthesizer report
# (which mentions `127.0.0.1:27001` for the dashboard URL) reports 2
# unresolved citations and fails the build-breaking gate. Anchor handles
# both bare `127.0.0.1` and URL-form `//127.0.0.1` (the citation regex's
# path group greedily eats leading `/` characters from `http://...`).
_IPV4_PATH_RE = re.compile(r"(?:^|/)\d+\.\d+\.\d+\.\d+$")
_FUZZY_WINDOW = 20

# Directories skipped during bare-filename resolution. A vendored copy under
# `node_modules/` or a build artifact under `dist/` shouldn't make a citation
# resolve to the wrong file.
_BARE_FILENAME_EXCLUDE_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})


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
class SpecFinding:
    """SpecDD Lever 2 lint signal: an edit relative to a `.sdd` contract.

    `kind` is one of:
      - `spec_violation` — modified path matches a `.sdd` `Forbids:` glob.
        STRICT gate (defense in depth against rename/mv evasion of the
        tool-side check).
      - `spec_orphan` — modified path is outside every `Owns:` glob in
        the chain, but at least one Owns glob exists. WARNING ONLY at
        Lever 2; promotion to strict is deferred to Lever 3 after the
        implicit-ownership convention is shaken out on real fixtures.

    `path` is repo-root-relative. `glob` and `sdd_path` identify the
    rule's source for reporting.
    """

    kind: str  # "spec_violation" | "spec_orphan"
    path: str
    glob: str | None
    sdd_path: str | None  # repo-root-relative


@dataclass
class LintResult:
    citations: list[CitationResult] = field(default_factory=list)
    spec_findings: list[SpecFinding] = field(default_factory=list)
    repo_root: Path | None = None
    base_sha: str = ""

    @property
    def unresolved(self) -> list[CitationResult]:
        bad = {"missing_file", "out_of_range", "content_mismatch", "shifted_unverified"}
        return [c for c in self.citations if c.status in bad]

    @property
    def spec_violations(self) -> list[SpecFinding]:
        return [f for f in self.spec_findings if f.kind == "spec_violation"]

    @property
    def spec_orphans(self) -> list[SpecFinding]:
        return [f for f in self.spec_findings if f.kind == "spec_orphan"]

    @property
    def is_blocking(self) -> bool:
        return len(self.unresolved) > 0 or len(self.spec_violations) > 0

    def summary(self) -> str:
        from collections import Counter
        c = Counter(r.status for r in self.citations)
        parts = [f"{k}={v}" for k, v in sorted(c.items())]
        if self.spec_violations:
            parts.append(f"spec_violation={len(self.spec_violations)}")
        if self.spec_orphans:
            parts.append(f"spec_orphan={len(self.spec_orphans)}")
        return ", ".join(parts)


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
        # Skip IPv4-shaped paths (`127.0.0.1:8000` is a host:port, not file:line).
        if _IPV4_PATH_RE.search(path):
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


def _resolve_bare_filename(repo_root: Path, basename: str) -> Path | None:
    """Resolve a bare filename (no `/`) to its canonical repo path when the
    repo contains exactly one file with that basename. Returns None when
    the filename is ambiguous or absent. Skips vendored / build / cache
    directories to avoid resolving to the wrong copy.

    This forgives synthesizer-prose truncations like `dashboard.py:71` when
    the canonical path is `bot/dashboard.py:71` and the deliverable's own
    citations are correct. A bare filename that resolves uniquely is a
    legitimate citation, not a hallucination — so the linter accepting it
    is more correct, not looser.
    """
    matches: list[Path] = []
    for p in repo_root.rglob(basename):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(repo_root).parts
        if any(part in _BARE_FILENAME_EXCLUDE_DIRS for part in rel_parts):
            continue
        matches.append(p)
        if len(matches) > 1:
            return None  # ambiguous
    return matches[0] if matches else None


def _check_one(citation: Citation, finding: ValidatorFinding | None,
               repo_root: Path, changed: set[str], deleted: set[str]) -> CitationResult:
    path = citation.path
    line = citation.line
    snippet = finding.snippet if finding else ""

    if path in deleted:
        return CitationResult(citation, "resolved_by_deletion",
                              detail="file deleted in diff (intentional fix)")

    abs_path = (repo_root / path)
    if not abs_path.is_file() and "/" not in path:
        resolved = _resolve_bare_filename(repo_root, path)
        if resolved is not None:
            path = str(resolved.relative_to(repo_root))
            abs_path = resolved
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

    SpecDD Lever 2: also evaluates every modified file against the
    repo's `.sdd` chain, producing `spec_violation` (strict) and
    `spec_orphan` (warning) findings on the result.
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
    spec_findings = _check_spec_compliance(root, changed - deleted)
    return LintResult(
        citations=results,
        spec_findings=spec_findings,
        repo_root=root,
        base_sha=base_sha,
    )


def _check_spec_compliance(repo_root: Path, modified_paths: set[str]) -> list[SpecFinding]:
    """Evaluate each modified path against the repo's `.sdd` chain.

    Emits `spec_violation` for any path matching a `Forbids:` glob in
    any `.sdd`, and `spec_orphan` for any path not matching any
    `Owns:` glob WHEN at least one `Owns:` glob exists in the chain.
    Repos without `.sdd` files emit no findings.

    Malformed `.sdd` short-circuits the check — a single emitted
    `spec_violation` finding flags the malformed file and stops; the
    tool-side enforcement layer surfaces the same error during the
    agent loop, so the lint surfacing is for human visibility.
    """
    if not modified_paths:
        return []
    try:
        sdds = find_all_sdd(repo_root)
    except SddParseError as e:
        return [SpecFinding(
            kind="spec_violation",
            path=str(getattr(e, "path", "")),
            glob=None,
            sdd_path=str(getattr(e, "path", "")),
            )]
    if not sdds:
        return []

    out: list[SpecFinding] = []
    has_any_owns = any(sf.owns for sf in sdds)

    for path in sorted(modified_paths):
        normalized = path.replace("\\", "/").lstrip("/")
        # Forbids check — strict gate.
        forbidden = False
        for sf in sdds:
            for glob in sf.forbids:
                if _glob_matches(glob, normalized):
                    try:
                        sdd_rel = str(sf.path.relative_to(repo_root))
                    except ValueError:
                        sdd_rel = str(sf.path)
                    out.append(SpecFinding(
                        kind="spec_violation",
                        path=normalized,
                        glob=glob,
                        sdd_path=sdd_rel,
                    ))
                    forbidden = True
                    break
            if forbidden:
                break
        if forbidden:
            continue

        # Orphan check — warning only. Skipped when no Owns globs exist
        # anywhere in the chain (nothing to be an orphan from).
        if not has_any_owns:
            continue
        owned = False
        for sf in sdds:
            for glob in sf.owns:
                if _glob_matches(glob, normalized):
                    owned = True
                    break
            if owned:
                break
        if not owned:
            out.append(SpecFinding(
                kind="spec_orphan",
                path=normalized,
                glob=None,
                sdd_path=None,
            ))

    return out
