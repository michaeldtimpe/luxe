"""gitkit DEEP MODE — staged map-reduce analysis for large repos.

The single-pass runner (`runner.run_git_report`) does ONE `run_single` pass over
a whole repo. That packaging cannot scale: on large repos the model enters a
repetition loop, blows the report token budget, and truncates mid-report even
though it found real issues. Deep mode fixes the *packaging* by staging the work
as multiple sequential read-only `run_single` passes orchestrated here in Python
(the `compare/run_pair.py` precedent — NOT the retired in-agent swarm/phased
modes; each pass is still one mono call with no in-agent repair loop):

  Stage 0  Survey   — deterministic repo map + one LLM pass → architectural
                      hypothesis (cached per repo under `map/`).
  Stage 1  Plan     — deterministic, token-budgeted chunker (cached under `map/`)
                      + an estimate and a large-repo confirmation gate.
  Stage 2  Analyze  — one pass per chunk → COMPACT structured notes + a running
                      structured cross-reference digest (hard-ceiling compacted).
  Stage 3  Synthesis— one pass over the AGGREGATE notes (NOT raw files) → the
                      consolidated report in the required gitkit shape, merging
                      duplicates and re-rating severity globally. A 2-level
                      reduce fallback triggers proactively if notes overflow.

Per-repo persistence (the user's "each mapped repo gets its own folder"): the
survey map + chunk plan live under `~/.luxe/reports/<repo_hash>/map/`, keyed by
HEAD, so a large repo is surveyed/chunked ONCE and reused across kinds/re-runs.
Per-run notes + the final digest live in a sibling `<kind>-<ts>-<rand>.work/`.

All directive strings live in `agents/prompts.py` (gitkit.sdd Forbids inline
prompts). This module owns only orchestration + deterministic data shaping.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from luxe.agents import prompts
from luxe.context import estimate_tokens
from luxe.repo_index import (
    _DEFAULT_EXCLUDES,
    _count_lines,
    _detect_language,
)

# --- tuning constants (per-stage wall fit from the 46-repo sweep) ------------

# Fraction of the effective context window reserved for file *content* the agent
# reads per chunk (leaves headroom for the prompt + injected map/digest + the
# report output + tool round-trips).
_CONTENT_BUDGET_FRAC = 0.55
# Repo footprint above this fraction of the window → deep mode (primary trigger).
# File count is NOT predictive (flying-fair failed at 66 files); token footprint
# is. Secondary signals stay out of the gate by design.
_DEEP_TRIGGER_FRAC = 0.55
# Hard ceiling for the running cross-reference digest, as a fraction of window.
_DIGEST_CEILING_FRAC = 0.15
# Synthesize-in-two-levels when the aggregate notes exceed this fraction.
_SYNTH_REDUCE_FRAC = 0.80
# Deep passes deliberately run at the BASE num_ctx, NOT the expanded num_ctx_max.
# The first aurora run expanded to 131072 and it backfired: chunks became huge
# (30-47 files, 600k-1.4M prompt tokens), passes were slow, and the model
# rambled without ever concluding (truncating before its findings). Smaller
# base-window chunks keep each pass focused enough that the model concludes —
# the "eaten in stages" intent. This multiple lets a capable box opt into a
# modestly larger deep window than base without returning to the 131k failure.
_DEEP_WINDOW_BASE_MULT = 1
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
_MAX_EVIDENCE_PER_FINDING = 6
# Generation headroom. The champion writes its whole analysis as prose in the
# final message, so a chunk pass fills whatever cap it is given; _CHUNK_MAX_TOKENS
# bounds that ramble (the extract pass recovers findings from it). _DEEP_MAX_TOKENS
# gives the synthesis report room beyond the single-pass GITKIT_MAX_TOKENS.
_CHUNK_MAX_TOKENS = 16384
_DEEP_MAX_TOKENS = 24576

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

_SEVERITY_RANK = {
    "critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0, "": 0,
}


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


def empty_digest() -> dict:
    return {"modules": [], "entities": [], "cross_cutting": [],
            "provisional_findings": [], "markdown_notes": [], "unparsed_chunks": [],
            "steps": []}


# --- map cache health -------------------------------------------------------

class MapState(str, Enum):
    """Health of a per-repo cached map. The whole point of the breadcrumb is to
    separate the two questions the old `load_map` conflated:
    *was this repo ever mapped?* vs *is the map currently usable?*"""
    FRESH = "FRESH"       # full valid cache, HEAD matches → reuse silently
    MISSING = "MISSING"   # never mapped (no breadcrumb) → re-survey, no warning
    STALE = "STALE"       # breadcrumb present, HEAD moved → re-survey
    PARTIAL = "PARTIAL"   # breadcrumb present + HEAD matches, but heavy files
                          # missing/corrupt → "damaged", surface it (don't silently
                          # equate with "never mapped")


@dataclass
class MapStatus:
    state: MapState
    head: str = ""              # breadcrumb's recorded HEAD (for STALE/PARTIAL)
    n_chunks: int = 0
    content_budget: int = 0
    mapped_at: int = 0          # int(time.time()) from the breadcrumb
    missing: list[str] = field(default_factory=list)   # heavy files gone/corrupt
    version: int = 1            # breadcrumb schema (v2 = incremental-capable)
    files: dict = field(default_factory=dict)          # v2: {rel: blob_sha}
    baseline: dict = field(default_factory=dict)       # v2: partition baseline


class CacheDecision(Enum):
    """Outcome of the partial-map prompt — an explicit sentinel (readable months
    later, unlike a naked object())."""
    REBUILD = "REBUILD"
    CANCEL = "CANCEL"


# --- effective window / footprint -------------------------------------------

def base_ctx(role_cfg) -> int:
    """The window a SINGLE pass actually runs at (loop.py sends role.num_ctx). The
    deep TRIGGER keys on this — does the repo fit one single-pass window?"""
    return getattr(role_cfg, "num_ctx", 8192)


def deep_window(role_cfg) -> int:
    """The window the deep CHUNK passes run at. Defaults to the BASE num_ctx (a
    deliberate choice — see _DEEP_WINDOW_BASE_MULT): small, focused chunks that
    the model can actually conclude, rather than the huge-but-unconcludable
    chunks an expanded window produced. Clamped to num_ctx_max when a >1
    multiple opts into a larger deep window."""
    base = base_ctx(role_cfg)
    ceiling = getattr(role_cfg, "num_ctx_max", 0) or base
    return min(base * _DEEP_WINDOW_BASE_MULT, ceiling) if ceiling >= base else base


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
    for cur, dirs, fnames in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in excludes and not d.startswith(".") or d == ".github"]
        for fname in fnames:
            p = Path(cur) / fname
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


def _symbols_for(files: set[str], symbol_index) -> list[str]:
    if symbol_index is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for s in getattr(symbol_index, "symbols", []):
        spath = str(s.path).replace(os.sep, "/")
        if spath in files and s.name not in seen:
            seen.add(s.name)
            names.append(s.name)
            if len(names) >= _MAX_CHUNK_SYMBOLS:
                break
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
            symbols=_symbols_for(fileset, symbol_index),
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


def framing_files(target: str | Path, *, limit: int = 40) -> list[str]:
    """Deterministic framing-file picker for the survey (README/CI/Docker/IaC/
    auth/config/routing/entrypoints). Returns POSIX relative paths, capped."""
    root = Path(target).resolve()
    found: list[str] = []
    for cur, dirs, fnames in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in _DEFAULT_EXCLUDES and not d.startswith(".")
                   or d == ".github"]
        for fname in fnames:
            rel = str((Path(cur) / fname).relative_to(root)).replace(os.sep, "/")
            if _FRAMING_RE.search(rel):
                found.append(rel)
    found.sort()
    return found[:limit]


# --- chunk-output parsing + digest maintenance ------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_DEEP_KEYS = ("findings", "modules", "entities", "cross_cutting", "steps")


def parse_chunk_notes(text: str) -> dict | None:
    """Parse a chunk pass's JSON output leniently and robustly. The champion
    often wraps the JSON in prose (or emits more than one block), so we collect
    every candidate — all fenced ```json blocks plus the outer `{...}` span — try
    to parse each, and return the best dict (one carrying a recognized deep key,
    preferring the one with the most findings). Returns None if nothing parses."""
    if not text:
        return None
    candidates: list[str] = [m.group(1) for m in _JSON_FENCE_RE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    best: dict | None = None
    best_score = -1
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if not any(k in obj for k in _DEEP_KEYS):
            continue
        score = len(obj.get("findings") or []) * 100 + len(obj)
        if score > best_score:
            best, best_score = obj, score
    return best


def _has_report_header(text: str, kind: str) -> bool:
    """True if the chunk output concluded with the kind's required markdown report
    header (so its findings can be recovered by slicing even without JSON)."""
    from luxe.gitkit.runner import _TITLES
    title = _TITLES.get(kind, "")
    if not title or not text:
        return False
    return re.search(rf"^#\s+{re.escape(title)}\s*$", text,
                     re.MULTILINE | re.IGNORECASE) is not None


def _finding_key(f: dict) -> str:
    """Dedup key: prefer root_cause, fall back to title; normalized."""
    base = (f.get("root_cause") or f.get("title") or "").strip().lower()
    return re.sub(r"\s+", " ", base)


def _merge_evidence(a: list, b: list) -> list:
    out: list[str] = []
    for ev in list(a) + list(b):
        ev = str(ev).strip()
        if ev and ev not in out:
            out.append(ev)
        if len(out) >= _MAX_EVIDENCE_PER_FINDING:
            break
    return out


def update_digest(digest: dict, parsed: dict, chunk_index: int) -> dict:
    """Fold one chunk's parsed notes into the running digest (in place)."""
    def _dedupe_extend(key: str, items, name_field: str) -> None:
        existing = {str(e.get(name_field, "")).strip().lower()
                    for e in digest[key] if isinstance(e, dict)}
        for it in items or []:
            if not isinstance(it, dict):
                continue
            nm = str(it.get(name_field, "")).strip().lower()
            if nm and nm not in existing:
                existing.add(nm)
                digest[key].append(it)

    _dedupe_extend("modules", parsed.get("modules"), "name")
    _dedupe_extend("entities", parsed.get("entities"), "name")
    for cc in parsed.get("cross_cutting") or []:
        cc = str(cc).strip()
        if cc and cc not in digest["cross_cutting"]:
            digest["cross_cutting"].append(cc)
    for f in parsed.get("findings") or []:
        if not isinstance(f, dict):
            continue
        rec = dict(f)
        rec["chunk"] = chunk_index
        rec.setdefault("chunks", [chunk_index])   # provenance: contributors
        rec.setdefault("source", "json")          # provenance: parse rung
        rec.setdefault("evidence", [])
        digest["provisional_findings"].append(rec)
    # gitchange: accumulate apply-ready steps (deduped by op + files + title). Empty
    # for gitaudit, so its digest stays byte-identical.
    seen = {(str(s.get("change", {}).get("op", "")),
             tuple(s.get("target_files", []) or []),
             str(s.get("title", "")).strip().lower()) for s in digest["steps"]}
    for s in parsed.get("steps") or []:
        if not isinstance(s, dict):
            continue
        key = (str(s.get("change", {}).get("op", "")),
               tuple(s.get("target_files", []) or []),
               str(s.get("title", "")).strip().lower())
        if key not in seen:
            seen.add(key)
            digest["steps"].append(s)
    return digest


# Provenance rungs, best → worst. Used to keep the BEST source on merge and to
# cap heuristic-salvaged findings at low confidence (provenance-honest).
_SOURCE_RANK = {"json": 3, "md_clean": 2, "md_transcribed": 1, "heuristic": 0}


def _evidence_keys(f: dict) -> set[str]:
    """Normalized `file:line` tokens from a finding's evidence strings
    ("a.py line 12" / "a.py:12" / "a.py 12" → "a.py:12")."""
    keys: set[str] = set()
    for ev in f.get("evidence", []) or []:
        for m in _FILE_LINE_RE.finditer(str(ev)):
            keys.add(re.sub(r"[:\s]+(?:line\s+)?", ":", m.group(0).lower()))
    return keys


def _finding_chunks(f: dict) -> list[int]:
    chunks = list(f.get("chunks") or [])
    if not chunks and "chunk" in f:
        chunks = [f["chunk"]]
    return sorted(set(chunks))


def confidence_of(f: dict) -> tuple[float, str]:
    """Deterministic, EVIDENCE-weighted confidence (never frequency-weighted:
    one hallucinated issue repeated by three chunks must not outscore one real
    issue found once with strong evidence). Weights: +0.5 ≥1 parseable
    file:line; +0.2 ≥2 distinct evidence locations; +0.2 structured/clean
    source (json/md_clean); +0.1 corroborated by ≥2 chunks. heuristic-salvaged
    findings cap at low regardless. Labels: ≥0.7 high, ≥0.4 medium, else low."""
    ev = _evidence_keys(f)
    score = 0.0
    if len(ev) >= 1:
        score += 0.5
    if len(ev) >= 2:
        score += 0.2
    if f.get("source") in ("json", "md_clean"):
        score += 0.2
    if len(_finding_chunks(f)) >= 2:
        score += 0.1
    label = "high" if score >= 0.7 else ("medium" if score >= 0.4 else "low")
    if f.get("source") == "heuristic":
        label = "low"
    return round(score, 2), label


def _merge_into(cur: dict, f: dict) -> None:
    """Merge finding `f` into `cur`: union evidence + chunks, max severity,
    best provenance source."""
    cur["evidence"] = _merge_evidence(cur.get("evidence", []),
                                      f.get("evidence", []))
    cur["chunks"] = sorted(set(_finding_chunks(cur)) | set(_finding_chunks(f)))
    if _SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 0) > \
       _SEVERITY_RANK.get(str(cur.get("severity", "")).lower(), 0):
        cur["severity"] = f.get("severity", cur.get("severity"))
    if _SOURCE_RANK.get(f.get("source", ""), -1) > \
       _SOURCE_RANK.get(cur.get("source", ""), -1):
        cur["source"] = f["source"]


def compact_digest(digest: dict, *, ceiling_tokens: int = 0,
                   log=None) -> dict:
    """Dedupe + merge the digest so it stays under `ceiling_tokens`. Two merge
    passes: (1) same root-cause/title key; (2) EVIDENCE-OVERLAP — findings
    sharing any normalized `file:line` evidence token merge (same bug, different
    words). Merges union evidence + contributing chunks, keep highest severity +
    best provenance source. If still over the ceiling, drops lowest-severity
    findings (logged). Stamps each surviving finding's deterministic
    `confidence`. Returns a new digest dict; never mutates the input."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for f in digest.get("provisional_findings", []):
        k = _finding_key(f)
        if not k:
            k = f"_anon_{len(order)}"
        if k in merged:
            _merge_into(merged[k], f)
        else:
            merged[k] = dict(f)
            merged[k]["evidence"] = _merge_evidence(f.get("evidence", []), [])
            order.append(k)

    # Pass 2 — evidence-overlap merge (catches same-bug-different-words misses
    # the root-cause key can't).
    by_ev: dict[str, str] = {}          # evidence token -> canonical key
    survivors: list[str] = []
    for k in order:
        f = merged[k]
        hit = next((by_ev[t] for t in _evidence_keys(f) if t in by_ev), None)
        if hit is not None and hit != k:
            _merge_into(merged[hit], f)
            for t in _evidence_keys(merged[hit]):
                by_ev.setdefault(t, hit)
            continue
        survivors.append(k)
        for t in _evidence_keys(f):
            by_ev.setdefault(t, k)

    findings = [merged[k] for k in survivors]
    for f in findings:
        f["confidence_score"], f["confidence"] = confidence_of(f)

    out = {
        "modules": list(digest.get("modules", [])),
        "entities": list(digest.get("entities", [])),
        "cross_cutting": list(digest.get("cross_cutting", [])),
        "provisional_findings": findings,
        "markdown_notes": list(digest.get("markdown_notes", [])),
        "unparsed_chunks": list(digest.get("unparsed_chunks", [])),
        "steps": list(digest.get("steps", [])),
    }

    if ceiling_tokens and estimate_tokens(json.dumps(out)) > ceiling_tokens:
        # Drop lowest-severity findings first until under ceiling.
        ranked = sorted(
            out["provisional_findings"],
            key=lambda f: _SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 0))
        dropped = 0
        while ranked and estimate_tokens(json.dumps(out)) > ceiling_tokens:
            victim = ranked.pop(0)
            out["provisional_findings"].remove(victim)
            dropped += 1
        if dropped and log:
            log(f"digest over budget — dropped {dropped} low-severity "
                f"provisional finding(s) to stay in window")
    return out


# --- gitchange: prose → steps JSON transcription ----------------------------

def _plan_extract_pass(text: str, *, pass_fn, role) -> str:
    """Run the gitchange transcription recovery pass: convert a prose/markdown change
    plan draft into the required gitplan/v1 JSON (directive GIT_CHANGE_EXTRACT_HINT).
    The champion converts its own draft far better than it emits JSON from scratch,
    so this rescues a chunk/synthesis pass that rambled instead of emitting steps.
    Returns the raw pass text (the caller parses it leniently). `pass_fn`/`role` are
    the run_deep_report `_pass` choke point so the recovery is timed like any stage."""
    ctx = f"<plan_draft>\n{text}\n</plan_draft>"
    goal = ("Convert the change plan draft into the required JSON.\n\n"
            + prompts.GIT_CHANGE_EXTRACT_HINT)
    res = pass_fn(goal, ctx, "plan-extract", role=role)
    return getattr(res, "final_text", "") or ""


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


@dataclass
class PassTiming:
    """One per-pass wall-clock record. Captured at the `_pass` choke point so every
    stage (survey / chunk-N / synthesis / format / reduce-N) is measured uniformly.
    This is the raw calibration dataset behind a future window/size-aware estimator
    that would replace the flat `_SECONDS_PER_CHUNK` constant. `started_at` (epoch
    seconds) preserves the timeline so later analysis can spot overnight pauses /
    machine sleep / model reloads. Chunk-only fields are enriched at the call site."""
    label: str
    kind: str
    wall_s: float
    completion_tokens: int
    steps: int
    tool_calls_total: int
    window: int
    started_at: int = 0
    est_tokens: int = 0     # chunk passes only
    loc: int = 0            # chunk passes only
    n_files: int = 0        # chunk passes only

    def to_dict(self) -> dict:
        return asdict(self)


# --- map cache (per-repo, HEAD-keyed) ---------------------------------------

def _atomic_write_text(path: Path, text: str) -> None:
    """Same-directory tmp + os.replace: a crash mid-write never leaves a torn
    file — readers see the old content or the new content, nothing between."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _map_dir(target: str | Path):
    from luxe.gitkit import store
    return store.reports_dir(target) / "map"


def _age_str(mapped_at: int) -> str:
    """Human age of a breadcrumb timestamp ('3h', '2d', '5m', 'just now')."""
    if not mapped_at:
        return "unknown time"
    secs = max(0, int(time.time()) - int(mapped_at))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit}"
    return "just now"


def map_status(target: str | Path, *, head: str) -> MapStatus:
    """Classify the cached map's health (FRESH / MISSING / STALE / PARTIAL).

    Keyed on the durable `mapped.json` breadcrumb so a missing/corrupt HEAVY file
    (survey_notes.md / chunks.json) is recognized as a DAMAGED map rather than
    silently treated as "never mapped" (which would re-survey with no warning).
    Pre-breadcrumb caches (no `mapped.json`) classify as MISSING → a harmless
    re-survey on the first run after upgrade."""
    d = _map_dir(target)
    bc = d / "mapped.json"
    if not bc.is_file():
        return MapStatus(MapState.MISSING)
    try:
        meta = json.loads(bc.read_text())
    except (ValueError, OSError):
        # A corrupt breadcrumb is still EVIDENCE a map existed → "damaged".
        return MapStatus(MapState.PARTIAL, missing=["mapped.json (corrupt)"])

    b_head = str(meta.get("head", "") or "")
    n_chunks = int(meta.get("n_chunks", 0) or 0)
    budget = int(meta.get("content_budget", 0) or 0)
    mapped_at = int(meta.get("mapped_at", 0) or 0)
    common = dict(head=b_head, n_chunks=n_chunks,
                  content_budget=budget, mapped_at=mapped_at,
                  version=int(meta.get("version", 1) or 1),
                  files=dict(meta.get("files", {}) or {}),
                  baseline=dict(meta.get("baseline", {}) or {}))

    if head and b_head != head:
        return MapStatus(MapState.STALE, **common)

    missing: list[str] = []
    head_f, chunks_f, notes_f = d / "head", d / "chunks.json", d / "survey_notes.md"
    if not notes_f.is_file() or not notes_f.read_text().strip():
        missing.append("survey_notes.md")
    if not chunks_f.is_file():
        missing.append("chunks.json")
    else:
        try:
            json.loads(chunks_f.read_text())
        except (ValueError, OSError):
            missing.append("chunks.json (corrupt)")
    if not head_f.is_file():
        missing.append("head")
    elif head and head_f.read_text().strip() != head:
        missing.append("head (content mismatch)")

    if missing:
        return MapStatus(MapState.PARTIAL, missing=missing, **common)
    return MapStatus(MapState.FRESH, **common)


def load_map(target: str | Path, *, head: str, rebuild: bool = False,
             allow_stale: bool = False) -> dict | None:
    """Return the cached survey+chunks for `target` iff the map is FRESH (delegates
    to `map_status` — single source of truth for "valid cache"); else None. The
    return shape (survey_notes / chunks / content_budget / framing) is unchanged.
    `allow_stale=True` also accepts a STALE map (HEAD moved) — the incremental
    re-audit path, which keeps the survey + partition and re-runs only dirty
    chunks."""
    if rebuild:
        return None
    state = map_status(target, head=head).state
    ok = state is MapState.FRESH or (allow_stale and state is MapState.STALE)
    if not ok:
        return None
    d = _map_dir(target)
    chunks_f, notes_f = d / "chunks.json", d / "survey_notes.md"
    try:  # defensive: absorb a TOCTOU delete between the status check and the read
        chunks_blob = json.loads(chunks_f.read_text())
        return {
            "survey_notes": notes_f.read_text(),
            "chunks": [Chunk.from_dict(c) for c in chunks_blob.get("chunks", [])],
            "content_budget": int(chunks_blob.get("content_budget", 0)),
            "framing": chunks_blob.get("framing", []),
        }
    except (ValueError, OSError):
        return None


def git_file_shas(target: str | Path) -> dict[str, str]:
    """{rel path: blob sha} for every tracked file at HEAD (`git ls-tree -r`).
    SHAs, never timestamps — the incremental staleness currency. Untracked
    files simply don't appear (callers treat sha-less files as always-dirty)."""
    from luxe.gitkit.health import _run_git
    ok, out = _run_git(
        ["ls-tree", "-r", "--format=%(objectname) %(path)", "HEAD"], target)
    if not ok:
        return {}
    shas: dict[str, str] = {}
    for ln in out.splitlines():
        parts = ln.split(" ", 1)
        # the committable .luxe/ sidecar (gitkit mirror, memory.md) is luxe's
        # OWN write — it must never count as a repo change (a committed mirror
        # README would otherwise read as a "framing file changed" rebuild).
        if len(parts) == 2 and not parts[1].startswith(".luxe/"):
            shas[parts[1]] = parts[0]
    return shas


def make_baseline(chunks: list[Chunk]) -> dict:
    """Partition baseline persisted in the v2 breadcrumb so the anti-drift
    compaction triggers are computable across incremental generations."""
    return {"orig_n_chunks": len(chunks),
            "orig_corpus_tokens": sum(c.est_tokens for c in chunks),
            "delta_chunks": 0, "delta_tokens": 0}


def save_map(target: str | Path, *, head: str, survey_notes: str,
             chunks: list[Chunk], content_budget: int,
             framing: list[str], summary_render: str,
             files: dict[str, str] | None = None,
             baseline: dict | None = None) -> Path:
    d = _map_dir(target)
    d.mkdir(parents=True, exist_ok=True)
    # Atomic per-file writes, breadcrumb LAST: a crash anywhere before the
    # mapped.json replace leaves the OLD breadcrumb pointing at the OLD heavy
    # files (a consistent FRESH/STALE map), never a half-new state.
    _atomic_write_text(d / "head", (head or "") + "\n")
    _atomic_write_text(d / "survey_notes.md", survey_notes.rstrip() + "\n")
    _atomic_write_text(d / "survey.json", json.dumps(
        {"head": head, "summary_render": summary_render, "framing": framing},
        indent=2))
    _atomic_write_text(d / "chunks.json", json.dumps(
        {"content_budget": content_budget, "framing": framing,
         "chunks": [c.to_dict() for c in chunks]}, indent=2))
    # Durable breadcrumb (tiny — survives deletion of the heavy files).
    # v2: per-file blob shas + the partition baseline make the incremental
    # re-audit's staleness rules and compaction triggers computable.
    _atomic_write_text(d / "mapped.json", json.dumps(
        {"version": 2, "head": head or "", "n_chunks": len(chunks),
         "content_budget": content_budget, "mapped_at": int(time.time()),
         "files": files if files is not None else git_file_shas(target),
         "baseline": baseline if baseline is not None else make_baseline(chunks)},
        indent=2))
    return d


# --- incremental re-audit (cache v2) -----------------------------------------

# Anti-drift compaction triggers: appended delta chunks degrade the partition
# over successive incremental runs; force a full rebuild when any fires.
_MAX_DELTA_CHUNKS = 4
_MAX_DELTA_TOKENS_FRAC = 0.15
_MAX_CHUNK_GROWTH_FRAC = 0.25
# >this fraction of mapped files added+deleted+renamed → full rebuild.
_MAX_FILE_CHURN_FRAC = 0.20


def _notes_dir(target: str | Path, kind: str) -> Path:
    return _map_dir(target) / "notes" / kind


def save_chunk_note(target: str | Path, kind: str, chunk: Chunk, *,
                    head: str, file_shas: dict[str, str],
                    contribution: dict, wall_s: float = 0.0) -> None:
    """Persist one chunk's digest CONTRIBUTION (inputs to a future digest fold —
    never merged final findings) right after the chunk completes. Atomic, so it
    doubles as crash-resume. Best-effort: an OS error never aborts the run."""
    try:
        d = _notes_dir(target, kind)
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(d / f"chunk-{chunk.index:02d}.json", json.dumps(
            {"head": head, "files": list(chunk.files),
             "file_shas": file_shas, "contribution": contribution,
             "wall_s": wall_s}, indent=1))
    except OSError:
        pass


def load_chunk_note(target: str | Path, kind: str, index: int) -> dict | None:
    p = _notes_dir(target, kind) / f"chunk-{index:02d}.json"
    if not p.is_file():
        return None
    try:
        note = json.loads(p.read_text())
    except (ValueError, OSError):
        return None
    return note if isinstance(note, dict) else None


def chunk_note_is_valid(note: dict | None, chunk: Chunk,
                        current_shas: dict[str, str]) -> bool:
    """A cached contribution is reusable iff it covers EXACTLY this chunk's
    files and every file's recorded blob sha matches the CURRENT tree
    (belt-and-braces — never trust the breadcrumb alone). A file without a
    current sha (untracked/missing) is always dirty."""
    if not note or not isinstance(note.get("contribution"), dict):
        return False
    if list(note.get("files", [])) != list(chunk.files):
        return False
    shas = note.get("file_shas", {})
    for rel in chunk.files:
        cur = current_shas.get(rel, "")
        if not cur or shas.get(rel, "") != cur:
            return False
    return True


def fold_contribution(digest: dict, contribution: dict, chunk_index: int) -> None:
    """Fold a cached chunk contribution into the digest EXACTLY as the live
    chunk loop would have (same update_digest / markdown_notes / unparsed
    paths) — the digest is always rebuilt from scratch, never merged from a
    cached final state."""
    parsed = contribution.get("parsed")
    if isinstance(parsed, dict):
        update_digest(digest, parsed, chunk_index)
    note = contribution.get("note")
    if isinstance(note, dict) and note.get("md"):
        digest["markdown_notes"].append(
            {"chunk": chunk_index, "label": note.get("label", ""),
             "md": note["md"], "source": note.get("source", "md_clean")})
    unparsed = contribution.get("unparsed")
    if unparsed:
        digest["unparsed_chunks"].append(str(unparsed))


@dataclass
class IncrementalPlan:
    mode: str                   # "incremental" | "rebuild"
    reason: str = ""            # rebuild trigger / incremental summary
    chunks: list[Chunk] = field(default_factory=list)  # pruned + delta partition
    dirty: set[int] = field(default_factory=set)       # indices that must re-run
    baseline: dict = field(default_factory=dict)       # carried-forward baseline
    n_changed: int = 0          # modified+deleted+added file count


def plan_incremental(*, old_files: dict[str, str], new_files: dict[str, str],
                     chunks: list[Chunk], baseline: dict,
                     added_recs: list[FileRec], content_budget: int,
                     symbol_index=None) -> IncrementalPlan:
    """PURE incremental planner (no I/O): decide full-rebuild vs incremental
    from the old/new blob-sha maps, and produce the updated partition.

    Rebuild triggers (each logged via `reason`): a FRAMING file changed; file
    churn (added+deleted) > 20% of mapped files; anti-drift compaction —
    cumulative delta chunks > 4, cumulative delta content > 15% of the original
    corpus tokens, or chunk count grown > 25% over the original partition.

    Incremental: survey + partition kept; deleted files pruned from their
    chunks (chunk → dirty); modified files mark their chunk dirty; added files
    pack into APPENDED delta chunks (all dirty)."""
    modified = {r for r, s in new_files.items()
                if r in old_files and old_files[r] != s}
    deleted = set(old_files) - set(new_files)
    added = set(new_files) - set(old_files)

    touched = modified | deleted | added
    framing_hit = sorted(r for r in touched if _FRAMING_RE.search(r))
    if framing_hit:
        return IncrementalPlan("rebuild",
                               reason=f"framing file changed ({framing_hit[0]})")
    if old_files and (len(added) + len(deleted)) > _MAX_FILE_CHURN_FRAC * len(old_files):
        return IncrementalPlan(
            "rebuild", reason=f"file churn {len(added) + len(deleted)}/"
            f"{len(old_files)} mapped files (> {_MAX_FILE_CHURN_FRAC:.0%})")

    # Updated partition: prune deletions, mark dirt, append delta chunks.
    new_chunks: list[Chunk] = []
    dirty: set[int] = set()
    mapped_files: set[str] = set()
    for c in chunks:
        keep = [f for f in c.files if f not in deleted]
        mapped_files.update(keep)
        nc = Chunk(index=c.index, files=keep, dirs=c.dirs, label=c.label,
                   est_tokens=c.est_tokens, loc=c.loc, symbols=c.symbols,
                   oversized=[f for f in c.oversized if f not in deleted])
        new_chunks.append(nc)
        if len(keep) != len(c.files) or any(f in modified for f in keep):
            dirty.add(c.index)

    # files added since the ORIGINAL map but unmapped (e.g. created between
    # generations and never folded) ride with the added set via added_recs.
    delta_recs = [r for r in added_recs if r.rel not in mapped_files]
    delta_tokens_new = sum(r.tokens for r in delta_recs)
    delta_chunks_new: list[Chunk] = []
    if delta_recs:
        delta_chunks_new = build_chunks(delta_recs, content_budget=content_budget,
                                        symbol_index=symbol_index)
        offset = max((c.index for c in new_chunks), default=-1) + 1
        for dc in delta_chunks_new:
            dc.index += offset
            new_chunks.append(dc)
            dirty.add(dc.index)

    # Anti-drift compaction triggers — evaluated on the would-be cumulative state.
    bl = dict(baseline or {})
    orig_n = int(bl.get("orig_n_chunks", 0) or len(chunks))
    orig_tok = int(bl.get("orig_corpus_tokens", 0)
                   or sum(c.est_tokens for c in chunks))
    cum_delta_chunks = int(bl.get("delta_chunks", 0)) + len(delta_chunks_new)
    cum_delta_tokens = int(bl.get("delta_tokens", 0)) + delta_tokens_new
    if cum_delta_chunks > _MAX_DELTA_CHUNKS:
        return IncrementalPlan("rebuild",
                               reason=f"compaction: {cum_delta_chunks} delta "
                               f"chunks (> {_MAX_DELTA_CHUNKS})")
    if orig_tok and cum_delta_tokens > _MAX_DELTA_TOKENS_FRAC * orig_tok:
        return IncrementalPlan(
            "rebuild", reason=f"compaction: delta content {cum_delta_tokens} tok "
            f"(> {_MAX_DELTA_TOKENS_FRAC:.0%} of {orig_tok})")
    if orig_n and len(new_chunks) > (1 + _MAX_CHUNK_GROWTH_FRAC) * orig_n:
        return IncrementalPlan(
            "rebuild", reason=f"compaction: partition grew to {len(new_chunks)} "
            f"chunks (> {_MAX_CHUNK_GROWTH_FRAC:.0%} over {orig_n})")

    bl.update({"orig_n_chunks": orig_n, "orig_corpus_tokens": orig_tok,
               "delta_chunks": cum_delta_chunks,
               "delta_tokens": cum_delta_tokens})
    return IncrementalPlan(
        "incremental",
        reason=f"{len(modified)} modified, {len(deleted)} deleted, "
               f"{len(delta_recs)} added",
        chunks=new_chunks, dirty=dirty, baseline=bl,
        n_changed=len(modified) + len(deleted) + len(delta_recs))


def _new_work_dir(target: str | Path, kind: str) -> Path:
    from luxe.gitkit import store
    ts = int(time.time())
    d = store.reports_dir(target) / f"{kind}-{ts}-{uuid.uuid4().hex[:6]}.work"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- extra_context block builders (pure data, no instructions) --------------

def _framing_block(framing: list[str]) -> str:
    body = "\n".join(f"- {p}" for p in framing) or "(none detected)"
    return f"<framing_files>\nRead these first to form the map:\n{body}\n</framing_files>"


def _chunk_block(chunk: Chunk, total: int) -> str:
    files = "\n".join(f"- {p}" for p in chunk.files) or "(none)"
    syms = ", ".join(chunk.symbols) if chunk.symbols else "(none indexed)"
    over = ""
    if chunk.oversized:
        over = ("\n\nOversized files (larger than the content budget — they "
                "truncate when read whole; read them in sections):\n"
                + "\n".join(f"- {p}" for p in chunk.oversized))
    return (f"<chunk_files>\nChunk {chunk.index + 1}/{total} — focus area "
            f"`{chunk.label}` ({len(chunk.files)} files, ~{chunk.loc} LOC). "
            f"Analyze ONLY these files (read them with your tools):\n{files}\n\n"
            f"Symbols defined in these files: {syms}{over}\n</chunk_files>")


def _digest_block(digest: dict) -> str:
    return ("<cross_reference_digest>\nRunning map from earlier chunks (use it to "
            "cross-reference; do not re-report its findings):\n"
            f"{json.dumps(digest, indent=1)}\n</cross_reference_digest>")


def _notes_block(digest: dict) -> str:
    """Synthesis input: the structured digest as JSON + any recovered markdown
    chunk notes rendered readably (the champion often emits markdown, not JSON)."""
    md_notes = digest.get("markdown_notes", [])
    structured = {k: v for k, v in digest.items() if k != "markdown_notes"}
    parts = ["<chunk_findings>\nAggregated notes from every chunk (consolidate "
             "THESE; do not re-read the repo).\n\nStructured findings:\n"
             f"{json.dumps(structured, indent=1)}"]
    if md_notes:
        parts.append("\n\nAdditional per-chunk findings (markdown):\n" + "\n\n".join(
            f"### chunk {n.get('chunk', '?')} ({n.get('label', '')})\n{n.get('md', '')}"
            for n in md_notes))
    parts.append("\n</chunk_findings>")
    return "".join(parts)


def _prior_findings_block(prior: str) -> str:
    """Pure-data block carrying a prior same-commit gitaudit's findings (the
    directive lives in GIT_CHANGE_*_HINT)."""
    return f"<prior_findings>\n{prior.strip()}\n</prior_findings>"


# --- per-kind directive maps (strings stay in agents/prompts.py) ------------

# Two kinds. gitaudit emits a markdown audit report per chunk (bugs/security +
# structural improvements) recovered/packaged like the old review path; gitchange
# emits per-chunk steps (markdown, recovered to JSON) that accumulate in the `steps`
# digest bucket and are consolidated into one ordered plan at synthesis. Both
# auto-route to deep on large repos; the chunk loop has a gitchange-specific
# step-recovery branch (a prose chunk → steps via the extract hint).
_CHUNK_HINTS = {
    "gitaudit": prompts.GIT_AUDIT_CHUNK_HINT,
    "gitchange": prompts.GIT_CHANGE_CHUNK_HINT,
    "gitaudit-diff": prompts.GIT_AUDIT_DIFF_CHUNK_HINT,
}
_SYNTH_HINTS = {
    "gitaudit": prompts.GIT_AUDIT_SYNTH_HINT,
    "gitchange": prompts.GIT_CHANGE_SYNTH_HINT,
    "gitaudit-diff": prompts.GIT_AUDIT_DIFF_SYNTH_HINT,
}


# --- orchestration ----------------------------------------------------------

def run_deep_report(
    kind: str,
    *,
    target: str,
    task_type: str,
    backend,
    role_cfg,
    languages,
    console,
    reader,
    summary,
    symbol_index=None,
    health_block: str = "",
    save: bool = True,
    verbose: bool = False,
    cancel=None,
    max_chunks: int | None = None,
    rebuild_map: bool = False,
    prior_report: str = "",
    mirror: bool = True,
    run_single_fn=None,
    chunks_override: list[Chunk] | None = None,
    chunk_extra_blocks: dict[int, str] | None = None,
    survey_notes_override: str | None = None,
    postprocess=None,
    extra_meta: dict | None = None,
    min_severity: str | None = None,
    no_incremental: bool = False,
) -> tuple[str, Path | None]:
    """Run the staged deep analysis. Returns (report_text, saved_path | None);
    ("", None) on cancel/decline. The caller (runner) owns target resolution,
    index build/restore, and model unload; this owns the staging.

    `run_single_fn` is injectable for tests (count passes with a stub); defaults
    to the real `run_single`.

    Diff mode (`gitaudit --base/--pr`) passes `chunks_override` (chunks built
    over the CHANGED files only) — that skips the survey pass entirely and
    neither reads nor writes the per-repo `map/` cache (gitkit.sdd diff-mode
    rules); `survey_notes_override` may opportunistically inject a FRESH
    whole-repo map's survey notes. `chunk_extra_blocks` appends a per-chunk
    pure-data block (the chunk-scoped `<change_diff>`) to that chunk's
    extra_context. `postprocess` (report→report) runs before save —
    deterministic tag-prior/caveat rendering. `extra_meta` merges into the
    saved report's frontmatter (base / merge_base).
    """
    from rich.markdown import Markdown

    from luxe.chat.render import ChatCancelled, raise_if_cancelled, truncate_for_display
    from luxe.gitkit import health, store
    from luxe.gitkit.runner import _activity_callbacks

    if run_single_fn is None:
        from luxe.agents.single import run_single as run_single_fn

    # Three windows/caps, deliberately different (copies — never mutate the
    # shared role):
    #  - CHUNK passes run at the BASE window (deep_window) so chunks are small
    #    enough to cover all their files before the model truncates its analysis.
    #  - The SYNTHESIS pass runs at the EXPANDED window (num_ctx_max) since it is a
    #    single pass that must hold ALL the aggregated notes at once, with extra
    #    generation headroom for the consolidated report.
    eff_ctx = deep_window(role_cfg)
    synth_ctx_win = getattr(role_cfg, "num_ctx_max", 0) or eff_ctx
    chunk_role = role_cfg.model_copy(
        update={"num_ctx": eff_ctx, "max_tokens_per_turn": _CHUNK_MAX_TOKENS})
    synth_role = role_cfg.model_copy(
        update={"num_ctx": synth_ctx_win, "max_tokens_per_turn": _DEEP_MAX_TOKENS})
    content_budget = max(1, int(eff_ctx * _CONTENT_BUDGET_FRAC))
    ceiling = int(eff_ctx * _DIGEST_CEILING_FRAC)
    head = health.current_head(target)
    # Per-pass wall-clock telemetry, accumulated across every `_pass` call (B1/B2).
    timings: list[PassTiming] = []

    def _emit(text: str) -> None:
        # NB: avoid a literal "[deep]" — Rich would parse it as a markup tag and
        # strip it. Use a plain "deep ·" prefix instead.
        console.print(f"[dim]· deep · {text}[/]")

    def _pass(goal: str, extra_context: str, label: str, role=None):
        role = role or chunk_role
        start = int(time.time())
        if console.is_terminal:
            with console.status(f"[dim]deep · {label}…[/]", spinner="dots") as st:
                on_e, on_t = _activity_callbacks(
                    lambda t: st.update(f"[dim]deep · {label} · {t}[/]"), cancel=cancel)
                res = run_single_fn(
                    backend, role, goal=goal, task_type=task_type,
                    languages=languages, extra_context=extra_context,
                    on_tool_event=on_e, on_token=on_t,
                    phase="chat", run_id=f"gitkit-deep-{kind}-{label}")
        else:
            on_e, on_t = _activity_callbacks(lambda t: None, cancel=cancel)
            res = run_single_fn(
                backend, role, goal=goal, task_type=task_type,
                languages=languages, extra_context=extra_context,
                on_tool_event=on_e, on_token=on_t,
                phase="chat", run_id=f"gitkit-deep-{kind}-{label}")
        # Read only public result fields (getattr-guarded — stubs + backends that
        # populate steps/tool_calls_total differently both stay safe).
        timings.append(PassTiming(
            label=label, kind=kind,
            wall_s=round(float(getattr(res, "wall_s", 0.0) or 0.0), 3),
            completion_tokens=int(getattr(res, "completion_tokens", 0) or 0),
            steps=int(getattr(res, "steps", 0) or 0),
            tool_calls_total=int(getattr(res, "tool_calls_total", 0) or 0),
            window=int(getattr(role, "num_ctx", 0) or 0),
            started_at=start))
        return res

    def _handle_partial_map(status: MapStatus) -> CacheDecision:
        """A damaged map (heavy file missing/corrupt) is ANNOUNCED, never silently
        equated with 'never mapped'. Interactive → ask rebuild/cancel; batch (no
        TTY) → log loudly and rebuild (never block)."""
        miss = ", ".join(status.missing) or "?"
        prior = (f"HEAD {status.head[:8] or '?'}, {status.n_chunks} chunks, "
                 f"mapped {_age_str(status.mapped_at)} ago")
        if console.is_terminal:
            _emit(f"map partial — prior map ({prior}); missing/corrupt: {miss}")
            ans = reader("  map partial — [Y] rebuild, [n] cancel: ").strip().lower()
            return CacheDecision.CANCEL if ans in ("n", "no") else CacheDecision.REBUILD
        _emit(f"map partial ({miss}) — rebuilding (re-surveying)")
        return CacheDecision.REBUILD

    # --- Stages 0+1: survey + chunk plan (cached per repo, HEAD-keyed) -------
    current_shas: dict[str, str] = {}
    if chunks_override is not None:
        # Diff mode: chunks cover the CHANGED files only. NO survey pass (diff
        # audits must be fast) and NO map/ reads or writes; a FRESH whole-repo
        # map's survey notes may be injected opportunistically by the caller.
        chunks = chunks_override
        survey_notes = survey_notes_override or "(no survey — diff-scoped audit)"
        framing = []
        cached = True  # sentinel: skip the survey/save_map branch below
    else:
        status = map_status(target, head=head)
        cached = None if rebuild_map else load_map(target, head=head)
        if not rebuild_map and status.state is MapState.PARTIAL:
            if _handle_partial_map(status) is CacheDecision.CANCEL:
                console.print("[yellow]· cancelled.[/]")
                return "", None
            # else fall through to re-survey (cached stays None)
        if cached:
            _emit(f"reusing cached repo map (HEAD {head or '?'})")
            survey_notes = cached["survey_notes"]
            chunks = cached["chunks"]
            framing = cached["framing"]
        elif (not rebuild_map and not no_incremental
              and status.state is MapState.STALE
              and status.version >= 2 and status.files):
            # INCREMENTAL RE-AUDIT: HEAD moved but the v2 breadcrumb carries
            # blob shas — keep the survey + partition, prune deletions, append
            # delta chunks, and let the sha-validated notes cache decide which
            # chunks actually re-run. plan_incremental is pure; any rebuild
            # trigger is logged loudly (never silently skipped).
            stale = load_map(target, head=head, allow_stale=True)
            if stale is not None:
                current_shas = git_file_shas(target)
                added = set(current_shas) - set(status.files)
                added_recs = [r for r in enumerate_files(target, summary,
                                                         log=_emit)
                              if r.rel in added]
                plan = plan_incremental(
                    old_files=status.files, new_files=current_shas,
                    chunks=stale["chunks"], baseline=status.baseline,
                    added_recs=added_recs, content_budget=content_budget,
                    symbol_index=symbol_index)
                if plan.mode == "incremental":
                    survey_notes = stale["survey_notes"]
                    chunks = plan.chunks
                    framing = stale["framing"]
                    _emit(f"incremental: HEAD {status.head[:8] or '?'} → "
                          f"{(head or '?')[:8]} — {plan.reason}; "
                          f"{len(plan.dirty)} chunk(s) marked dirty")
                    save_map(target, head=head, survey_notes=survey_notes,
                             chunks=chunks, content_budget=content_budget,
                             framing=framing, summary_render=summary.render(),
                             files=current_shas, baseline=plan.baseline)
                    cached = True  # sentinel: skip the survey/save_map branch
                else:
                    _emit(f"incremental unavailable — {plan.reason}; "
                          "full rebuild (re-survey)")
    if not cached:
        framing = framing_files(target)
        survey_ctx = (f"{health_block}\n\n<repo_map>\n{summary.render()}\n</repo_map>"
                      f"\n\n{_framing_block(framing)}")
        survey_goal = ("Survey the repository in the current working directory.\n\n"
                       + prompts.GIT_SURVEY_HINT)
        try:
            res = _pass(survey_goal, survey_ctx, "survey")
        except (ChatCancelled, KeyboardInterrupt):
            console.print("[yellow]· cancelled.[/]")
            return "", None
        survey_notes = (getattr(res, "final_text", "") or "").strip() \
            or "(survey produced no notes)"
        files = enumerate_files(target, summary, log=_emit)
        chunks = build_chunks(files, content_budget=content_budget,
                              symbol_index=symbol_index)
        save_map(target, head=head, survey_notes=survey_notes, chunks=chunks,
                 content_budget=content_budget, framing=framing,
                 summary_render=summary.render())

    # max-chunks safety valve (loud — no silent truncation).
    if max_chunks is not None and len(chunks) > max_chunks:
        dropped = chunks[max_chunks:]
        dropped_dirs = sorted({c.label for c in dropped})
        chunks = chunks[:max_chunks]
        _emit(f"--max-chunks={max_chunks}: analyzing {len(chunks)} of "
              f"{len(chunks) + len(dropped)} chunks; SKIPPING {len(dropped)} "
              f"(areas: {', '.join(dropped_dirs)})")

    # Sha-validated per-chunk notes reuse (incremental re-audit + crash-resume):
    # a cached contribution is reused iff it covers exactly the chunk's files
    # and every blob sha matches the CURRENT tree. Cached contributions are
    # chunk INPUTS only — the digest is rebuilt from scratch every run and the
    # synthesis always re-runs, so a stale finding cannot survive its source
    # chunk's invalidation by construction.
    contributions: dict[int, dict] = {}
    if chunks_override is None:
        current_shas = current_shas or git_file_shas(target)
        if not no_incremental and not rebuild_map:
            for c in chunks:
                if not c.files:
                    continue
                note = load_chunk_note(target, kind, c.index)
                if chunk_note_is_valid(note, c, current_shas):
                    contributions[c.index] = note["contribution"]
    n_eff = sum(1 for c in chunks if c.files)
    if contributions:
        _emit(f"incremental: reusing {len(contributions)}/{n_eff} cached chunk "
              f"note(s) — {n_eff - len(contributions)} chunk pass(es) to run")

    # The survey pass has already completed by here (reused from the map cache OR
    # freshly run above), so the estimate covers the REMAINING chunk + synth work.
    est = estimate_run(n_eff - len(contributions), survey_cached=True)
    _emit(f"plan: {est.line()}")

    # Confirmation gate — large repos only, interactive only.
    if est.large and console.is_terminal:
        ans = reader(f"  deep analysis: {est.line()}. Proceed? [Y/n]: ").strip().lower()
        if ans in ("n", "no"):
            console.print("[yellow]· cancelled.[/]")
            return "", None

    work_dir = _new_work_dir(target, kind) if save else None
    digest = empty_digest()
    from luxe.gitkit.runner import extract_report

    # --- Stage 2: per-chunk analysis ----------------------------------------
    chunk_hint = _CHUNK_HINTS[kind]
    try:
        for c in chunks:
            if not c.files:
                continue
            raise_if_cancelled(cancel) if cancel is not None else None
            if c.index in contributions:
                # Clean chunk: fold the cached contribution in chunk order,
                # exactly as the live path would have.
                fold_contribution(digest, contributions[c.index], c.index)
                if estimate_tokens(json.dumps(digest)) > ceiling:
                    digest = compact_digest(digest, ceiling_tokens=ceiling,
                                            log=_emit)
                _emit(f"chunk {c.index + 1}/{len(chunks)} ({c.label}) — "
                      "cached note reused")
                if work_dir is not None:
                    (work_dir / "xref.json").write_text(
                        json.dumps(digest, indent=2))
                continue
            contribution: dict = {}
            _emit(f"chunk {c.index + 1}/{len(chunks)} ({c.label})")
            extra = (f"<survey_notes>\n{survey_notes}\n</survey_notes>\n\n"
                     f"{_digest_block(digest)}\n\n{_chunk_block(c, len(chunks))}")
            if chunk_extra_blocks and c.index in chunk_extra_blocks:
                extra += f"\n\n{chunk_extra_blocks[c.index]}"
            goal = (f"Analyze chunk {c.index + 1} of {len(chunks)} of this "
                    f"repository.\n\n{chunk_hint}")
            res = _pass(goal, extra, f"chunk-{c.index + 1}")
            # Enrich the just-recorded chunk timing with its footprint — defensively
            # (a mid-pass throw would have raised before appending, so guard the
            # index + label match rather than blindly indexing timings[-1]).
            if timings and timings[-1].label == f"chunk-{c.index + 1}":
                timings[-1].est_tokens = c.est_tokens
                timings[-1].loc = c.loc
                timings[-1].n_files = len(c.files)
            text = (getattr(res, "final_text", "") or "").strip()
            parsed = parse_chunk_notes(text)
            if work_dir is not None:
                (work_dir / f"chunk-{c.index + 1:02d}.md").write_text(
                    text or "(no output)")

            if kind == "gitchange":
                # gitchange chunks emit a CONCISE MARKDOWN step list (a JSON-only chunk
                # contract makes the champion ramble past the cap without concluding
                # — confirmed on luxe). Recover gitplan/v1 steps via the transcription
                # pass; a chunk that already emitted JSON steps is used directly. Three
                # outcomes: steps recorded / analyzed-but-no-steps / unanalyzed (a
                # genuine coverage gap — never silently dropped).
                steps_obj = parsed if (parsed and parsed.get("steps")) else None
                analyzed = parsed is not None      # chunk emitted parseable JSON
                if steps_obj is None and text and not parsed:
                    recovered = parse_chunk_notes(
                        _plan_extract_pass(text, pass_fn=_pass, role=chunk_role))
                    if recovered is not None:       # transcription produced valid JSON
                        analyzed = True
                        if recovered.get("steps"):
                            steps_obj = recovered
                if steps_obj and steps_obj.get("steps"):
                    update_digest(digest, steps_obj, c.index)
                    contribution["parsed"] = steps_obj
                    if estimate_tokens(json.dumps(digest)) > ceiling:
                        digest = compact_digest(digest, ceiling_tokens=ceiling,
                                                log=_emit)
                    _emit(f"chunk {c.index + 1}: {len(steps_obj['steps'])} "
                          "step(s) recorded")
                elif analyzed:
                    _emit(f"chunk {c.index + 1}: no structural steps in these files")
                else:
                    label = f"chunk {c.index + 1} ({c.label}): " \
                        + ", ".join(c.files[:4]) + (" …" if len(c.files) > 4 else "")
                    digest["unparsed_chunks"].append(label)
                    contribution["unparsed"] = label
                    _emit(f"chunk {c.index + 1} produced no usable steps "
                          "(empty/truncated) — flagged as unanalyzed")
                if chunks_override is None:
                    save_chunk_note(
                        target, kind, c, head=head,
                        file_shas={r: current_shas.get(r, "")
                                   for r in c.files},
                        contribution=contribution,
                        wall_s=(timings[-1].wall_s if timings else 0.0))
                if work_dir is not None:
                    (work_dir / "xref.json").write_text(json.dumps(digest, indent=2))
                continue

            note_src = None
            if parsed:
                update_digest(digest, parsed, c.index)
                contribution["parsed"] = parsed
                if estimate_tokens(json.dumps(digest)) > ceiling:
                    digest = compact_digest(digest, ceiling_tokens=ceiling, log=_emit)
            elif _has_report_header(text, kind):
                # The model concluded with the required header itself — slice off
                # any leading monologue and keep the conclusion.
                note_src = extract_report(text, kind)
            elif text:
                # The common champion case: the final message is a long file-by-file
                # analysis that never reaches a structured conclusion. The findings
                # ARE in that prose; _clean_note recovers them (transcription pass →
                # heuristic). This is load-bearing: the model won't self-package.
                note_src = text
            if note_src is not None:
                # Always store a CLEAN note (as-is if already clean, else a
                # transcription pass, else heuristic finding lines) so the final
                # report can be assembled without any rambly text.
                clean, note_source = _clean_note(note_src, kind, pass_fn=_pass,
                                                 role=chunk_role, log=_emit)
                if clean:
                    digest["markdown_notes"].append(
                        {"chunk": c.index, "label": c.label, "md": clean,
                         "source": note_source})
                    contribution["note"] = {"label": c.label, "md": clean,
                                            "source": note_source}
                    _emit(f"chunk {c.index + 1}: findings recorded")
                else:
                    note_src = None  # nothing salvageable → fall through to unparsed
            if note_src is None and not parsed:
                # Empty/unsalvageable output — never silently drop a chunk; record
                # it so the report can flag the coverage gap.
                label = f"chunk {c.index + 1} ({c.label}): {', '.join(c.files[:4])}" \
                    + (" …" if len(c.files) > 4 else "")
                digest["unparsed_chunks"].append(label)
                contribution["unparsed"] = label
                _emit(f"chunk {c.index + 1} produced no usable findings "
                      f"(empty/truncated) — flagged as unanalyzed")
            if chunks_override is None:
                save_chunk_note(
                    target, kind, c, head=head,
                    file_shas={r: current_shas.get(r, "") for r in c.files},
                    contribution=contribution,
                    wall_s=(timings[-1].wall_s if timings else 0.0))
            if work_dir is not None:
                (work_dir / "xref.json").write_text(json.dumps(digest, indent=2))
    except (ChatCancelled, KeyboardInterrupt):
        if work_dir is not None:
            (work_dir / "xref.json").write_text(json.dumps(digest, indent=2))
            console.print(f"[yellow]· cancelled — partial notes saved to "
                          f"{work_dir}[/]")
        else:
            console.print("[yellow]· cancelled.[/]")
        return "", None

    # Final dedupe/merge before synthesis (also re-runs the merge globally).
    digest = compact_digest(digest, ceiling_tokens=0)

    # --- Stage 3: synthesis at the EXPANDED window (one pass over all notes) -
    notes_tokens = estimate_tokens(json.dumps(digest))
    if notes_tokens > _SYNTH_REDUCE_FRAC * synth_ctx_win:
        _emit(f"aggregate notes large ({notes_tokens} tok) — 2-level reduce")
        digest = _reduce_findings(digest, eff_ctx=synth_ctx_win, pass_fn=_pass,
                                  log=_emit)

    synth_ctx = (f"{health_block}\n\n<survey_notes>\n{survey_notes}\n</survey_notes>"
                 f"\n\n{_notes_block(digest)}")
    if prior_report:
        synth_ctx += f"\n\n{_prior_findings_block(prior_report)}"
    synth_goal = ("Write the final consolidated report for the repository in the "
                  "current working directory.\n\n" + _SYNTH_HINTS[kind])
    try:
        res = _pass(synth_goal, synth_ctx, "synthesis", role=synth_role)
    except (ChatCancelled, KeyboardInterrupt):
        console.print("[yellow]· cancelled.[/]")
        return "", None

    synth_text = (getattr(res, "final_text", "") or "").strip()
    if kind == "gitchange":
        # gitchange emits a structured JSON plan, not a markdown report. Robustness
        # ladder: parse the synthesis JSON → on a prose synthesis, a transcription
        # recovery pass (its own draft → JSON) → finally the aggregated per-chunk
        # digest steps (Python packaging never rambles, so a valid plan is always
        # produced). Then save plan.json + render the markdown deterministically.
        from luxe.gitkit import plan as plan_mod
        from luxe.gitkit.runner import _TITLES

        def _extract_plan_json(draft: str) -> str:
            return _plan_extract_pass(draft, pass_fn=_pass, role=synth_role)

        report, _ = plan_mod.finalize_and_save(
            target, head, synth_text, extract_fn=_extract_plan_json,
            fallback_steps=digest.get("steps"), title=_TITLES["gitchange"])
    else:
        report = extract_report(synth_text, kind)
        # Use the LLM synthesis ONLY if it came back clean. The champion narrates its
        # consolidation into the report, so when it doesn't: try a strict transcription
        # pass, and if THAT is still rambly, assemble the report DETERMINISTICALLY from
        # the (already-cleaned) per-chunk notes — Python packaging never rambles, so a
        # clean, complete report is guaranteed.
        if not report or _looks_rambly(report):
            cleaned = (_format_final_report(synth_text, kind, pass_fn=_pass,
                                            role=synth_role)
                       if _looks_rambly(synth_text) else report)
            if cleaned and not _looks_rambly(cleaned):
                _emit("synthesis verbose — formatted a clean report")
                report = cleaned
            else:
                _emit("synthesis unclean — assembling report deterministically")
                report = _render_report(digest, kind)
        report = report or _render_report(digest, kind) or "(no report produced)"

    if postprocess is not None:
        report = postprocess(report)

    # --- timing telemetry: raw per-pass records + cheap aggregates (B3/B4) ----
    total_wall_s = round(sum(t.wall_s for t in timings), 3)
    n_passes = len(timings)
    avg_pass_s = round(total_wall_s / n_passes, 3) if n_passes else 0.0
    if work_dir is not None:
        # The `passes` list is the raw, append-only record (ages best); the
        # aggregates are convenience derivations of it.
        (work_dir / "timing.json").write_text(json.dumps({
            "kind": kind, "head": head, "n_passes": n_passes,
            "total_wall_s": total_wall_s, "avg_pass_s": avg_pass_s,
            "passes": [t.to_dict() for t in timings],
        }, indent=2))

    saved: Path | None = None
    if save:
        saved = store.save_report(
            target, kind, report,
            meta={"model": backend.model, "head": head, "repo": target,
                  "mode": "deep", "chunks": len(chunks),
                  "total_wall_s": total_wall_s, "n_passes": n_passes,
                  "avg_pass_s": avg_pass_s, **(extra_meta or {})})
        if mirror and store.mirror_to_repo(target, kind, report, head):
            _emit("mirrored map + report to <repo>/.luxe/gitkit/")

    console.print()
    display_src, n_filtered = report, 0
    if min_severity:
        # DISPLAY-side only — the saved report above is always unfiltered.
        display_src, n_filtered = store.filter_min_severity(report, min_severity)
    if verbose:
        console.print(Markdown(display_src))
    else:
        shown, hidden = truncate_for_display(display_src, max_lines=30)
        console.print(Markdown(shown))
        if hidden:
            console.print(f"[dim]… +{hidden} more lines — full report saved[/]")
    if n_filtered:
        where = saved if saved else "(not saved — run without --no-save)"
        console.print(f"[dim]Filtered: {n_filtered} findings below "
                      f"{min_severity} — full report at {where}[/]")
    if saved:
        _emit(f"deep report ({len(chunks)} chunks) saved")
        console.print(f"[green]✓[/] report saved to [cyan]{saved}[/]")
        if work_dir is not None:
            console.print(f"[dim]· survey/chunk notes: {work_dir}[/]")
    return report, saved


_RAMBLE_MARKERS = (
    "let me", "i need to", "i should", "wait,", "okay,", "ok,", "chain-of-thought",
    "i'll ", "let's ", "first, i", "now i", "hmm", "actually,", "working notes",
    "re-rating", "consolidation:", "i realize", "to summarize my",
)


def _looks_rambly(report: str) -> bool:
    """Heuristic: did a report pass narrate its reasoning instead of emitting a
    clean report? Long output or first-person reasoning markers in the body."""
    if not report:
        return False
    if len(report.splitlines()) > 200:
        return True
    low = report.lower()
    return sum(low.count(m) for m in _RAMBLE_MARKERS) >= 3


def _format_final_report(draft: str, kind: str, *, pass_fn, role) -> str | None:
    """Strict transcription pass: reproduce a clean report from a rambly synthesis
    draft (copy findings verbatim, drop the narration). Returns the sliced clean
    report, or None if it still has no header."""
    from luxe.gitkit.runner import extract_report
    ctx = f"<report_draft>\n{draft}\n</report_draft>"
    goal = ("Produce the clean final report from the draft below.\n\n"
            + prompts.GIT_DEEP_FORMAT_HINT)
    res = pass_fn(goal, ctx, "format", role=role)
    text = (getattr(res, "final_text", "") or "").strip()
    sliced = extract_report(text, kind)
    return sliced if _has_report_header(sliced, kind) else None


def _render_report(digest: dict, kind: str) -> str:
    """Assemble a clean final report DETERMINISTICALLY from the (already-cleaned)
    per-chunk notes + any JSON findings + coverage gaps. This is the guaranteed
    non-rambly path when the LLM synthesis won't behave — Python never rambles."""
    from luxe.gitkit.runner import _TITLES
    title = _TITLES.get(kind, "Report")
    notes = digest.get("markdown_notes", [])
    pf = digest.get("provisional_findings", [])
    unparsed = digest.get("unparsed_chunks", [])

    sections: list[str] = []
    for n in notes:
        body = _strip_report_header(n.get("md", ""))
        if body:
            head = (f"## Area: {n.get('label', '?')} "
                    f"(chunk {n.get('chunk', 0) + 1})")
            if n.get("source") == "heuristic":
                head += ("\n\n*(heuristic salvage from verbose model output — "
                         "confidence: low)*")
            sections.append(f"{head}\n\n{body}")
    if pf:
        # severity desc, then deterministic confidence desc (evidence-weighted)
        ranked = sorted(pf, key=lambda f: (
            -_SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 0),
            -confidence_of(f)[0]))
        lines = []
        for f in ranked:
            ev = (f.get("evidence") or ["?"])[0]
            conf = f.get("confidence") or confidence_of(f)[1]
            lines.append(f"- **{f.get('severity', '?')}** `{ev}` — "
                         f"{f.get('title', '')}. {f.get('impact', '')} "
                         f"Fix: {f.get('fix', '')}".rstrip()
                         + f" *(confidence: {conf})*")
        sections.append("## Additional findings\n\n" + "\n".join(lines))

    # Only gitaudit reaches deterministic render (gitchange returns via plan_mod).
    n = len(pf) + sum(len(_heuristic_findings(s)) for s in sections)
    header = f"# {title}\n**Findings: {n} (consolidated across chunks)**"

    out = [header, *sections]
    if unparsed:
        out.append("## Coverage gaps\n\nThese areas could not be analyzed "
                   "(verbose or empty model output) and may still contain issues:\n"
                   + "\n".join(f"- {u}" for u in unparsed))
    if not sections and not unparsed:
        out.append("No findings were recorded.")
    return "\n\n".join(out)


# Heuristic finding patterns for the deterministic-render fallback (review).
_SEV_LINE_RE = re.compile(
    r"(critical|high|medium|low)\b.*?(`[^`]+`|\b[\w./-]+\.[a-z]{1,4}:\d+)",
    re.IGNORECASE)
