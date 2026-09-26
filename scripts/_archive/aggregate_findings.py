#!/usr/bin/env python3
"""Cross-repo findings deep-dive over the swept gitreview reports (read-only).

Mines ~/.luxe/reports/*/gitreview-*.md (the canonical store — NEVER the throwaway
sweep-clones) and emits two durable artifacts:

  scripts/out/cross_repo_findings.md  — findings ranked by severity across all
      repos (Critical/High first), a RECURRENCE section (issues that appear in
      multiple repos — often more actionable than a one-off Critical), per-repo
      counts, and a clean-repo list.
  scripts/out/report_corpus.csv       — one row per report (repo, head, model,
      mode, chunks, n_passes, total_wall_s, report_bytes, severity counts) — the
      durable prompt-performance dataset that survives the sweep-clone cleanup.

Parsing: the structured xref.json digest is consumed first when it carries data,
but in practice findings live in the rendered markdown (the deterministic-render
path leaves provisional_findings empty AND the `**Findings: N**` count line is
unreliable — it can say 0 while listing findings). So we parse the markdown body
and COUNT actual entries, supplementing with xref when populated. Every finding
keeps its source report path so you can jump back.
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

REPORTS = Path.home() / ".luxe" / "reports"
OUT = Path(__file__).resolve().parent / "out"
DIGEST_MD = OUT / "cross_repo_findings.md"
CORPUS_CSV = OUT / "report_corpus.csv"

_SEV_FROM_LETTER = {"C": "critical", "H": "high", "M": "medium", "L": "low"}
_SEV_FROM_WORD = {"critical": "critical", "high": "high", "medium": "medium",
                  "low": "low"}
_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_SECTION_RE = re.compile(r"^#{2,}\s*(Critical|High|Medium|Low)\b", re.IGNORECASE)
# A finding is anchored on its **File:** citation — shared by BOTH the LLM-synthesis
# format (`- **File:** `x`, line N`) and the deterministic `## Area:` format
# (`**File:** `x`` then `**Line:** N`).
_FILE_RE = re.compile(r"^\s*[-*]?\s*\*\*File\*?\*?:?\*?\*?\s*(.+)", re.IGNORECASE)
# **H1 — title**  /  **C2 - title**  (letter-coded header, format 1)
_LETTER_RE = re.compile(r"^\s*\*\*([CHML])\d*\s*[—–:\-]\s*(.+?)\*\*", re.IGNORECASE)
# **Medium severity: title** / **High — title** (word-coded header, format 1)
_WORD_RE = re.compile(
    r"^\s*\*\*(Critical|High|Medium|Low)(?:\s+severity)?\s*[:—–\-]\s*(.+?)\*\*",
    re.IGNORECASE)
_IMPACT_RE = re.compile(r"\*\*Impact:?\*\*\s*(.+)", re.IGNORECASE)
_LINE_RE = re.compile(r"\*\*Lines?:?\*\*\s*([\d,\s–-]+)")
# `path/to/file.ext`(, line 12)  /  bare path.py:12
_LOC_RE = re.compile(
    r"`?([\w./\-]+\.[A-Za-z]{1,6})`?(?:[,:]?\s*(?:lines?\s*)?:?\s*([\d]+))?")


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    fm: dict = {}
    for line in (text[4:end] if end != -1 else "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def _norm_title(t: str) -> str:
    """Normalize a finding title for cross-repo recurrence matching."""
    t = re.sub(r"`[^`]*`", "", t.lower())          # drop inline code spans
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_NOISE_TITLES = {"bug", "issue", "issues", "vulnerability", "finding", "problem",
                 "error", "security issue"}
_NOISE_LOCS = {"file.py", "file.js", "example.py", "path/to/file", "foo.py"}


def _is_noise(f: dict) -> bool:
    """Drop degenerate template-echo findings (e.g. title 'Bug' at 'file.py:42')."""
    t = re.sub(r"[^a-z0-9 ]", "", f["title"].lower()).strip()
    loc = f["location"].split(":", 1)[0]
    return t in _NOISE_TITLES or loc in _NOISE_LOCS or len(t) < 4


def _first_location(block: str) -> str:
    for line in block.splitlines():
        if "file" in line.lower() or "`" in line or re.search(r"\.\w+:\d", line):
            m = _LOC_RE.search(line)
            if m:
                return f"{m.group(1)}:{m.group(2)}" if m.group(2) else m.group(1)
    return ""


def parse_report(md_path: Path, repo: str) -> list[dict]:
    """Extract findings from a rendered gitreview report, format-agnostically.

    A finding is anchored on its **File:** citation (present in both the LLM-
    synthesis `**H1 — title**` format and the deterministic `## Area:` format).
    severity ← nearest severity section; title ← a `**H#/Word**` header just above,
    else the `**Impact:**` line just below, else the file basename; location ←
    the file path + a `**Line(s):**`/inline line number."""
    text = md_path.read_text(errors="ignore")
    body = text.split("\n---\n", 1)[-1]
    lines = body.splitlines()
    findings: list[dict] = []
    section_sev = ""
    for i, line in enumerate(lines):
        sec = _SECTION_RE.match(line)
        if sec:
            section_sev = sec.group(1).lower()
            continue
        fm = _FILE_RE.match(line)
        if not fm:
            continue
        rest = fm.group(1)
        locm = _LOC_RE.search(rest)
        if not locm:
            continue
        path, lineno = locm.group(1), locm.group(2)
        # title: a header in the 3 lines above, else **Impact:** in the 10 below
        title = ""
        for up in lines[max(0, i - 3):i]:
            h = _LETTER_RE.match(up) or _WORD_RE.match(up)
            if h:
                title = h.group(h.lastindex).strip()
        below = "\n".join(lines[i:i + 10])
        if not title:
            im = _IMPACT_RE.search(below)
            title = (im.group(1).strip() if im else "").rstrip(".")
        if not lineno:
            lm = _LINE_RE.search(below)
            if lm:
                lineno = re.search(r"\d+", lm.group(1)).group(0)
        title = title or Path(path).name
        loc = f"{path}:{lineno}" if lineno else path
        f = {"repo": repo, "severity": section_sev or "medium",
             "title": title[:120], "location": loc, "source": str(md_path)}
        if not _is_noise(f):
            findings.append(f)
    return findings


def xref_findings(work_dir: Path, repo: str, src: str) -> list[dict]:
    """Supplement from a populated xref.json digest (often empty in practice)."""
    out: list[dict] = []
    xj = work_dir / "xref.json"
    if not xj.is_file():
        return out
    try:
        d = json.loads(xj.read_text())
    except (ValueError, OSError):
        return out
    for f in d.get("provisional_findings", []) or []:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity", "")).lower()
        sev = next((s for s in _SEV_RANK if s in sev), "medium")
        title = (f.get("title") or f.get("root_cause") or "").strip()
        if title:
            ev = (f.get("evidence") or [""])[0]
            rec = {"repo": repo, "severity": sev, "title": title,
                   "location": str(ev), "source": src}
            if not _is_noise(rec):
                out.append(rec)
    return out


def newest(paths: list[Path]) -> Path | None:
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    all_findings: list[dict] = []
    corpus: list[dict] = []
    clean: list[str] = []

    for rdir in sorted(REPORTS.iterdir()):
        if not rdir.is_dir():
            continue
        reps = list(rdir.glob("gitreview-*.md"))
        rep = newest(reps)
        if not rep:
            continue
        fm = parse_frontmatter(rep.read_text(errors="ignore"))
        repo = Path(fm.get("repo", str(rdir))).name or rdir.name
        head = fm.get("head", "")
        # Among reports at the newest HEAD, use the RICHEST (a later clean re-run
        # at the same commit shouldn't hide a findings-rich run of that commit).
        same_head = [r for r in reps
                     if parse_frontmatter(r.read_text(errors="ignore")).get("head", "") == head] or [rep]
        finds = max((parse_report(r, repo) for r in same_head), key=len, default=[])
        if not finds:  # try the digest before declaring clean
            work = newest(list(rdir.glob("gitreview-*.work")))
            if work:
                finds = xref_findings(work, repo, str(rep))
        all_findings.extend(finds)
        counts = defaultdict(int)
        for f in finds:
            counts[f["severity"]] += 1
        corpus.append({
            "repo": repo, "head": fm.get("head", ""), "model": fm.get("model", ""),
            "mode": fm.get("mode", "single"), "chunks": fm.get("chunks", ""),
            "n_passes": fm.get("n_passes", ""),
            "total_wall_s": fm.get("total_wall_s", ""),
            "report_bytes": rep.stat().st_size,
            "n_critical": counts["critical"], "n_high": counts["high"],
            "n_medium": counts["medium"], "n_low": counts["low"]})
        if not finds:
            clean.append(repo)

    # --- recurrence: titles appearing across multiple repos ------------------
    by_norm: dict[str, list[dict]] = defaultdict(list)
    for f in all_findings:
        by_norm[_norm_title(f["title"])].append(f)
    recurring = {k: v for k, v in by_norm.items()
                 if len({f["repo"] for f in v}) >= 2}

    # --- write corpus CSV ----------------------------------------------------
    cols = ["repo", "head", "model", "mode", "chunks", "n_passes", "total_wall_s",
            "report_bytes", "n_critical", "n_high", "n_medium", "n_low"]
    with CORPUS_CSV.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for row in sorted(corpus, key=lambda r: (-int(r["n_critical"] or 0),
                                                 -int(r["n_high"] or 0))):
            w.writerow(row)

    # --- write digest MD -----------------------------------------------------
    lines = ["# Cross-repo gitreview findings", ""]
    tot = len(all_findings)
    by_sev = defaultdict(list)
    for f in all_findings:
        by_sev[f["severity"]].append(f)
    lines.append(f"**{tot} findings across {len(corpus)} reports** — "
                 + ", ".join(f"{len(by_sev[s])} {s}" for s in
                             ("critical", "high", "medium", "low")) + ".")
    lines.append("")
    if recurring:
        lines += ["## Recurring across repos", ""]
        for k, v in sorted(recurring.items(),
                           key=lambda kv: -len({f["repo"] for f in kv[1]})):
            repos = sorted({f["repo"] for f in v})
            worst = min((f["severity"] for f in v), key=lambda s: _SEV_RANK[s])
            lines.append(f"- **[{worst}]** {v[0]['title']} — {len(repos)} repos: "
                         f"{', '.join(repos)}")
        lines.append("")
    for sev in ("critical", "high", "medium", "low"):
        items = sorted(by_sev[sev], key=lambda f: f["repo"])
        if not items:
            continue
        lines += [f"## {sev.capitalize()} ({len(items)})", ""]
        for f in items:
            loc = f" `{f['location']}`" if f["location"] else ""
            lines.append(f"- **{f['repo']}**{loc} — {f['title']}")
        lines.append("")
    if clean:
        lines += ["## Clean (no parsed findings)", "",
                  ", ".join(sorted(clean)), ""]
    DIGEST_MD.write_text("\n".join(lines))

    # --- console summary -----------------------------------------------------
    print(f"· {tot} findings across {len(corpus)} reports "
          f"({len(by_sev['critical'])}C {len(by_sev['high'])}H "
          f"{len(by_sev['medium'])}M {len(by_sev['low'])}L); "
          f"{len(recurring)} recurring; {len(clean)} clean")
    print(f"· digest → {DIGEST_MD}")
    print(f"· corpus → {CORPUS_CSV}")
    for sev in ("critical", "high"):
        for f in sorted(by_sev[sev], key=lambda f: f["repo"]):
            loc = f" [{f['location']}]" if f["location"] else ""
            print(f"  {sev.upper():<8} {f['repo']}{loc}: {f['title'][:80]}")


if __name__ == "__main__":
    main()
