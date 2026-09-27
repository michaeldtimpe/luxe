"""gitkit deep mode — the per-repo, HEAD-keyed `map/` cache and the
incremental re-audit (cache v2): map health, load/save, blob / working-tree
shas, the per-kind chunk notes cache, and the pure incremental planner.

On-disk format is load-bearing (existing `~/.luxe/reports/<hash>/map/` dirs
must keep loading): `tests/test_gitkit_deep_map_compat.py` pins it against a
map written by the pre-package code."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from luxe.ephemeral import is_ephemeral
from luxe.gitkit.deep.chunking import (
    _FRAMING_RE,
    Chunk,
    FileRec,
    _is_visible,
    _pick_framing,
    build_chunks,
)
from luxe.gitkit.deep.digest import update_digest


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


# --- map cache (per-repo, HEAD-keyed) ---------------------------------------

def _atomic_write_text(path: Path, text: str) -> None:
    """Same-directory tmp + os.replace: a crash mid-write never leaves a torn
    file — readers see the old content or the new content, nothing between.

    The single funnel for every `map/` + `notes/` write in deep mode, which is
    why the ephemeral guard sits here rather than at each of the eight call
    sites. An ephemeral deep run just loses its incremental cache: the next
    run re-surveys instead of resuming, which is the documented cost of the
    mode, not a correctness problem (`--rebuild-map` does the same thing)."""
    if is_ephemeral():
        return
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
    # Explicitly Any-valued: the fields are heterogeneous (str/int/dict), so
    # an inferred dict[str, object] makes every `MapStatus(**common)` below a
    # type error while being perfectly correct at runtime.
    common: dict[str, Any] = dict(head=b_head, n_chunks=n_chunks,
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
    # -z: NUL-terminated and UNQUOTED (a non-ASCII path would otherwise come
    # back C-quoted and never match the tree walk's name for it).
    ok, out = _run_git(
        ["ls-tree", "-r", "-z", "--format=%(objectname) %(path)", "HEAD"], target)
    if not ok:
        return {}
    shas: dict[str, str] = {}
    for ln in out.split("\0"):
        parts = ln.split(" ", 1)
        # the committable .luxe/ sidecar (gitkit mirror, memory.md) is luxe's
        # OWN write — it must never count as a repo change (a committed mirror
        # README would otherwise read as a "framing file changed" rebuild).
        if len(parts) == 2 and not parts[1].startswith(".luxe/"):
            shas[parts[1]] = parts[0]
    return shas


_HASH_BATCH = 200


def worktree_file_shas(target: str | Path) -> dict[str, str]:
    """{rel path: blob sha} of the WORKING TREE — what a chunk pass actually
    reads. HEAD's `ls-tree` shas, overlaid with `git hash-object` shas for
    every modified or untracked (non-ignored) file, minus files deleted in
    the tree. The cache used to validate against HEAD only, so an
    uncommitted edit reused the note written for the committed content.

    `git status` failing returns {} — every note is then invalid (re-run),
    never "assume clean"."""
    from luxe import gitcmd
    shas = git_file_shas(target)
    try:
        st = gitcmd.run_in(target, "status", "--porcelain", "-z",
                           "--untracked-files=all", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    if st.returncode != 0:
        return {}
    root = Path(target)
    to_hash: list[str] = []
    toks = st.stdout.split("\0")
    i = 0
    while i < len(toks):
        ent = toks[i]
        i += 1
        if len(ent) < 4:
            continue
        paths = [ent[3:]]
        if ent[:1] in ("R", "C"):               # "XY new\0old"
            if i < len(toks) and ent[:1] == "R":
                shas.pop(toks[i], None)         # the old name is gone
            i += 1
        for rel in paths:
            if rel.startswith(".luxe/"):
                continue
            if (root / rel).is_file():
                to_hash.append(rel)
            else:
                shas.pop(rel, None)             # deleted in the working tree
    for k in range(0, len(to_hash), _HASH_BATCH):
        batch = to_hash[k:k + _HASH_BATCH]
        try:
            r = gitcmd.run_in(target, "hash-object", "--", *batch, timeout=120)
        except (OSError, subprocess.SubprocessError):
            r = None
        out = r.stdout.split() if (r is not None and r.returncode == 0) else []
        if len(out) != len(batch):
            for rel in batch:                   # unknown content → never reusable
                shas.pop(rel, None)
            continue
        shas.update(zip(batch, out))
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
    if is_ephemeral():
        return d       # nothing is written — and no empty map/ dir either
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
         "files": files if files is not None else worktree_file_shas(target),
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
    if is_ephemeral():
        return
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


def _cacheable(contribution: dict, chunk_timings) -> bool:
    """Whether a chunk's contribution may be written to the notes cache. Never
    when ANY pass for the chunk aborted (the chunk pass itself or a recovery
    pass — a failed format pass silently degrades a note to heuristic
    salvage), and never an 'unanalyzed' result: both are transient failures,
    and a cached one would be reused by sha on every later run as though the
    chunk had been analyzed. Re-runs retry them instead."""
    if contribution.get("unparsed"):
        return False
    return not any(getattr(t, "aborted", False) for t in chunk_timings)


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
    baseline: dict = field(default_factory=dict)       # carried-forward baseline
    n_changed: int = 0          # modified+deleted+added file count


def plan_incremental(*, old_files: dict[str, str], new_files: dict[str, str],
                     chunks: list[Chunk], baseline: dict,
                     added_recs: list[FileRec], content_budget: int,
                     symbol_index=None,
                     framing: list[str] | None = None) -> IncrementalPlan:
    """PURE incremental planner (no I/O): decide full-rebuild vs incremental
    from the old/new blob-sha maps, and produce the updated partition.

    Rebuild triggers (each logged via `reason`): a FRAMING file changed — one
    the survey actually read (the map's saved `framing` list) or one that
    would now join that list; file churn (added+deleted) > 20% of mapped
    files; anti-drift compaction — cumulative delta chunks > 4, cumulative
    delta content > 15% of the original corpus tokens, or chunk count grown
    > 25% over the original partition. `framing=None` (a caller without the
    saved list) falls back to "any touched path matching the framing
    pattern", which rebuilt on every edit to any index.js / app.py / main.*.

    Incremental: survey + partition kept; deleted files pruned from their
    chunks; added files pack into APPENDED delta chunks. Which chunks re-run
    is NOT decided here: the sha-validated notes cache does that against the
    working tree (`chunk_note_is_valid`)."""
    modified = {r for r, s in new_files.items()
                if r in old_files and old_files[r] != s}
    deleted = set(old_files) - set(new_files)
    added = set(new_files) - set(old_files)

    touched = modified | deleted | added
    if framing is None:
        framing_hit = sorted(r for r in touched if _FRAMING_RE.search(r))
    else:
        old_fr = set(framing)
        new_fr = set(_pick_framing(
            {r for r in new_files if _is_visible(r)} | (old_fr - deleted)))
        # a changed/deleted file the survey READ, or an added one that would
        # now join the list it reads
        framing_hit = sorted((touched & old_fr) | (added & new_fr))
    if framing_hit:
        return IncrementalPlan("rebuild",
                               reason=f"framing file changed ({framing_hit[0]})")
    if old_files and (len(added) + len(deleted)) > _MAX_FILE_CHURN_FRAC * len(old_files):
        return IncrementalPlan(
            "rebuild", reason=f"file churn {len(added) + len(deleted)}/"
            f"{len(old_files)} mapped files (> {_MAX_FILE_CHURN_FRAC:.0%})")

    # Updated partition: prune deletions, mark dirt, append delta chunks.
    new_chunks: list[Chunk] = []
    mapped_files: set[str] = set()
    for c in chunks:
        keep = [f for f in c.files if f not in deleted]
        mapped_files.update(keep)
        nc = Chunk(index=c.index, files=keep, dirs=c.dirs, label=c.label,
                   est_tokens=c.est_tokens, loc=c.loc, symbols=c.symbols,
                   oversized=[f for f in c.oversized if f not in deleted])
        new_chunks.append(nc)

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
        chunks=new_chunks, baseline=bl,
        n_changed=len(modified) + len(deleted) + len(delta_recs))


def _new_work_dir(target: str | Path, kind: str) -> Path | None:
    from luxe.gitkit import store
    if is_ephemeral():
        return None
    ts = int(time.time())
    d = store.reports_dir(target) / f"{kind}-{ts}-{uuid.uuid4().hex[:6]}.work"
    d.mkdir(parents=True, exist_ok=True)
    return d