# Additional finding shapes the champion actually emits when it rambles past the
# report header (offline-recovery analysis 2026-06-08, scripts/recover_offline.py):
# numbered BOLD list items carrying a file/line/code ref, and canonical report
# bullets. Keyed on the FINDING shape (numbered+bold, or a labelled bullet) so plain
# exploration narrative ("Let me look at cli.py:29") is not swept in. This lifted
# heuristic salvage on captured unparsed dumps from ~2% to ~64% at zero model cost.
_NUM_BOLD_RE = re.compile(r"^\s*\d+[.)]\s+\*\*")
_REPORT_BULLET_RE = re.compile(
    r"\*\*\s*(file|issue|bug|severity|line|impact|fix|problem|risk|location)\b", re.I)
_FILE_LINE_RE = re.compile(
    r"\b[\w./-]+\.(py|rs|js|ts|tsx|go|sh|ya?ml|toml|c|cpp|h)\b(?:[:\s]+(?:line\s+)?\d+)",
    re.I)
_BOLD_FILE_RE = re.compile(
    r"\*\*[^*]*?(?:\.(py|rs|js|ts|go|sh|ya?ml)\b|line\s+\d+)[^*]*?\*\*", re.I)
# Lines the model explicitly marks as NON-findings — drop them to keep the salvage clean.
_NON_FINDING_RE = re.compile(
    r"\b(not a bug|no issue|no code|nothing here|n/?a|this is correct"
    r"|let me (check|verify|look|see))\b", re.I)
