"""BM25 search over repo source files — better than grep for natural-language queries.

The architect and workers can use `bm25_search(query)` to find files most
relevant to a goal phrasing like "where is auth middleware applied?" without
having to know exact tokens. The index is built once per session at session
start and reused across subtasks (cacheable).

Per plan §7: BM25 alone misses semantic queries on >100k LOC repos; the
companion `symbols.find_symbol` AST tool covers exact symbol lookup. Vector
search is punted to v1.1.

Tokenization: split on non-alphanumerics, lowercase, drop tokens shorter
than 3 chars. Good enough for code identifiers + natural-language queries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi

from luxe.tools.base import ToolDef, ToolFn


_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_DEFAULT_EXCLUDES = {".git", "node_modules", "__pycache__", ".venv", "venv",
                     "dist", "build", "target", ".next", ".nuxt",
                     ".pytest_cache", ".ruff_cache", ".mypy_cache"}
_DEFAULT_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go",
                       ".java", ".cpp", ".cc", ".h", ".hpp", ".rb", ".php",
                       ".kt", ".swift", ".cs", ".md"}


def _tokenize(text: str) -> list[str]:
    """Tokenize for BM25: split on non-alphanumerics, then split camelCase /
    PascalCase, lowercase, drop tokens shorter than 2 chars.

    `authenticate_user` → ["authenticate", "user"]
    `UserService`       → ["user", "service"]
    """
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        for piece in _CAMEL_SPLIT_RE.split(raw):
            piece = piece.lower()
            if len(piece) >= 2:
                out.append(piece)
    return out


@dataclass
class BM25Index:
    paths: list[str]
    bm25: Any
    repo_root: Path

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        if not query.strip() or not self.paths:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self.bm25.get_scores(tokens)
        top = sorted(zip(self.paths, scores), key=lambda x: x[1], reverse=True)[:k]
        return [(p, float(s)) for p, s in top if s > 0.0]


def build_bm25_index(
    repo_root: str | Path,
    extensions: set[str] | None = None,
    excludes: set[str] | None = None,
    max_file_bytes: int = 256 * 1024,
) -> BM25Index:
    """Walk repo_root, build a BM25 index over source files (line-tokenized)."""
    root = Path(repo_root).resolve()
    extensions = extensions if extensions is not None else _DEFAULT_EXTENSIONS
    excludes = excludes if excludes is not None else _DEFAULT_EXCLUDES

    paths: list[str] = []
    docs: list[list[str]] = []

    for cur, dirs, files in __import__("os").walk(root):
        dirs[:] = [d for d in dirs if d not in excludes and not d.startswith(".") or d in {".github"}]
        for fname in files:
            p = Path(cur) / fname
            if p.suffix.lower() not in extensions:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > max_file_bytes:
                continue
            try:
                text = p.read_text(errors="replace")
            except OSError:
                continue
            tokens = _tokenize(text)
            if not tokens:
                continue
            rel = str(p.relative_to(root))
            paths.append(rel)
            docs.append(tokens)

    if not paths:
        # rank_bm25 raises on empty corpus; build a 1-doc empty index.
        return BM25Index(paths=[], bm25=BM25Okapi([["__empty__"]]), repo_root=root)

    bm25 = BM25Okapi(docs)
    return BM25Index(paths=paths, bm25=bm25, repo_root=root)


# --- tool surface ----------------------------------------------------------

_index: BM25Index | None = None


def set_index(index: BM25Index) -> None:
    global _index
    _index = index


def reset_index() -> None:
    global _index
    _index = None


def bm25_search_def() -> ToolDef:
    return ToolDef(
        name="bm25_search",
        description=(
            "Find source files most relevant to a natural-language query. "
            "Better than grep for queries like 'where is auth middleware applied?'. "
            "Returns up to k file paths with relevance scores."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Natural-language query"},
                "k": {"type": "integer",
                      "description": "Max results (default 10)"},
            },
            "required": ["query"],
        },
    )


def _bm25_search_fn(args: dict[str, Any]) -> tuple[str, str | None]:
    if _index is None:
        return "", "BM25 index not built (set_index must be called first)"
    query = str(args.get("query", "")).strip()
    k = int(args.get("k", 10))
    if not query:
        return "", "query is required"
    hits = _index.search(query, k=k)
    if not hits:
        return "(no matches)", None
    lines = [f"{path}\t{score:.2f}" for path, score in hits]
    return "\n".join(lines), None


TOOL_FNS: dict[str, ToolFn] = {"bm25_search": _bm25_search_fn}
CACHEABLE = {"bm25_search"}
