"""Durable project memory — curated-first, anti-accretion.

Two tiers with explicit confidence (memory.sdd):
  - repo-local `<repo>/.luxe/memory.md` — user-curated, committable, priority,
    ALWAYS injected.
  - `~/.luxe/memory/<project_hash>/facts.jsonl` — auto-captured facts tagged
    `confidence: auto`. NEVER injected until promoted to `confidence: manual`.

This prevents the "graveyard of stale preferences": only curated/promoted memory
ever enters a prompt. Injection happens only via `run_single(extra_context=...)`,
wrapped in a `<project_memory>` block — never by editing the prompt registry.

This module must never read `~/.claude/` or the repo-root `CLAUDE.md` (Claude
Code's own project memory).
"""

from __future__ import annotations

from luxe.ephemeral import is_ephemeral
from luxe.paths import luxe_home

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def memory_root() -> Path:
    return luxe_home() / "memory"


def repo_hash(repo_root: str | Path, *, length: int = 16) -> str:
    """Stable per-repo identifier: a hex slice of sha256(absolute repo path).

    The single source of truth for hashing a repo to a directory name. `length`
    16 is the default (reports, gitkit); the memory subsystem keeps 12 via
    `project_hash` for backwards-compatible store layout.
    """
    abs_path = str(Path(repo_root).resolve())
    return hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:length]


def project_hash(repo_root: str | Path) -> str:
    """12-char repo hash for the auto-memory store dir (kept stable for existing
    `~/.luxe/memory/<hash>/` layouts). Delegates to `repo_hash`."""
    return repo_hash(repo_root, length=12)


def project_store_dir(repo_root: str | Path) -> Path:
    return memory_root() / project_hash(repo_root)


def repo_memory_file(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".luxe" / "memory.md"


@dataclass
class Fact:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    ts: float = field(default_factory=time.time)
    kind: str = "pref"        # pref | fact | note
    text: str = ""
    source: str = "auto"      # auto | user
    confidence: str = "auto"  # auto (parked) | manual (injected)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Fact":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ProjectMemory:
    repo_root: str = ""
    curated_md: str = ""        # contents of repo-local .luxe/memory.md
    facts: list[Fact] = field(default_factory=list)

    @property
    def injected_facts(self) -> list[Fact]:
        """Only manually-curated/promoted facts enter context."""
        return [f for f in self.facts if f.confidence == "manual"]

    def is_empty(self) -> bool:
        return not self.curated_md.strip() and not self.injected_facts


def _facts_path(repo_root: str | Path) -> Path:
    return project_store_dir(repo_root) / "facts.jsonl"


def _read_facts(repo_root: str | Path) -> list[Fact]:
    p = _facts_path(repo_root)
    if not p.is_file():
        return []
    out: list[Fact] = []
    # errors="replace" (NOT surrogateescape): these Facts escape this module
    # into `<project_memory>` (render_block, below) and from there into the
    # request body of every chat turn. httpx encodes JSON bodies with
    # ensure_ascii=False (httpx/_content.py:177-178) then `.encode("utf-8")`,
    # which raises UnicodeEncodeError on a lone surrogate — so text handed to
    # a caller must already be valid UTF-8, never round-trip-only. A decode
    # error on the file-level read must not crash the whole read, either —
    # one bad line already degrades gracefully below (JSONDecodeError,
    # skipped).
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Fact.from_dict(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return out


def _write_facts(repo_root: str | Path, facts: list[Fact]) -> None:
    if is_ephemeral():
        return
    p = _facts_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(f.to_dict()) + "\n" for f in facts))
    tmp.replace(p)


def load_memory(repo_root: str | Path) -> ProjectMemory:
    """Load curated repo-local memory + stored facts. Pure read."""
    md = ""
    mf = repo_memory_file(repo_root)
    if mf.is_file():
        # errors="replace" (NOT surrogateescape): `curated_md` escapes this
        # module — it flows into render_block()'s <project_memory> block,
        # which becomes `extra_context` on every chat turn (backend.py
        # dispatch, `json=body`) and is also printed straight to the console
        # by `/memory` (chat/cmd_project.py). Both destinations require valid
        # UTF-8: httpx encodes request bodies with ensure_ascii=False then
        # `.encode("utf-8")` (httpx/_content.py:177-178), which raises
        # UnicodeEncodeError on a lone surrogate, and so does writing one to
        # stdout. A stray non-UTF-8 byte (2026-08-24 — a gzip-magic
        # `.luxe/memory.md`) must not crash session start, so read it lossily
        # here rather than losslessly — contrast `splice_block` below, whose
        # read/write pair never leaves this module and keeps surrogateescape
        # on purpose.
        md = mf.read_text(encoding="utf-8", errors="replace")
    return ProjectMemory(
        repo_root=str(repo_root),
        curated_md=md,
        facts=_read_facts(repo_root),
    )


def add_fact(
    repo_root: str | Path,
    text: str,
    *,
    kind: str = "pref",
    source: str = "auto",
    confidence: str = "auto",
) -> Fact:
    """Append a fact. Auto-captured facts default to confidence='auto' and are
    NOT injected until promoted; user-added facts may pass confidence='manual'."""
    facts = _read_facts(repo_root)
    fact = Fact(kind=kind, text=text.strip(), source=source, confidence=confidence)
    facts.append(fact)
    _write_facts(repo_root, facts)
    return fact