# A4 (2026-06-10) — broadened salvage shapes, derived from the 9-repo gap corpus
# (scripts/recover_offline.py dumps): numbered NON-bold items carrying a file/line
# ref; bold/bracket/`Severity:` severity-LEAD lines whose file ref may trail within
# the next 2 lines; `###`/`####` finding headings carrying a severity word or file
# ref. Still keyed on FINDING shape so exploration narrative is not swept in.
_NUM_PLAIN_RE = re.compile(r"^\s*\d+[.)]\s+\S")
_SEV_LEAD_RE = re.compile(
    r"^(?:\*\*\s*(?:critical|high|medium|low)\b[^*]*\*\*"
    r"|\[\s*(?:critical|high|medium|low)\s*\]"
    r"|severity\s*[:=]\s*(?:critical|high|medium|low)\b)",
    re.IGNORECASE)
_SEV_WORD_RE = re.compile(r"\b(critical|high|medium|low)\b", re.IGNORECASE)
_FINDING_HEADING_RE = re.compile(r"^#{3,4}\s+\S")
_FILE_REF_RE = re.compile(
    r"\b[\w./-]+\.(py|rs|js|ts|tsx|go|sh|ya?ml|toml|c|cpp|h)\b", re.IGNORECASE)


def _strip_report_header(md: str) -> str:
    """Drop a note's own `# <title>` + `**Findings: …**`/`**Use-risk…**` lines so
    the body can be re-grouped under a single final header."""
    lines = (md or "").splitlines()
    out, skipping = [], True
    for ln in lines:
        if skipping and (ln.strip() == "" or ln.startswith("# ")
                         or ln.strip().startswith("**Findings:")
                         or ln.strip().startswith("**Use-risk")
                         or ln.strip().startswith("**Refactor steps")):
            continue
        skipping = False
        out.append(ln)
    return "\n".join(out).strip()


