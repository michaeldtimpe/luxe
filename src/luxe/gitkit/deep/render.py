"""gitkit deep mode — pure-data `extra_context` block builders (framing,
chunk files, cross-reference digest, aggregated notes, prior findings) and
the deterministic report assembly used when the synthesis will not come back
clean."""

from __future__ import annotations

import json
import re

from luxe.context import estimate_tokens
from luxe.gitkit.deep.chunking import Chunk
from luxe.gitkit.deep.digest import _SEVERITY_RANK, _sev_rank, confidence_of
from luxe.gitkit.deep.salvage import _heuristic_findings


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


_INDEX_LINE_CHARS = 160


def _index_entries(digest: dict) -> list[str]:
    """One line per earlier finding — severity, title, first file:line — for
    the chunk-pass cross-reference. Structured findings first (severity
    desc), then the finding-shaped lines of each earlier markdown note."""
    out: list[str] = []
    for f in sorted(digest.get("provisional_findings", []), key=_sev_rank,
                    reverse=True):
        ev = (f.get("evidence") or [""])[0]
        line = (f"[{f.get('severity') or '?'}] {f.get('title', '')}"
                + (f" — {ev}" if ev else "") + f" (chunk {f.get('chunk', 0) + 1})")
        out.append(line[:_INDEX_LINE_CHARS])
    for n in digest.get("markdown_notes", []):
        for item in _heuristic_findings(n.get("md", ""), cap=30):
            item = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", item)
            out.append(f"{item} (chunk {n.get('chunk', 0) + 1})"[:_INDEX_LINE_CHARS])
    return out


def _digest_block(digest: dict, *, max_tokens: int = 0) -> str:
    """The chunk pass's cross-reference: the structural map (modules,
    entities, cross-cutting concerns) plus a one-line INDEX of the findings
    earlier chunks recorded — never their full notes (those go to synthesis
    only; they used to be copied into every later chunk, unbounded). With
    `max_tokens`, trailing index lines are cut to fit and the cut is stated;
    the digest itself keeps everything."""
    index = _index_entries(digest)
    head = {"modules": digest.get("modules", []),
            "entities": digest.get("entities", []),
            "cross_cutting": digest.get("cross_cutting", [])}
    map_json = json.dumps(head, indent=1)
    kept: list[str] = []
    if max_tokens:
        used = estimate_tokens(map_json)
        for ln in index:
            cost = estimate_tokens(ln) + 1
            if used + cost > max_tokens:
                break
            kept.append(ln)
            used += cost
    else:
        kept = index
    body = map_json
    if kept:
        body += "\n\nFindings recorded so far (index):\n" + "\n".join(
            f"- {ln}" for ln in kept)
    if len(kept) < len(index):
        body += (f"\n(+{len(index) - len(kept)} more earlier findings not "
                 "listed here — all are kept for the final report)")
    return ("<cross_reference_digest>\nRunning map from earlier chunks (use it to "
            "cross-reference; do not re-report its findings):\n"
            f"{body}\n</cross_reference_digest>")


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
    n_note_findings = 0
    for n in notes:
        body = _strip_report_header(n.get("md", ""))
        if body:
            head = (f"## Area: {n.get('label', '?')} "
                    f"(chunk {n.get('chunk', 0) + 1})")
            if n.get("source") == "heuristic":
                head += ("\n\n*(heuristic salvage from verbose model output — "
                         "confidence: low)*")
            sections.append(f"{head}\n\n{body}")
            n_note_findings += len(_heuristic_findings(body))
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
    # pf findings + the finding lines of the NOTE sections (the Additional
    # findings section renders pf itself — counting it again doubled pf)
    n = len(pf) + n_note_findings
    header = f"# {title}\n**Findings: {n} (consolidated across chunks)**"

    out = [header, *sections]
    if unparsed:
        out.append("## Coverage gaps\n\nThese areas could not be analyzed "
                   "(verbose or empty model output) and may still contain issues:\n"
                   + "\n".join(f"- {u}" for u in unparsed))
    if not sections and not unparsed:
        out.append("No findings were recorded.")
    return "\n\n".join(out)


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
