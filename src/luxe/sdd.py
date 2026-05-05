"""SDD — `.sdd` file parser for SpecDD Lever 2.

A `.sdd` file is a plain-markdown contract that lives next to the code it
governs (`src/luxe/agents/agents.sdd` for `src/luxe/agents/`). It carries
six canonical sections:

    # Title (H1; one line)

    Optional preamble prose (free text; not parsed).

    ## Must
    - Statement 1
    - Statement 2

    ## Must not
    - Anti-pattern 1

    ## Owns
    - src/luxe/spec.py
    - src/luxe/spec_validator.py

    ## Depends on
    - src/luxe/agents/

    ## Forbids
    - tests/**
    - **/test_*.py

    ## Done when
    - Statement of completion criterion

`Owns`, `Depends on`, and `Forbids` are path globs (relative to the repo
root). `Must`, `Must not`, and `Done when` are prose statements (one
bullet per statement).

Section names are case-insensitive and tolerate `must_not` / `must-not` /
`must not`. Each section is optional — a `.sdd` with only `Forbids:` is
valid (e.g., a fixture-scoped writeguard with no positive contract).

This parser is deliberately strict-but-small: it does not interpret the
glob syntax (that's `spec_resolver.py`'s job); it does not validate that
referenced paths exist; it does not enforce ordering of sections. Its
single responsibility is to turn `.sdd` text into an `SddFile` dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


CANONICAL_SECTIONS = ("must", "must_not", "owns", "depends_on", "forbids", "done_when")


@dataclass(frozen=True)
class SddFile:
    """A parsed `.sdd` contract.

    `path` is the absolute path the file was loaded from (or a synthetic
    path for in-memory parses; tests use this). `title` is the H1 text
    or, if no H1, the file stem. The six section lists are independent;
    any may be empty.
    """

    path: Path
    title: str
    must: list[str] = field(default_factory=list)
    must_not: list[str] = field(default_factory=list)
    owns: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    forbids: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)


class SddParseError(ValueError):
    """Raised on malformed `.sdd` content. Carries `path` for context."""

    def __init__(self, message: str, path: Path | None = None) -> None:
        prefix = f"{path}: " if path is not None else ""
        super().__init__(f"{prefix}{message}")
        self.path = path


def parse_sdd(text: str, path: Path | None = None) -> SddFile:
    """Parse `.sdd` source text into an `SddFile`.

    `path` is recorded on the result and used in error messages; it does
    not need to exist on disk. Pass it for readability when parsing
    in-memory strings.
    """
    effective_path = path if path is not None else Path("<inline>")

    title = _extract_title(text, effective_path)
    sections = _split_sections(text, effective_path)

    return SddFile(
        path=effective_path,
        title=title,
        must=sections.get("must", []),
        must_not=sections.get("must_not", []),
        owns=sections.get("owns", []),
        depends_on=sections.get("depends_on", []),
        forbids=sections.get("forbids", []),
        done_when=sections.get("done_when", []),
    )


def parse_sdd_file(path: Path) -> SddFile:
    """Read a `.sdd` file from disk and parse it. Path is canonicalized."""
    resolved = path.resolve()
    if not resolved.is_file():
        raise SddParseError(f"not a file", path=resolved)
    return parse_sdd(resolved.read_text(encoding="utf-8"), path=resolved)


# --- internals ----------------------------------------------------------


def _extract_title(text: str, path: Path) -> str:
    """First H1 line wins; fall back to the filename stem.

    Preamble prose between the H1 and the first H2 is silently dropped —
    it's documentation for human readers, not parsed content.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# ") and not line.startswith("## "):
            title = line[2:].strip()
            if title:
                return title
    return path.stem


def _split_sections(text: str, path: Path) -> dict[str, list[str]]:
    """Walk lines once; collect bullets under each H2 section header.

    A "bullet" is a line whose first non-whitespace character is `-`.
    Continuation indents (subsequent lines indented under a bullet) are
    not folded — multi-line statements should fit on one line. Blank
    lines and prose between bullets are ignored.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()

        if stripped.startswith("## "):
            header = stripped[3:].strip()
            normalized = _normalize_section_name(header)
            if normalized is None:
                # Unknown section — skip silently. Future SDD versions may
                # add sections; we don't want today's parser to reject
                # tomorrow's files. Author can add anything they want; we
                # just won't surface it.
                current = None
                continue
            if normalized in seen:
                raise SddParseError(
                    f"duplicate section '{header}' (normalized: {normalized!r})",
                    path=path,
                )
            seen.add(normalized)
            current = normalized
            sections[current] = []
            continue

        if current is None:
            continue

        if stripped.startswith("- "):
            entry = stripped[2:].strip()
            if entry:
                sections[current].append(entry)

    return sections


def _normalize_section_name(header: str) -> str | None:
    """Map a header string to a canonical section name; None on miss.

    Tolerates: `Must`, `must`, `MUST`, `Must Not`, `Must not`, `must_not`,
    `must-not`, `Depends on`, `Depends_on`, `depends-on`, `Done when`,
    `done_when`, `done-when`. Whitespace and case are normalized.
    """
    canonical = (
        header.lower()
        .replace("-", "_")
        .replace(" ", "_")
        .strip("_")
    )
    if canonical in CANONICAL_SECTIONS:
        return canonical
    return None
