"""Tree-sitter AST symbol index — exact lookup for class/function/method/etc.

Per plan §7: BM25 covers natural-language queries; AST covers exact symbol
lookup ("show me class UserService"). Together they give >100k-LOC repos a
useful retrieval surface without an embedding model in the loop.

Supported languages (v1.0): python, javascript, typescript, rust, go.
For repos in unsupported languages (java, ruby, php, etc.) the index simply
contains no entries; `find_symbol` returns an empty list with a `note`
field so the agent knows to fall back to bm25_search.

Coverage transparency (Reviewer R1.1 round 2): the index exposes
`coverage()` returning a dict {language: indexed_file_count} that the
architect's repo summary surfaces. A worker that searches for `class Foo`
on a Java repo gets a clear "fall back to BM25" note rather than silent zero.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from luxe.tools.base import ToolDef, ToolFn


# Languages we support, mapped to the tree-sitter-languages identifier and
# the source file extensions that route to that parser.
_LANGUAGE_EXTENSIONS = {
    "python":     [".py"],
    "javascript": [".js", ".jsx", ".mjs", ".cjs"],
    "typescript": [".ts"],
    "tsx":        [".tsx"],
    "rust":       [".rs"],
    "go":         [".go"],
}

# Per-language tree-sitter node types we index as symbols. Kind names are
# normalised across languages so `kind="class"` works whether you're in
# Python, JS, or Rust.
_LANGUAGE_QUERIES: dict[str, list[tuple[str, str]]] = {
    "python": [
        ("function_definition", "function"),
        ("class_definition", "class"),
    ],
    "javascript": [
        ("function_declaration", "function"),
        ("class_declaration", "class"),
        ("method_definition", "method"),
    ],
    "typescript": [
        ("function_declaration", "function"),
        ("class_declaration", "class"),
        ("interface_declaration", "interface"),
        ("type_alias_declaration", "type"),
        ("method_definition", "method"),
    ],
    "tsx": [
        ("function_declaration", "function"),
        ("class_declaration", "class"),
        ("interface_declaration", "interface"),
        ("method_definition", "method"),
    ],
    "rust": [
        ("function_item", "function"),
        ("struct_item", "struct"),
        ("enum_item", "type"),
        ("trait_item", "interface"),
        ("type_item", "type"),
        ("const_item", "const"),
        ("static_item", "var"),
    ],
    "go": [
        ("function_declaration", "function"),
        ("method_declaration", "method"),
        ("type_declaration", "type"),
    ],
}

_DEFAULT_EXCLUDES = {".git", "node_modules", "__pycache__", ".venv", "venv",
                     "dist", "build", "target", ".next", ".nuxt",
                     ".pytest_cache", ".ruff_cache", ".mypy_cache"}

SUPPORTED_LANGUAGES = list(_LANGUAGE_EXTENSIONS.keys())


@dataclass
class Symbol:
    name: str
    kind: str          # function | class | method | struct | interface | type | const | var
    language: str
    path: str
    start_line: int    # 1-indexed
    end_line: int


@dataclass
class SymbolIndex:
    repo_root: Path
    symbols: list[Symbol] = field(default_factory=list)
    coverage: dict[str, int] = field(default_factory=dict)

    def find(self, name: str, kind: str = "any", language: str = "any",
             k: int = 50) -> list[Symbol]:
        out: list[Symbol] = []
        for s in self.symbols:
            if name and name not in s.name:
                continue
            if kind != "any" and s.kind != kind:
                continue
            if language != "any" and s.language != language:
                continue
            out.append(s)
            if len(out) >= k:
                break
        return out


# --- parsing ---------------------------------------------------------------

def _detect_language(suffix: str) -> str | None:
    suffix = suffix.lower()
    for lang, exts in _LANGUAGE_EXTENSIONS.items():
        if suffix in exts:
            return lang
    return None


def _extract_name(node: Any, src: bytes) -> str:
    """Best-effort symbol name extraction across language node shapes."""
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return src[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
    # Some Rust/TS variants put the identifier as the first identifier child
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            return src[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
    return "<anonymous>"


def _walk_for_symbols(node: Any, src: bytes, kind_map: dict[str, str],
                      lang: str, rel_path: str, out: list[Symbol]) -> None:
    if node.type in kind_map:
        name = _extract_name(node, src)
        out.append(Symbol(
            name=name,
            kind=kind_map[node.type],
            language=lang,
            path=rel_path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        ))
    for child in node.children:
        _walk_for_symbols(child, src, kind_map, lang, rel_path, out)


def _parse_file(path: Path, lang: str) -> list[Symbol]:
    from tree_sitter_languages import get_parser
    try:
        parser = get_parser(lang)
    except Exception:
        return []
    try:
        src = path.read_bytes()
    except OSError:
        return []
    tree = parser.parse(src)
    rel = str(path)
    out: list[Symbol] = []
    kind_map = dict(_LANGUAGE_QUERIES.get(lang, []))
    if not kind_map:
        return out
    _walk_for_symbols(tree.root_node, src, kind_map, lang, rel, out)
    return out


def build_symbol_index(
    repo_root: str | Path,
    excludes: set[str] | None = None,
    max_file_bytes: int = 256 * 1024,
) -> SymbolIndex:
    root = Path(repo_root).resolve()
    excludes = excludes if excludes is not None else _DEFAULT_EXCLUDES

    symbols: list[Symbol] = []
    coverage: dict[str, int] = {}

    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in excludes and not d.startswith(".") or d in {".github"}]
        for fname in files:
            p = Path(cur) / fname
            lang = _detect_language(p.suffix)
            if lang is None:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > max_file_bytes:
                continue
            file_syms = _parse_file(p, lang)
            if file_syms:
                rel = str(p.relative_to(root))
                for s in file_syms:
                    s.path = rel
                symbols.extend(file_syms)
                coverage[lang] = coverage.get(lang, 0) + 1

    return SymbolIndex(repo_root=root, symbols=symbols, coverage=coverage)


# --- tool surface ----------------------------------------------------------

_index: SymbolIndex | None = None


def set_index(index: SymbolIndex) -> None:
    global _index
    _index = index


def reset_index() -> None:
    global _index
    _index = None


def find_symbol_def() -> ToolDef:
    return ToolDef(
        name="find_symbol",
        description=(
            "Find a class/function/method/struct/etc. by name in the AST "
            "symbol index. Exact lookup; faster and more precise than "
            "bm25_search for known symbols. Falls back gracefully when the "
            "language is not in the symbol index — pass any name, see "
            "the `note` in the response."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name (substring match)"},
                "kind": {"type": "string",
                         "description": "Optional: function/class/method/struct/interface/type/const/var/any"},
                "language": {"type": "string",
                             "description": "Optional: python/javascript/typescript/rust/go/any"},
                "k": {"type": "integer", "description": "Max results (default 50)"},
            },
            "required": ["name"],
        },
    )


def _find_symbol_fn(args: dict[str, Any]) -> tuple[str, str | None]:
    if _index is None:
        return "", "symbol index not built (set_index must be called first)"
    name = str(args.get("name", "")).strip()
    if not name:
        return "", "name is required"
    kind = str(args.get("kind", "any")).strip().lower() or "any"
    language = str(args.get("language", "any")).strip().lower() or "any"
    k = int(args.get("k", 50))

    hits = _index.find(name=name, kind=kind, language=language, k=k)

    # If language=any and zero hits AND coverage shows nothing for the file's
    # language, we surface a helpful fall-back note.
    if not hits:
        if language != "any" and language not in _index.coverage:
            return ("", f"language `{language}` is not covered by the symbol "
                    f"index (covered: {sorted(_index.coverage)}). "
                    "Try `bm25_search` for natural-language search.")
        if not _index.coverage:
            return ("", "symbol index is empty (no supported languages "
                    "detected in this repo). Use `bm25_search` instead.")
        return "(no matches)", None

    lines = [f"{s.kind:<10}  {s.path}:{s.start_line}-{s.end_line}\t{s.name}"
             for s in hits]
    return "\n".join(lines), None


TOOL_FNS: dict[str, ToolFn] = {"find_symbol": _find_symbol_fn}
CACHEABLE = {"find_symbol"}
