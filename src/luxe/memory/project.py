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

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def memory_root() -> Path:
    return Path.home() / ".luxe" / "memory"


def project_hash(repo_root: str | Path) -> str:
    abs_path = str(Path(repo_root).resolve())
    return hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:12]


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
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Fact.from_dict(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return out


def _write_facts(repo_root: str | Path, facts: list[Fact]) -> None:
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
        md = mf.read_text(encoding="utf-8")
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
