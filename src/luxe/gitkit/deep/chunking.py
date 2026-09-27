"""gitkit deep mode — Stages 0/1, deterministic: the effective window and
the single-pass-vs-deep footprint gate, file enumeration, the token-budgeted
chunk partition, the survey's framing-file picker, and the wall estimate.
Pure data shaping — no model passes, no persistence."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from luxe.fswalk import iter_pruned
from luxe.repo_index import (
    _DEFAULT_EXCLUDES,
    _count_lines,
    _detect_language,
)


# --- tuning constants (per-stage wall fit from the 46-repo sweep) ------------

# Repo footprint above this fraction of the window → deep mode (primary trigger).
# File count is NOT predictive (flying-fair failed at 66 files); token footprint
# is. Secondary signals stay out of the gate by design.
_DEEP_TRIGGER_FRAC = 0.55
# Deep passes deliberately run at the BASE num_ctx, NOT the expanded num_ctx_max.
# The first aurora run expanded to 131072 and it backfired: chunks became huge
# (30-47 files, 600k-1.4M prompt tokens), passes were slow, and the model
# rambled without ever concluding (truncating before its findings). Smaller
# base-window chunks keep each pass focused enough that the model concludes —
# the "eaten in stages" intent (see deep_window).
# Ask for confirmation (interactive only) once a deep run needs this many chunks.
_LARGE_CONFIRM_CHUNKS = 8
# Per-STAGE wall estimates (seconds), fit from the 46-repo sweep, 2026-06
# (n=292 chunk / 30 survey / 30 synthesis passes on the M5 Max champion). The old
# flat `_SECONDS_PER_CHUNK = 300 × (n+2)` over-estimated EVERY deep repo by ~48%.
# Crucially chunk wall does NOT scale with LOC (correlation r=0.07) — a chunk pass
# is a roughly fixed ~210s + amortized ~0.6 format-recovery pass/chunk (~25s) ≈ 235s.
# Survey ~70s, synthesis ~70s. This per-stage model lands within ~9% of actuals.
_SURVEY_S = 70
_CHUNK_S = 235
_SYNTH_S = 70
# Cap on symbols listed per chunk + entities/findings kept after compaction.
_MAX_CHUNK_SYMBOLS = 60

# Rough chars→tokens for cheap per-file sizing without reading every file.
_CHARS_PER_TOKEN = 4

# Path substrings / filename stems that mark high-priority (entry / security /
# core) files so cross-references accumulate usefully early.
_PRIORITY_SUBSTRINGS = (
    "auth", "secur", "login", "crypto", "password", "secret", "token",
    "session", "webhook", "payment", "billing", "middleware", "permission",
    "api", "route", "router", "handler", "controller", "endpoint", "server",
    "gateway", "admin", "oauth", "jwt",
)
_ENTRY_STEMS = {
    "main", "app", "server", "index", "cli", "__main__", "urls", "settings",
    "config", "wsgi", "asgi", "manage", "routes", "application",
}

# Deterministic framing-file globs for the survey (richer than README — infra
# often reveals architecture/risk better). Matched case-insensitively on the
# POSIX relative path.
_FRAMING_PATTERNS = (
    r"readme(\.|$)", r"security(\.|$)", r"contributing(\.|$)",
    r"\.github/workflows/", r"dockerfile", r"docker-compose", r"compose\.ya?ml$",
    r"\.tf$", r"\.tfvars$", r"k8s/", r"kubernetes/", r"helm/", r"deploy",
    r"procfile", r"makefile", r"justfile",
    r"pyproject\.toml$", r"package\.json$", r"go\.mod$", r"cargo\.toml$",
    r"requirements.*\.txt$", r"setup\.(py|cfg)$",
    r"urls\.py$", r"settings.*\.py$", r"wsgi\.py$", r"asgi\.py$",
    r"(^|/)(main|app|server|index|cli)\.[a-z]+$",
    r"routes?\.[a-z]+$", r"router\.[a-z]+$",
)
_FRAMING_RE = re.compile("|".join(_FRAMING_PATTERNS), re.IGNORECASE)


# --- data shapes ------------------------------------------------------------

@dataclass
class FileRec:
    rel: str            # POSIX relative path
    language: str
    loc: int
    bytes: int
    tokens: int         # cheap estimate (bytes // 4)
    top_dir: str        # first path segment, or "." for root files
    priority: int       # 0 = entry/security, 1 = recent, 2 = normal


@dataclass
class Chunk:
    index: int
    files: list[str] = field(default_factory=list)      # rel paths
    dirs: list[str] = field(default_factory=list)
    label: str = ""
    est_tokens: int = 0
    loc: int = 0
    symbols: list[str] = field(default_factory=list)    # symbols defined here
    oversized: list[str] = field(default_factory=list)  # files > content budget

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        return cls(
            index=int(d["index"]), files=list(d.get("files", [])),
            dirs=list(d.get("dirs", [])), label=d.get("label", ""),
            est_tokens=int(d.get("est_tokens", 0)), loc=int(d.get("loc", 0)),
            symbols=list(d.get("symbols", [])),
            # back-compat: cached chunks.json predating the field still loads
            oversized=list(d.get("oversized", [])),
        )


# --- effective window / footprint -------------------------------------------

def base_ctx(role_cfg) -> int:
    """The window a SINGLE pass actually runs at (loop.py sends role.num_ctx). The
    deep TRIGGER keys on this — does the repo fit one single-pass window?"""
    return getattr(role_cfg, "num_ctx", 8192)


def deep_window(role_cfg) -> int:
    """The window the deep CHUNK passes run at: the BASE num_ctx, a deliberate
    choice — small, focused chunks that the model can actually conclude,
    rather than the huge-but-unconcludable chunks an expanded window produced.
    (A `base × multiple` clamped to num_ctx_max used to live here; the
    multiple was 1, so the clamp never did anything.)"""
    return base_ctx(role_cfg)


def estimate_repo_tokens(summary) -> int:
    """Cheap repo token footprint from the deterministic summary (LOC-based)."""
    # ~ chars per LOC ≈ 40 → tokens per LOC ≈ 10; matches bytes//4 closely enough
    # for a gate. Use total_loc so we don't re-walk the tree.
    return int(summary.total_loc * 10)


def should_use_deep(summary, role_cfg, *, override: bool | None = None) -> bool:
    """Decide single-pass vs deep. `override` (from --deep/--no-deep) wins.
    Otherwise: deep when the estimated repo token footprint crosses
    `_DEEP_TRIGGER_FRAC` of the SINGLE-PASS window (`base_ctx`) — i.e. the repo
    won't fit one single-pass run. File/symbol counts are NOT in the gate; token
    footprint is the predictive signal (file count failed at 66 on flying-fair)."""
    if override is not None:
        return override
    base = base_ctx(role_cfg)
    if base <= 0:
        return False
    return estimate_repo_tokens(summary) >= _DEEP_TRIGGER_FRAC * base


# --- file enumeration + chunking --------------------------------------------

def _norm_recent(summary) -> set[str]:
    return {p.replace(os.sep, "/") for p in (summary.recent_files or [])}


def _file_priority(rel: str, recent: set[str]) -> int:
    low = rel.lower()
    stem = Path(rel).stem.lower()
    if stem in _ENTRY_STEMS or any(s in low for s in _PRIORITY_SUBSTRINGS):
        return 0
    if rel in recent:
        return 1
    return 2


def _is_visible(rel: str, excludes=None) -> bool:
    """`iter_pruned`'s pruning applied to a repo-relative path from git: no
    excluded directory and no dot-directory (except .github) on the way down.
    Lets git-sourced path lists (incremental adds, framing) see exactly the
    files a tree walk would have."""
    excludes = excludes if excludes is not None else _DEFAULT_EXCLUDES
    for seg in rel.split("/")[:-1]:
        if seg in excludes or (seg.startswith(".") and seg != ".github"):
            return False
    return True


def file_recs_for(target: str | Path, rels, *, recent: set[str] | None = None,
                  prune: bool = True, log=None) -> list[FileRec]:
    """FileRecs for an explicit list of repo-relative paths (recognized
    languages that exist as files) — the incremental path's added files and
    the diff audit's changed files. Builds only what it is asked for; the
    whole-tree walk (`enumerate_files`) is for a full (re)map."""
    root = Path(target).resolve()
    recent = recent or set()
    recs: list[FileRec] = []
    for rel in rels:
        if prune and not _is_visible(rel):
            continue
        p = root / rel
        lang = _detect_language(p.suffix)
        if lang is None or not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError as e:
            if log:
                log(f"skipping unreadable file {p}: {e}")
            continue
        top = rel.split("/", 1)[0] if "/" in rel else "."
        recs.append(FileRec(
            rel=rel, language=lang, loc=_count_lines(p), bytes=size,
            tokens=max(1, size // _CHARS_PER_TOKEN), top_dir=top,
            priority=_file_priority(rel, recent),
        ))
    return recs


def enumerate_files(target: str | Path, summary, *,
                    excludes: set[str] | None = None, log=None) -> list[FileRec]:
    """Walk `target` for recognized source files with cheap token estimates and a
    priority bucket (entry/security → recent → normal). Deterministic order is
    applied by `build_chunks`. `log` (optional callable) surfaces skipped
    unreadable files — a silently skipped file is a silent coverage gap."""
    root = Path(target).resolve()
    excludes = excludes if excludes is not None else _DEFAULT_EXCLUDES
    recent = _norm_recent(summary)
    recs: list[FileRec] = []
    for p in iter_pruned(root, excludes=excludes):
        lang = _detect_language(p.suffix)
        if lang is None:
            continue
        try:
            size = p.stat().st_size
        except OSError as e:
            if log:
                log(f"skipping unreadable file {p}: {e}")
            continue
        rel = str(p.relative_to(root)).replace(os.sep, "/")
        top = rel.split("/", 1)[0] if "/" in rel else "."
        recs.append(FileRec(
            rel=rel, language=lang, loc=_count_lines(p), bytes=size,
            tokens=max(1, size // _CHARS_PER_TOKEN), top_dir=top,
            priority=_file_priority(rel, recent),
        ))
    return recs


def _symbols_by_path(symbol_index) -> dict[str, list[str]]:
    """One pass over the symbol index → {posix path: [names in index order]}.
    Built once per partition; scanning the whole index once PER CHUNK was
    O(chunks × symbols)."""
    by_path: dict[str, list[str]] = {}
    for s in getattr(symbol_index, "symbols", []) if symbol_index is not None else []:
        by_path.setdefault(str(s.path).replace(os.sep, "/"), []).append(s.name)
    return by_path


def _symbols_for(files, symbol_index, *,
                 by_path: dict[str, list[str]] | None = None) -> list[str]:
    """Symbols defined in `files` (first-seen, deduped, capped), in the
    index's order."""
    if symbol_index is None and by_path is None:
        return []
    if by_path is None:
        by_path = _symbols_by_path(symbol_index)
    files = set(files)
    names: list[str] = []
    seen: set[str] = set()
    # index order is preserved by walking the paths in their first-seen order
    for path, pnames in by_path.items():
        if path not in files:
            continue
        for name in pnames:
            if name not in seen:
                seen.add(name)
                names.append(name)
                if len(names) >= _MAX_CHUNK_SYMBOLS:
                    return names
    return names