def _heuristic_findings(text: str, *, cap: int = 60) -> list[str]:
    """Last-resort: pull finding-shaped lines out of rambly prose, deduped, so a
    clean report can still be rendered. Matches the shapes the champion emits when
    it never reaches the report header — a severity word + `path`/file:line, OR a
    numbered BOLD item with a file/line/code ref, OR a canonical report bullet
    (**File:**/**Impact:**/…), OR (A4) a numbered non-bold item with a file:line
    ref, a severity-lead line (file ref within 2 lines), or a ###/#### finding
    heading. Keeps markdown markers (matches the raw line) so the bold/numbered
    shapes survive; drops explicit non-findings."""
    seen: set[str] = set()
    out: list[str] = []
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if len(s) < 12 or _NON_FINDING_RE.search(s):
            continue
        emit: str | None = None
        if (_SEV_LINE_RE.search(s)
                or (_NUM_BOLD_RE.search(s)
                    and (_FILE_LINE_RE.search(s) or _BOLD_FILE_RE.search(s)
                         or "`" in s))
                or _REPORT_BULLET_RE.search(s)):
            emit = s
        elif _NUM_PLAIN_RE.match(s) and _FILE_LINE_RE.search(s):
            # numbered non-bold item with an explicit file:line ref
            emit = s
        elif (_FINDING_HEADING_RE.match(s) and _SEV_WORD_RE.search(s)
              and (_FILE_REF_RE.search(s) or "`" in s or any(c.isdigit() for c in s))):
            # ###/#### finding heading: severity word PLUS substance (file ref /
            # code / number). A file ref alone is the per-file EXPLORATION
            # heading shape ("### app/api/auth.py") — corpus-verified FP class.
            emit = s
        elif _SEV_LEAD_RE.match(s):
            # severity-lead line; the file ref may trail within the next 2 lines
            if _FILE_LINE_RE.search(s) or _FILE_REF_RE.search(s):
                emit = s
            else:
                for la in lines[i + 1:i + 3]:
                    if _FILE_LINE_RE.search(la) or _FILE_REF_RE.search(la):
                        emit = f"{s} — {la.strip()}"
                        break
        if not emit:
            continue
        # dedup key ignores list numbering ("1." vs "2." re-numberings of the
        # same finding) and trailing-tail variants (first 100 chars).
        key = re.sub(r"^\d+[.)]\s+", "", emit.lower())
        key = re.sub(r"\s+", " ", key)[:100]
        if key in seen:
            continue
        seen.add(key)
        out.append(emit[:200])
        if len(out) >= cap:
            break
    return out