def promote_fact(repo_root: str | Path, fact_id: str) -> bool:
    """Flip a fact's confidence to 'manual' so it begins to be injected.
    Returns True if a matching fact was found."""
    facts = _read_facts(repo_root)
    found = False
    for f in facts:
        if f.id == fact_id:
            f.confidence = "manual"
            found = True
    if found:
        _write_facts(repo_root, facts)
    return found


def forget_fact(repo_root: str | Path, fact_id: str) -> bool:
    facts = _read_facts(repo_root)
    kept = [f for f in facts if f.id != fact_id]
    if len(kept) == len(facts):
        return False
    _write_facts(repo_root, kept)
    return True


# --- machine-managed fenced blocks in .luxe/memory.md -----------------------
#
# `memory.md` is a USER file that luxe is allowed to add to, never to own. Two
# machine-managed regions live inside it, each delimited by an HTML comment
# pair: `luxe:brief` (the `luxe init` project brief) and `luxe:notes` (session
# working notes). The markers are LOAD-BEARING — every writer re-reads the
# file, replaces only the region between its own markers, and appends at the
# END when the region is absent, so hand-written text above (the curated,
# highest-authority part) survives byte-for-byte.

_HEADER = ("<!-- luxe project memory. Hand-written notes are highest priority "
           "and are never touched by luxe; the fenced luxe:* blocks below are "
           "machine-managed. -->\n")


def block_markers(name: str) -> tuple[str, str]:
    """(begin-prefix, end-marker) for a machine-managed block."""
    return f"<!-- luxe:{name} begin", f"<!-- luxe:{name} end -->"


def _block_span(text: str, name: str) -> tuple[int, int] | None:
    """(start, end) character span of the whole block including markers, or None."""
    begin_pre, end_marker = block_markers(name)
    start = text.find(begin_pre)
    if start == -1:
        return None
    end = text.find(end_marker, start)
    if end == -1:
        return None
    return start, end + len(end_marker)


def read_block(text: str, name: str) -> str | None:
    """The BODY of a machine-managed block (markers and their lines stripped),
    or None when the block isn't present."""
    span = _block_span(text, name)
    if span is None:
        return None
    inner = text[span[0]:span[1]]
    lines = inner.splitlines()
    return "\n".join(lines[1:-1]).strip()


def splice_block(repo_root: str | Path, name: str, body: str, *,
                 stamp: str = "") -> Path:
    """Write `body` into the `luxe:<name>` block of `<repo>/.luxe/memory.md`.

    Replaces the block in place if it exists, else APPENDS it at end of file —
    curated text stays first, which is also `render_block`'s truncation
    priority. Everything outside the block is preserved byte-for-byte. Creates
    the file (with a one-line header comment) when absent. Never touches
    `facts.jsonl`. Returns the path written.

    Ephemeral sessions skip the write and return the path unwritten — the
    caller only uses it to report where the note landed, and every caller
    (session-end notes, `/note`, `/init`) is best-effort by contract.
    """
    path = repo_memory_file(repo_root)
    if is_ephemeral():
        return path
    # errors="surrogateescape" (read AND write, matched) — deliberately NOT
    # errors="replace" here, unlike load_memory/_read_facts above. This text
    # never escapes the function: it is read, spliced, and written straight
    # back to the SAME file, so byte-exact round-tripping is correct and
    # required — errors="replace" would instead permanently rewrite an
    # undecodable byte as literal U+FFFD on the very next splice, corrupting
    # a file that was merely unusual, not broken. (2026-08-24 — a memory.md
    # was found starting with gzip magic 0x1f8b.)
    existing = (path.read_text(encoding="utf-8", errors="surrogateescape")
                if path.is_file() else "")
    begin_pre, end_marker = block_markers(name)
    begin = f"{begin_pre}{(' ' + stamp) if stamp else ''} -->"
    block = f"{begin}\n{body.strip()}\n{end_marker}"

    span = _block_span(existing, name)
    if span is not None:
        new = existing[:span[0]] + block + existing[span[1]:]
    else:
        head = existing if existing else _HEADER
        sep = "" if head.endswith("\n\n") else ("\n" if head.endswith("\n") else "\n\n")
        new = f"{head}{sep}\n{block}\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    # Matches the read's errors= above: encoding surrogate-escaped codepoints
    # back with "strict" would raise UnicodeEncodeError on the very bytes we
    # just promised to preserve.
    tmp.write_text(new, encoding="utf-8", errors="surrogateescape")
    tmp.replace(path)
    return path


def render_block(memory: ProjectMemory, *, max_chars: int = 4000) -> str:
    """Render the `<project_memory>` context block, or "" when empty.

    Order inside the block: curated markdown first (highest authority), then
    promoted facts. Capped at `max_chars` to bound prompt growth.
    """
    if memory.is_empty():
        return ""
    parts: list[str] = []
    md = memory.curated_md.strip()
    if md:
        parts.append(md)
    inj = memory.injected_facts
    if inj:
        lines = [f"- ({f.kind}) {f.text}" for f in inj if f.text]
        if lines:
            parts.append("\n".join(lines))
    body = "\n\n".join(parts).strip()
    if not body:
        return ""
    if len(body) > max_chars:
        body = body[: max_chars - 1].rstrip() + "…"
    return f"<project_memory>\n{body}\n</project_memory>"