def build_chunks(files: list[FileRec], *, content_budget: int,
                 symbol_index=None) -> list[Chunk]:
    """Greedy, deterministic, token-budgeted partition. Files are ordered
    (priority, top_dir, path) so same-directory files stay adjacent and
    entry/security dirs come first; packing keeps each chunk's content under
    `content_budget`. A single oversized file gets its own chunk (its content is
    capped at read time). Always returns ≥ 1 chunk."""
    budget = max(1, content_budget)
    ordered = sorted(files, key=lambda f: (f.priority, f.top_dir, f.rel))
    by_path = _symbols_by_path(symbol_index) if symbol_index is not None else None
    chunks: list[Chunk] = []
    cur: list[FileRec] = []
    cur_tok = 0

    def _flush() -> None:
        nonlocal cur, cur_tok
        if not cur:
            return
        idx = len(chunks)
        fileset = {f.rel for f in cur}
        dir_counts: dict[str, int] = {}
        for f in cur:
            dir_counts[f.top_dir] = dir_counts.get(f.top_dir, 0) + 1
        label = max(sorted(dir_counts), key=lambda d: dir_counts[d])
        chunks.append(Chunk(
            index=idx, files=[f.rel for f in cur],
            dirs=sorted(dir_counts), label=label,
            est_tokens=sum(f.tokens for f in cur), loc=sum(f.loc for f in cur),
            symbols=_symbols_for(fileset, symbol_index, by_path=by_path),
            oversized=[f.rel for f in cur if f.tokens > budget],
        ))
        cur, cur_tok = [], 0

    for f in ordered:
        ftok = min(f.tokens, budget)
        if cur and cur_tok + ftok > budget:
            _flush()
        cur.append(f)
        cur_tok += ftok
    _flush()
    if not chunks:  # empty repo → one empty chunk so callers stay uniform
        chunks.append(Chunk(index=0, label="."))
    return chunks


