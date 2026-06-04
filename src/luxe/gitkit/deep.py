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
from pathlib import Path

from luxe.agents import prompts
from luxe.context import estimate_tokens
from luxe.repo_index import (
    _DEFAULT_EXCLUDES,
    _count_lines,
    _detect_language,
    build_repo_summary,
)

# --- tuning constants (calibrate `_SECONDS_PER_CHUNK` from real runs) --------

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
# Per-pass wall estimate (rough). Calibrated from the first aurora run: 13 passes
# at a 131072 window took ~64 min ≈ 300s/pass on the M5 Max champion.
_SECONDS_PER_CHUNK = 300
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Chunk":
        return cls(
            index=int(d["index"]), files=list(d.get("files", [])),
            dirs=list(d.get("dirs", [])), label=d.get("label", ""),
            est_tokens=int(d.get("est_tokens", 0)), loc=int(d.get("loc", 0)),
            symbols=list(d.get("symbols", [])),
        )


def empty_digest() -> dict:
    return {"modules": [], "entities": [], "cross_cutting": [],
            "provisional_findings": [], "markdown_notes": [], "unparsed_chunks": []}


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
                    excludes: set[str] | None = None) -> list[FileRec]:
    """Walk `target` for recognized source files with cheap token estimates and a
    priority bucket (entry/security → recent → normal). Deterministic order is
    applied by `build_chunks`."""
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
            except OSError:
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
_DEEP_KEYS = ("findings", "modules", "entities", "cross_cutting")


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
        rec.setdefault("evidence", [])
        digest["provisional_findings"].append(rec)
    return digest


