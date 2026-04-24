"""Lightweight Python import graph for pre-retrieval.

Selectivity-based pre-retrieval applied to import relationships: given a
seed file, find files it imports plus files that import it (one hop).
Paired with `shared.trace_hints.parse_trace_paths`, this lets the
orchestrator pre-read the minimal useful neighborhood around a cited
file so the dispatched agent doesn't burn its first few tool calls
rediscovering the module boundary.

Scope: Python only. TS/Go/Rust follow the same pattern but need
per-language parsers — deferred until the Python version proves out.

Compression-benchmark finding (commit 608e04f): local coder models
regress on summarised / outlined context but gain on oracle-style
whole-file pre-reads. This module picks *which* files to pre-read; the
reading itself stays whole-file.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from cli.repo_survey import _IGNORE_DIRS


@dataclass
class ImportGraph:
    """One-hop import graph for .py files under `root`.

    `imports[p]`  — set of files `p` imports from (first-party only).
    `imported_by` — reverse index; built once after the forward pass.
    `root` / `mtime_key` — cache invalidation inputs.
    """

    root: Path
    mtime_key: float
    imports: dict[Path, frozenset[Path]] = field(default_factory=dict)
    imported_by: dict[Path, frozenset[Path]] = field(default_factory=dict)


_CACHE: dict[Path, ImportGraph] = {}


def _iter_py_files(root: Path) -> Iterator[Path]:
    root = root.resolve()
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
        cur_path = Path(cur)
        for fname in files:
            if fname.endswith(".py"):
                yield cur_path / fname


def _max_py_mtime(root: Path) -> float:
    """Newest `.py` mtime under `root`. Cheap cache key: if this matches
    a cached graph's `mtime_key`, no file has been edited since build."""
    newest = 0.0
    for p in _iter_py_files(root):
        try:
            m = p.stat().st_mtime
            if m > newest:
                newest = m
        except OSError:
            continue
    return newest


def _module_candidates(
    module: str, current_file: Path, root: Path
) -> list[Path]:
    """Resolve a dotted module name like `luxe.router` to possible file
    paths inside `root`. Returns up to 2 hits: the module file
    (`luxe/router.py`) and the package init (`luxe/router/__init__.py`)
    if either exists. Relative imports (`.foo`, `..bar`) resolve against
    `current_file`'s parent."""
    if not module:
        return []
    parts = module.split(".")
    out: list[Path] = []

    # Absolute import: try every path anchor under root that looks like
    # it could be a first-party package root. For luxe this means the
    # file exists at `root / parts[0] / … / parts[-1].py` or with an
    # __init__.py, OR `root / <parent> / parts[0] / …` (monorepos).
    # Keep it simple: walk parts[0] among top-level dirs.
    top = parts[0]
    # `root/<top>/…`
    candidate_base = root / top
    if candidate_base.exists():
        base = root
        rel = Path(*parts[:-1]) / (parts[-1] + ".py") if len(parts) > 1 else Path(parts[0] + ".py")
        p1 = base / rel
        if p1.exists():
            out.append(p1.resolve())
        pkg_init = base / Path(*parts) / "__init__.py"
        if pkg_init.exists():
            out.append(pkg_init.resolve())
    return out


def _resolve_relative(
    module: str, level: int, current_file: Path, root: Path
) -> list[Path]:
    """Resolve a `from . import X` / `from ..pkg import y` style import."""
    base = current_file.resolve().parent
    for _ in range(level - 1):
        base = base.parent
    parts = module.split(".") if module else []
    if not parts:
        # `from . import X` with level=1 — can't resolve to a specific
        # file without knowing which X; skip (imprecision acceptable).
        return []
    candidate_file = base / Path(*parts[:-1]) / (parts[-1] + ".py") if len(parts) > 1 else base / (parts[0] + ".py")
    candidate_init = base / Path(*parts) / "__init__.py"
    out: list[Path] = []
    try:
        root_real = root.resolve()
        for cand in (candidate_file, candidate_init):
            if cand.exists():
                cand = cand.resolve()
                # Must stay inside repo root.
                try:
                    cand.relative_to(root_real)
                except ValueError:
                    continue
                out.append(cand)
    except OSError:
        pass
    return out


def _imports_for_file(path: Path, root: Path) -> frozenset[Path]:
    try:
        src = path.read_text(errors="ignore")
    except OSError:
        return frozenset()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return frozenset()
    hits: set[Path] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for p in _module_candidates(alias.name, path, root):
                    hits.add(p)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                for p in _resolve_relative(
                    node.module or "", node.level, path, root
                ):
                    hits.add(p)
            elif node.module:
                # `from pkg import X` can import a submodule (file) or a
                # symbol (not a file). Try the package path first, then
                # each `pkg.X` as a submodule candidate so we catch the
                # common `from luxe.agents import review, code, …` form.
                for p in _module_candidates(node.module, path, root):
                    hits.add(p)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    dotted = f"{node.module}.{alias.name}"
                    for p in _module_candidates(dotted, path, root):
                        hits.add(p)
    return frozenset(hits)


def build_graph(root: Path) -> ImportGraph:
    """Build (or return cached) import graph for `.py` files under root.
    Invalidates when any `.py` under root has been touched since the
    previous build. ~1s on a 10k-LOC repo; amortised free across
    subtasks within one task."""
    root_real = root.resolve()
    newest = _max_py_mtime(root_real)
    cached = _CACHE.get(root_real)
    if cached is not None and cached.mtime_key >= newest:
        return cached

    imports: dict[Path, frozenset[Path]] = {}
    reverse: dict[Path, set[Path]] = {}
    for f in _iter_py_files(root_real):
        fr = f.resolve()
        edges = _imports_for_file(fr, root_real)
        imports[fr] = edges
        for target in edges:
            reverse.setdefault(target, set()).add(fr)
    imported_by = {k: frozenset(v) for k, v in reverse.items()}

    g = ImportGraph(
        root=root_real,
        mtime_key=newest,
        imports=imports,
        imported_by=imported_by,
    )
    _CACHE[root_real] = g
    return g


def neighbors(
    graph: ImportGraph,
    path: Path,
    *,
    max_neighbors: int = 5,
) -> list[Path]:
    """Return first-hop neighbors of `path`: files it imports + files
    that import it. Imports first (they're usually the API the module
    under inspection depends on), deduped, capped at `max_neighbors`."""
    p = path.resolve()
    fwd = graph.imports.get(p, frozenset())
    rev = graph.imported_by.get(p, frozenset())
    seen: set[Path] = set()
    out: list[Path] = []
    for group in (fwd, rev):
        for n in sorted(group, key=lambda x: str(x)):
            if n == p or n in seen:
                continue
            seen.add(n)
            out.append(n)
            if len(out) >= max_neighbors:
                return out
    return out


def clear_cache() -> None:
    """Tests / explicit invalidation."""
    _CACHE.clear()


__all__ = ["ImportGraph", "build_graph", "neighbors", "clear_cache"]