_FRAMING_LIMIT = 40


def _pick_framing(rels, *, limit: int = _FRAMING_LIMIT) -> list[str]:
    """The framing selection rule over a path list (sorted, capped)."""
    return sorted(r for r in rels if _FRAMING_RE.search(r))[:limit]


def framing_files(target: str | Path, *, limit: int = _FRAMING_LIMIT) -> list[str]:
    """Deterministic framing-file picker for the survey (README/CI/Docker/IaC/
    auth/config/routing/entrypoints). Returns POSIX relative paths, capped."""
    root = Path(target).resolve()
    return _pick_framing(
        (str(p.relative_to(root)).replace(os.sep, "/")
         for p in iter_pruned(root, excludes=_DEFAULT_EXCLUDES)), limit=limit)


# --- estimate ---------------------------------------------------------------

@dataclass
class DeepEstimate:
    chunks: int
    passes: int          # survey + chunks + synthesis
    seconds: int
    minutes: int
    large: bool

    def line(self) -> str:
        return (f"{self.chunks} chunks · {self.passes} passes · "
                f"~{self.minutes} min (rough)")


def estimate_run(n_chunks: int, *, survey_cached: bool = False) -> DeepEstimate:
    """Wall estimate from the per-stage constants. `survey_cached=True` (a FRESH map
    is reused → the survey pass is skipped) drops the survey term. Guards
    `n_chunks <= 0` against a deceptive survey+synth-only floor when there is no
    work to do."""
    n_chunks = max(0, n_chunks)
    passes = n_chunks + (1 if not survey_cached else 0) + 1  # survey? + chunks + synth
    if n_chunks == 0:
        secs = _SYNTH_S  # nothing to chunk → a single light pass, no false floor
    else:
        secs = (0 if survey_cached else _SURVEY_S) + n_chunks * _CHUNK_S + _SYNTH_S
    return DeepEstimate(
        chunks=n_chunks, passes=passes, seconds=secs,
        minutes=max(1, round(secs / 60)),
        large=n_chunks >= _LARGE_CONFIRM_CHUNKS,
    )
