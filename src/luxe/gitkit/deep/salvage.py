"""gitkit deep mode — recovery of findings from output the model did not
package: the report-header check, the ramble detector, the transcription
passes (report format / gitchange plan extract), and the deterministic
heuristic finding-line salvage. The champion will not self-package, so
detection and packaging are separate (gitkit.sdd) — this is the packaging."""

from __future__ import annotations

import re

from luxe.agents import prompts
from luxe.gitkit import patterns


def _has_report_header(text: str, kind: str) -> bool:
    """True if the chunk output concluded with the kind's required markdown report
    header (so its findings can be recovered by slicing even without JSON)."""
    from luxe.gitkit.runner import _TITLES
    title = _TITLES.get(kind, "")
    if not title or not text:
        return False
    return re.search(rf"^#\s+{re.escape(title)}\s*$", text,
                     re.MULTILINE | re.IGNORECASE) is not None


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


# Heuristic finding patterns for the deterministic-render fallback (review).
# A severity WORD (leading \b: "flow"/"allow"/"below" are not `low`) followed
# by a code span or a file:line ref.
_SEV_LINE_RE = re.compile(
    r"\b(critical|high|medium|low)\b.*?(`[^`]+`|" + patterns.FILE_LINE_RE.pattern
    + ")", re.IGNORECASE)
# Additional finding shapes the champion actually emits when it rambles past the
# report header (offline-recovery analysis 2026-06-08, scripts/recover_offline.py):
# numbered BOLD list items carrying a file/line/code ref, and canonical report
# bullets. Keyed on the FINDING shape (numbered+bold, or a labelled bullet) so plain
# exploration narrative ("Let me look at cli.py:29") is not swept in. This lifted
# heuristic salvage on captured unparsed dumps from ~2% to ~64% at zero model cost.
_NUM_BOLD_RE = re.compile(r"^\s*\d+[.)]\s+\*\*")
_REPORT_BULLET_RE = re.compile(
    r"\*\*\s*(file|issue|bug|severity|line|impact|fix|problem|risk|location)\b", re.I)
_FILE_LINE_RE = patterns.FILE_LINE_RE
_BOLD_FILE_RE = re.compile(
    r"\*\*[^*]*?(?:\.(?:" + patterns.EXT_ALT + r")\b|line\s+\d+)[^*]*?\*\*", re.I)
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
_SEV_WORD_RE = patterns.SEV_WORD_RE
_FINDING_HEADING_RE = re.compile(r"^#{3,4}\s+\S")
_FILE_REF_RE = patterns.FILE_REF_RE


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