def compact_digest(digest: dict, *, ceiling_tokens: int = 0,
                   log=None) -> dict:
    """Dedupe + merge the digest so it stays under `ceiling_tokens`. Merges
    same-root-cause findings (union evidence, keep highest severity); if still
    over after dedupe, drops lowest-severity findings (logged). Returns a new
    digest dict; never mutates the input."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    for f in digest.get("provisional_findings", []):
        k = _finding_key(f)
        if not k:
            k = f"_anon_{len(order)}"
        if k in merged:
            cur = merged[k]
            cur["evidence"] = _merge_evidence(cur.get("evidence", []),
                                              f.get("evidence", []))
            if _SEVERITY_RANK.get(str(f.get("severity", "")).lower(), 0) > \
               _SEVERITY_RANK.get(str(cur.get("severity", "")).lower(), 0):
                cur["severity"] = f.get("severity", cur.get("severity"))
        else:
            merged[k] = dict(f)
            merged[k]["evidence"] = _merge_evidence(f.get("evidence", []), [])
            order.append(k)

    out = {
        "modules": list(digest.get("modules", [])),
        "entities": list(digest.get("entities", [])),
        "cross_cutting": list(digest.get("cross_cutting", [])),
        "provisional_findings": [merged[k] for k in order],
        "markdown_notes": list(digest.get("markdown_notes", [])),
        "unparsed_chunks": list(digest.get("unparsed_chunks", [])),
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


def estimate_run(n_chunks: int) -> DeepEstimate:
    passes = n_chunks + 2  # survey + synthesis
    secs = passes * _SECONDS_PER_CHUNK
    return DeepEstimate(
        chunks=n_chunks, passes=passes, seconds=secs,
        minutes=max(1, round(secs / 60)),
        large=n_chunks >= _LARGE_CONFIRM_CHUNKS,
    )


# --- map cache (per-repo, HEAD-keyed) ---------------------------------------

def _map_dir(target: str | Path):
    from luxe.gitkit import store
    return store.reports_dir(target) / "map"


def load_map(target: str | Path, *, head: str, rebuild: bool = False) -> dict | None:
    """Return the cached survey+chunks for `target` if present and still valid
    (HEAD matches, not forced to rebuild); else None."""
    if rebuild:
        return None
    d = _map_dir(target)
    head_f, chunks_f, notes_f = d / "head", d / "chunks.json", d / "survey_notes.md"
    if not (head_f.is_file() and chunks_f.is_file() and notes_f.is_file()):
        return None
    if head and head_f.read_text().strip() != head:
        return None
    try:
        chunks_blob = json.loads(chunks_f.read_text())
    except (ValueError, OSError):
        return None
    return {
        "survey_notes": notes_f.read_text(),
        "chunks": [Chunk.from_dict(c) for c in chunks_blob.get("chunks", [])],
        "content_budget": int(chunks_blob.get("content_budget", 0)),
        "framing": chunks_blob.get("framing", []),
    }


def save_map(target: str | Path, *, head: str, survey_notes: str,
             chunks: list[Chunk], content_budget: int,
             framing: list[str], summary_render: str) -> Path:
    d = _map_dir(target)
    d.mkdir(parents=True, exist_ok=True)
    (d / "head").write_text((head or "") + "\n")
    (d / "survey_notes.md").write_text(survey_notes.rstrip() + "\n")
    (d / "survey.json").write_text(json.dumps(
        {"head": head, "summary_render": summary_render, "framing": framing},
        indent=2))
    (d / "chunks.json").write_text(json.dumps(
        {"content_budget": content_budget, "framing": framing,
         "chunks": [c.to_dict() for c in chunks]}, indent=2))
    return d


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
    return (f"<chunk_files>\nChunk {chunk.index + 1}/{total} — focus area "
            f"`{chunk.label}` ({len(chunk.files)} files, ~{chunk.loc} LOC). "
            f"Analyze ONLY these files (read them with your tools):\n{files}\n\n"
            f"Symbols defined in these files: {syms}\n</chunk_files>")


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


# --- per-kind directive maps (strings stay in agents/prompts.py) ------------

_CHUNK_HINTS = {
    "gitsummary": prompts.GIT_SUMMARY_CHUNK_HINT,
    "gitreview": prompts.GIT_REVIEW_CHUNK_HINT,
    "gitrefactor": prompts.GIT_REFACTOR_CHUNK_HINT,
}
_SYNTH_HINTS = {
    "gitsummary": prompts.GIT_SUMMARY_SYNTH_HINT,
    "gitreview": prompts.GIT_REVIEW_SYNTH_HINT,
    "gitrefactor": prompts.GIT_REFACTOR_SYNTH_HINT,
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
    run_single_fn=None,
) -> tuple[str, Path | None]:
    """Run the staged deep analysis. Returns (report_text, saved_path | None);
    ("", None) on cancel/decline. The caller (runner) owns target resolution,
    index build/restore, and model unload; this owns the staging.

    `run_single_fn` is injectable for tests (count passes with a stub); defaults
    to the real `run_single`.
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

    def _emit(text: str) -> None:
        # NB: avoid a literal "[deep]" — Rich would parse it as a markup tag and
        # strip it. Use a plain "deep ·" prefix instead.
        console.print(f"[dim]· deep · {text}[/]")

    def _pass(goal: str, extra_context: str, label: str, role=None):
        role = role or chunk_role
        if console.is_terminal:
            with console.status(f"[dim]deep · {label}…[/]", spinner="dots") as st:
                on_e, on_t = _activity_callbacks(
                    lambda t: st.update(f"[dim]deep · {label} · {t}[/]"), cancel=cancel)
                return run_single_fn(
                    backend, role, goal=goal, task_type=task_type,
                    languages=languages, extra_context=extra_context,
                    on_tool_event=on_e, on_token=on_t,
                    phase="chat", run_id=f"gitkit-deep-{kind}-{label}")
        on_e, on_t = _activity_callbacks(lambda t: None, cancel=cancel)
        return run_single_fn(
            backend, role, goal=goal, task_type=task_type,
            languages=languages, extra_context=extra_context,
            on_tool_event=on_e, on_token=on_t,
            phase="chat", run_id=f"gitkit-deep-{kind}-{label}")

    # --- Stages 0+1: survey + chunk plan (cached per repo, HEAD-keyed) -------
    cached = load_map(target, head=head, rebuild=rebuild_map)
    if cached:
        _emit(f"reusing cached repo map (HEAD {head or '?'})")
        survey_notes = cached["survey_notes"]
        chunks = cached["chunks"]
        framing = cached["framing"]
    else:
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
        files = enumerate_files(target, summary)
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

    est = estimate_run(len(chunks))
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
            _emit(f"chunk {c.index + 1}/{len(chunks)} ({c.label})")
            extra = (f"<survey_notes>\n{survey_notes}\n</survey_notes>\n\n"
                     f"{_digest_block(digest)}\n\n{_chunk_block(c, len(chunks))}")
            goal = (f"Analyze chunk {c.index + 1} of {len(chunks)} of this "
                    f"repository.\n\n{chunk_hint}")
            res = _pass(goal, extra, f"chunk-{c.index + 1}")
            text = (getattr(res, "final_text", "") or "").strip()
            parsed = parse_chunk_notes(text)
            if work_dir is not None:
                (work_dir / f"chunk-{c.index + 1:02d}.md").write_text(
                    text or "(no output)")
            note_md = None
            if parsed:
                update_digest(digest, parsed, c.index)
                if estimate_tokens(json.dumps(digest)) > ceiling:
                    digest = compact_digest(digest, ceiling_tokens=ceiling, log=_emit)
            elif _has_report_header(text, kind):
                # Rare: the model concluded with the required header itself — slice
                # off any leading monologue and keep the conclusion.
                note_md = extract_report(text, kind)
                _emit(f"chunk {c.index + 1}: used self-reported findings")
            elif text:
                # The common champion case: the final message is a long file-by-file
                # analysis that runs out of tokens before any structured conclusion.
                # The findings ARE in that prose — recover them with a focused
                # EXTRACT pass that only REFORMATS the analysis into the report
                # shape (no tools, no re-analysis). This is the load-bearing fix:
                # the model will not lead with its conclusion, so we transform it.
                _emit(f"chunk {c.index + 1}: extracting findings from analysis…")
                note_md = _extract_chunk_report(
                    text, kind, pass_fn=_pass, role=chunk_role)
            if note_md and _has_report_header(note_md, kind):
                digest["markdown_notes"].append(
                    {"chunk": c.index, "label": c.label, "md": note_md})
            elif not parsed:
                # Empty output or the extract pass also failed — never silently drop
                # a chunk; record it so synthesis can flag the coverage gap.
                label = f"chunk {c.index + 1} ({c.label}): {', '.join(c.files[:4])}" \
                    + (" …" if len(c.files) > 4 else "")
                digest["unparsed_chunks"].append(label)
                _emit(f"chunk {c.index + 1} produced no usable findings "
                      f"(empty/truncated) — flagged as unanalyzed")
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
    synth_goal = ("Write the final consolidated report for the repository in the "
                  "current working directory.\n\n" + _SYNTH_HINTS[kind])
    try:
        res = _pass(synth_goal, synth_ctx, "synthesis", role=synth_role)
    except (ChatCancelled, KeyboardInterrupt):
        console.print("[yellow]· cancelled.[/]")
        return "", None

    synth_text = (getattr(res, "final_text", "") or "").strip()
    report = extract_report(synth_text, kind)
    # The champion narrates its consolidation into the report; if the synthesis
    # output is bloated with reasoning, run a strict transcription pass to produce
    # a clean report (the findings are already decided — this only reformats).
    if _looks_rambly(report or synth_text):
        _emit("synthesis verbose — formatting a clean report")
        cleaned = _format_final_report(synth_text, kind, pass_fn=_pass,
                                       role=synth_role)
        if cleaned:
            report = cleaned
    report = report or "(no report produced)"

    saved: Path | None = None
    if save:
        saved = store.save_report(
            target, kind, report,
            meta={"model": backend.model, "head": head, "repo": target,
                  "mode": "deep", "chunks": len(chunks)})

    console.print()
    if verbose:
        console.print(Markdown(report))
    else:
        shown, hidden = truncate_for_display(report, max_lines=30)
        console.print(Markdown(shown))
        if hidden:
            console.print(f"[dim]… +{hidden} more lines — full report saved[/]")
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


def _extract_chunk_report(analysis: str, kind: str, *, pass_fn, role) -> str | None:
    """Reformat a chunk's rambly analysis prose into the report shape via a focused
    pass that does NOT use tools or re-analyze — it just consolidates the findings
    the analysis already contains. Returns the sliced report (from the required
    header) or None if even this pass produced no header. This is how the deep
    engine copes with a champion that won't lead with its conclusion."""
    from luxe.gitkit.runner import extract_report
    ctx = ("<chunk_findings>\nRaw analysis of part of the repository (your own "
           "earlier notes — may be truncated). Consolidate the CONFIRMED, serious "
           "findings it contains; ignore inconclusive musing:\n"
           f"{analysis}\n</chunk_findings>")
    goal = ("Format the confirmed findings from the analysis below into the report "
            "shape. Do NOT use tools and do NOT re-read the repository — only "
            "consolidate what the analysis already establishes.\n\n"
            + _SYNTH_HINTS[kind])
    res = pass_fn(goal, ctx, "extract", role=role)
    text = (getattr(res, "final_text", "") or "").strip()
    sliced = extract_report(text, kind)
    return sliced if _has_report_header(sliced, kind) else None


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
