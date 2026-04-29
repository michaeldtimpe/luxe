"""Repo summary for the architect — token-budgeted overview with
symbol_index_coverage so the architect knows when to fall back to BM25.

Output is a single ~2k-token block:
- Top-level directory layout (depth 2)
- Language breakdown (file count + LOC)
- 30 largest source files
- 30 most-recently-changed files (last 90 days)
- symbol_index_coverage: dict[language: file_count]
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


_LANGUAGE_EXTENSIONS = {
    "python":     [".py"],
    "javascript": [".js", ".jsx", ".mjs", ".cjs"],
    "typescript": [".ts", ".tsx"],
    "rust":       [".rs"],
    "go":         [".go"],
    "java":       [".java"],
    "ruby":       [".rb"],
    "php":        [".php"],
    "kotlin":     [".kt"],
    "swift":      [".swift"],
    "csharp":     [".cs"],
    "cpp":        [".cpp", ".cc", ".h", ".hpp", ".cxx"],
    "c":          [".c", ".h"],
    "shell":      [".sh", ".bash", ".zsh"],
    "yaml":       [".yaml", ".yml"],
    "markdown":   [".md", ".markdown"],
}

_DEFAULT_EXCLUDES = {".git", "node_modules", "__pycache__", ".venv", "venv",
                     "dist", "build", "target", ".next", ".nuxt",
                     ".pytest_cache", ".ruff_cache", ".mypy_cache"}


def _detect_language(suffix: str) -> str | None:
    suffix = suffix.lower()
    for lang, exts in _LANGUAGE_EXTENSIONS.items():
        if suffix in exts:
            return lang
    return None


@dataclass
class FileInfo:
    rel_path: str
    language: str
    loc: int
    bytes: int


@dataclass
class RepoSummary:
    file_count: int = 0
    total_loc: int = 0
    languages: dict[str, int] = field(default_factory=dict)        # name → file count
    languages_loc: dict[str, int] = field(default_factory=dict)    # name → loc count
    top_dirs: list[tuple[str, int]] = field(default_factory=list)  # (path, file count)
    largest_files: list[FileInfo] = field(default_factory=list)
    recent_files: list[str] = field(default_factory=list)
    symbol_index_coverage: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        """Format as a ~2k token markdown block for the architect."""
        lines: list[str] = []
        lines.append(f"## Repository overview ({self.file_count} files, "
                     f"{self.total_loc:,} LOC)")
        lines.append("")
        if self.languages:
            lines.append("### Languages")
            for lang, count in sorted(self.languages.items(),
                                       key=lambda x: -x[1]):
                loc = self.languages_loc.get(lang, 0)
                lines.append(f"- {lang}: {count} files, {loc:,} LOC")
            lines.append("")
        if self.symbol_index_coverage:
            lines.append("### AST symbol index coverage")
            covered = ", ".join(f"{lang} ({n})" for lang, n in
                                sorted(self.symbol_index_coverage.items(),
                                       key=lambda x: -x[1]))
            lines.append(f"`find_symbol` covers: {covered}")
            uncovered = [lang for lang in self.languages
                         if lang not in self.symbol_index_coverage]
            if uncovered:
                lines.append(f"Falls back to `bm25_search` for: "
                             f"{', '.join(uncovered)}")
            lines.append("")
        if self.top_dirs:
            lines.append("### Top-level directories")
            for path, count in self.top_dirs[:15]:
                lines.append(f"- `{path}` ({count} files)")
            lines.append("")
        if self.largest_files:
            lines.append("### Largest source files")
            for fi in self.largest_files[:30]:
                lines.append(f"- `{fi.rel_path}` ({fi.language}, "
                             f"{fi.loc} LOC)")
            lines.append("")
        if self.recent_files:
            lines.append("### Most recently changed (last 90 days)")
            for path in self.recent_files[:30]:
                lines.append(f"- `{path}`")
            lines.append("")
        return "\n".join(lines)


def _count_lines(path: Path) -> int:
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _git_recent_files(repo_root: Path, days: int = 90) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "log", f"--since={days}.days", "--name-only",
             "--pretty=format:"],
            cwd=repo_root, capture_output=True, text=True, check=False,
        )
        if out.returncode != 0:
            return []
        seen: set[str] = set()
        recent: list[str] = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line or line in seen:
                continue
            seen.add(line)
            recent.append(line)
            if len(recent) >= 60:
                break
        return recent
    except (OSError, subprocess.SubprocessError):
        return []


def build_repo_summary(
    repo_root: str | Path,
    excludes: set[str] | None = None,
    symbol_coverage: dict[str, int] | None = None,
) -> RepoSummary:
    """Walk repo_root, produce a RepoSummary for the architect.

    `symbol_coverage` is the dict from build_symbol_index(); pass it through
    so the architect sees which languages are covered by AST symbols.
    """
    root = Path(repo_root).resolve()
    excludes = excludes if excludes is not None else _DEFAULT_EXCLUDES

    files: list[FileInfo] = []
    top_dir_counts: dict[str, int] = {}

    for cur, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if d not in excludes and not d.startswith(".") or d in {".github"}]
        for fname in fs:
            p = Path(cur) / fname
            lang = _detect_language(p.suffix)
            if lang is None:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            loc = _count_lines(p)
            rel = str(p.relative_to(root))
            files.append(FileInfo(rel_path=rel, language=lang, loc=loc, bytes=size))
            # Top-dir bucket: first segment of relative path (or "." for root files)
            top = rel.split(os.sep, 1)[0] if os.sep in rel else "."
            top_dir_counts[top] = top_dir_counts.get(top, 0) + 1

    summary = RepoSummary()
    summary.file_count = len(files)
    summary.total_loc = sum(f.loc for f in files)
    for f in files:
        summary.languages[f.language] = summary.languages.get(f.language, 0) + 1
        summary.languages_loc[f.language] = summary.languages_loc.get(f.language, 0) + f.loc
    summary.top_dirs = sorted(top_dir_counts.items(), key=lambda x: -x[1])
    summary.largest_files = sorted(files, key=lambda f: -f.loc)[:30]
    summary.recent_files = _git_recent_files(root)
    summary.symbol_index_coverage = dict(symbol_coverage or {})
    return summary