def _clean_note(md: str, kind: str, *, pass_fn, role,
                log=None) -> tuple[str | None, str]:
    """Return (clean note, provenance source) for a per-chunk note: as-is when
    already clean (`md_clean`), else a transcription pass (`md_transcribed`),
    else a heuristic finding list (`heuristic`). (None, "") if nothing
    salvageable. `log` surfaces WHICH recovery rung packaged the note."""
    if not md:
        return None, ""
    if not _looks_rambly(md):
        return md, "md_clean"
    cleaned = _format_final_report(md, kind, pass_fn=pass_fn, role=role)
    if cleaned and not _looks_rambly(cleaned):
        if log:
            log("rambly note → transcription pass recovered")
        return cleaned, "md_transcribed"
    bullets = _heuristic_findings(md)
    if bullets:
        if log:
            log(f"rambly note → heuristic salvage ({len(bullets)} lines)")
        return "\n".join(f"- {b}" for b in bullets), "heuristic"
    return None, ""


def _reduce_findings(digest: dict, *, eff_ctx: int, pass_fn, log=None) -> dict:
    """2-level reduce: consolidate provisional_findings in window-sized batches
    via LLM merge passes, then return a digest carrying the survivors. Falls back
    to the input digest if a batch pass yields nothing parseable."""
    findings = digest.get("provisional_findings", [])
    batch_budget = int(eff_ctx * _SYNTH_REDUCE_FRAC)
    batches: list[list[dict]] = []
    cur: list[dict] = []
    for f in findings:
        cur.append(f)
        if estimate_tokens(json.dumps(cur)) > batch_budget and len(cur) > 1:
            batches.append(cur[:-1])
            cur = [f]
    if cur:
        batches.append(cur)

    survivors: list[dict] = []
    for i, batch in enumerate(batches):
        if log:
            log(f"reduce batch {i + 1}/{len(batches)} ({len(batch)} findings)")
        ctx = ("<chunk_findings>\nConsolidate these findings:\n"
               f"{json.dumps({'findings': batch}, indent=1)}\n</chunk_findings>")
        goal = "Consolidate this batch of findings.\n\n" + prompts.GIT_DEEP_REDUCE_HINT
        res = pass_fn(goal, ctx, f"reduce-{i + 1}")
        parsed = parse_chunk_notes((getattr(res, "final_text", "") or "").strip())
        if parsed and isinstance(parsed.get("findings"), list):
            survivors.extend(parsed["findings"])
        else:
            survivors.extend(batch)  # never lose findings on a parse miss

    out = dict(digest)
    out["provisional_findings"] = survivors
    return compact_digest(out, ceiling_tokens=0)
