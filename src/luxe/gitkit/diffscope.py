"""gitkit diff scope — deterministic helpers for `gitaudit --base/--pr`.

Everything here is plain Python over `git`/`gh` subprocess output (the health.py
precedent — never the agent tool surface): merge-base resolution, changed-file
enumeration → `FileRec`s, per-file hunk extraction, the `<change_diff>` data
block (token-capped, truncation announced), the hunk-overlap classification
PRIOR, and the PR → base-ref glue.

Classification honesty (gitkit.sdd): the model CANNOT reliably prove a finding
was introduced by a change. The tag vocabulary is `likely-introduced` vs
`pre-existing (touched code)` — never a bare "introduced" — and the report
header carries a fixed caveat line. The hunk-overlap test here is the
deterministic prior: a finding whose file:line falls OUTSIDE every changed hunk
can never stay tagged likely-introduced (the model's tag refines, not invents).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from luxe.context import estimate_tokens
from luxe.gitkit.health import _run_gh, _run_git

# The <change_diff> block may occupy at most this fraction of the window.
DIFF_BUDGET_FRAC = 0.25
_DIFF_CONTEXT_LINES = 10
_DIFF_TIMEOUT = 60

TAG_LIKELY = "likely-introduced"
TAG_PREEXISTING = "pre-existing (touched code)"
# Fixed report-header caveat (deterministically ensured, never model-trusted).
CAVEAT_LINE = ("*Classification is heuristic — `likely-introduced` vs "
               "`pre-existing (touched code)` is based on hunk overlap, "
               "not proof.*")
_TRUNCATION_NOTICE = ("[change_diff truncated at the token cap — read the "
                      "changed files with tools for full context]")

# first `path.ext:NN` / `path.ext line NN` ref on a finding line
_REF_RE = re.compile(
    r"(?P<path>[\w./-]+\.[A-Za-z]\w{0,3})[:\s]+(?:line\s+)?(?P<line>\d+)")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def resolve_base_ref(repo: str | Path, ref: str) -> str | None:
    """Resolve `ref` to a commit sha — as given first, then `origin/<ref>` (a
    plain branch name the user only has as a remote-tracking ref)."""
    for cand in (ref, f"origin/{ref}"):
        ok, out = _run_git(["rev-parse", "--verify", "--quiet",
                            f"{cand}^{{commit}}"], repo)
        if ok and out:
            return cand
    return None


def merge_base(repo: str | Path, base_ref: str) -> str | None:
    """`git merge-base <base> HEAD`, or None when unresolvable."""
    ok, out = _run_git(["merge-base", base_ref, "HEAD"], repo)
    return out.strip() if ok and out.strip() else None


def changed_files(repo: str | Path, mb: str) -> list[str]:
    """Surviving changed files vs the merge-base (renames followed via -M; the
    NEW path is kept, deletions are dropped — there is nothing left to audit)."""
    ok, out = _run_git(["diff", "--name-status", "-M", f"{mb}..HEAD"], repo,
                       timeout=_DIFF_TIMEOUT)
    if not ok:
        return []
    files: list[str] = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("D"):
            continue
        # renames/copies: "R100\told\tnew" — audit the surviving (new) path
        files.append(parts[-1])
    return files


def diff_stats(repo: str | Path, mb: str) -> tuple[int, int, int]:
    """(changed files, added lines, deleted lines) via numstat (binary files
    count as 0/0)."""
    ok, out = _run_git(["diff", "--numstat", "-M", f"{mb}..HEAD"], repo,
                       timeout=_DIFF_TIMEOUT)
    if not ok:
        return 0, 0, 0
    n = adds = dels = 0
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        n += 1
        if parts[0] != "-":
            adds += int(parts[0])
        if parts[1] != "-":
            dels += int(parts[1])
    return n, adds, dels


def file_recs(repo: str | Path, files: list[str]):
    """Changed surviving files → deep.FileRec list (the deep chunker's input).
    Files that vanished or have an unrecognized language are skipped."""
    from luxe.gitkit.deep import _CHARS_PER_TOKEN, FileRec, _file_priority
    from luxe.repo_index import _count_lines, _detect_language

    root = Path(repo).resolve()
    recs: list[FileRec] = []
    for rel in files:
        p = root / rel
        lang = _detect_language(p.suffix)
        if lang is None or not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        top = rel.split("/", 1)[0] if "/" in rel else "."
        recs.append(FileRec(
            rel=rel, language=lang, loc=_count_lines(p), bytes=size,
            tokens=max(1, size // _CHARS_PER_TOKEN), top_dir=top,
            priority=_file_priority(rel, set()),
        ))
    return recs


def changed_hunks(repo: str | Path, mb: str) -> dict[str, list[tuple[int, int]]]:
    """Per-file NEW-side line ranges of every changed hunk (U0 → exact spans)."""
    ok, out = _run_git(["diff", "-U0", "-M", f"{mb}..HEAD"], repo,
                       timeout=_DIFF_TIMEOUT)
    if not ok:
        return {}
    hunks: dict[str, list[tuple[int, int]]] = {}
    cur: str | None = None
    for ln in out.splitlines():
        if ln.startswith("+++ "):
            path = ln[4:].strip()
            cur = None if path == "/dev/null" else path.removeprefix("b/")
            continue
        m = _HUNK_RE.match(ln)
        if m and cur:
            start = int(m.group("start"))
            count = int(m.group("count") or "1")
            if count > 0:
                hunks.setdefault(cur, []).append((start, start + count - 1))
    return hunks


def in_changed_hunk(hunks: dict[str, list[tuple[int, int]]],
                    path: str, line: int) -> bool:
    for lo, hi in hunks.get(path, []):
        if lo <= line <= hi:
            return True
    return False


def change_diff_block(repo: str | Path, mb: str, *, base_label: str,
                      max_tokens: int, files: list[str] | None = None) -> str:
    """The `<change_diff>` data block: `git diff -U10 <mb>..HEAD` (optionally
    scoped to `files` for a chunk), token-capped with an explicit truncation
    notice. Pure data — the directive lives in GIT_AUDIT_DIFF_* hints."""
    args = ["diff", f"-U{_DIFF_CONTEXT_LINES}", "-M", f"{mb}..HEAD"]
    if files:
        args += ["--", *files]
    ok, diff = _run_git(args, repo, timeout=_DIFF_TIMEOUT)
    if not ok:
        diff = f"(diff unavailable: {diff})"
    n, adds, dels = diff_stats(repo, mb)
    truncated = False
    if estimate_tokens(diff) > max_tokens:
        diff = diff[:max(0, max_tokens * 4)].rsplit("\n", 1)[0]
        truncated = True
    scope = f", scoped to {len(files)} file(s)" if files else ""
    parts = [
        "<change_diff>",
        f"Base: {base_label} (merge-base {mb[:8]}) — {n} files, "
        f"+{adds}/−{dels}{scope}",
        diff,
    ]
    if truncated:
        parts.append(_TRUNCATION_NOTICE)
    parts.append("</change_diff>")
    return "\n".join(parts)


def pr_base_ref(repo: str | Path, pr_number: int) -> tuple[str | None, str]:
    """Resolve a PR number to its base ref via `gh`. Returns (base_ref, "") on
    success; (None, why) on failure — the message names the ACTUAL failure
    class (gh missing / network or auth / PR not found) before suggesting
    --base, never a generic shrug."""
    ok, out = _run_gh(["pr", "view", str(pr_number), "--json", "baseRefName"],
                      repo)
    if ok:
        try:
            base = str(json.loads(out).get("baseRefName") or "")
        except ValueError:
            base = ""
        if base:
            return base, ""
        return None, (f"gh returned no base branch for PR #{pr_number} "
                      f"(output: {out[:200]}). Use --base <ref> instead.")
    low = out.lower()
    if "not installed" in low:
        why = "the `gh` CLI is not installed"
    elif "timed out" in low:
        why = "gh timed out (network?)"
    elif ("could not resolve" in low or "no pull requests" in low
          or "not found" in low or "no default remote" in low):
        why = f"PR #{pr_number} was not found on this repo's remote"
    elif "auth" in low or "401" in low or "403" in low or "log in" in low:
        why = "gh is not authenticated (`gh auth login`)"
    else:
        why = "gh failed"
    return None, f"--pr {pr_number} failed: {why} ({out.strip()[:200]}). " \
                 "Use --base <ref> to audit against a local ref instead."


# --- deterministic report post-processing ------------------------------------

def header_line(base_label: str, mb: str, n: int, adds: int, dels: int) -> str:
    return f"**Base: {base_label} (merge-base {mb[:8]}) — {n} files, +{adds}/−{dels}**"


def ensure_header(report: str, base_label: str, mb: str,
                  stats: tuple[int, int, int]) -> str:
    """Deterministically guarantee the `**Base: …**` line and the fixed caveat
    line right under the `# Diff audit` title (insert when the model omitted
    them; never duplicate)."""
    lines = (report or "").splitlines()
    if not lines:
        return report
    head = "\n".join(lines[:6])
    inserts: list[str] = []
    if "**Base:" not in head:
        inserts.append(header_line(base_label, mb, *stats))
    if "hunk overlap" not in head.lower():
        inserts.append(CAVEAT_LINE)
    if not inserts:
        return report
    # insert after the title line (or any existing **Base:** line beneath it)
    pos = 1
    while pos < len(lines) and lines[pos].strip().startswith("**Base:"):
        pos += 1
    return "\n".join(lines[:pos] + inserts + lines[pos:])


def apply_tag_priors(report: str,
                     hunks: dict[str, list[tuple[int, int]]]) -> str:
    """Render the hunk-overlap prior onto the findings section: a finding line
    whose first file:line ref falls OUTSIDE every changed hunk can never stay
    `likely-introduced` (rewritten to the pre-existing tag), and an untagged
    finding line gets the prior's default. Only the `## Bugs & security`
    section is touched; the model's tag refines, never invents."""
    out: list[str] = []
    in_findings = False
    for ln in (report or "").splitlines():
        if ln.startswith("## "):
            in_findings = ln.lower().startswith("## bugs")
        if not in_findings:
            out.append(ln)
            continue
        m = _REF_RE.search(ln)
        is_finding_line = bool(m) and (
            ln.lstrip().startswith(("-", "*")) or ln.lstrip()[:3].rstrip(". )").isdigit()
            or "**" in ln)
        if not is_finding_line:
            out.append(ln)
            continue
        inside = in_changed_hunk(hunks, m.group("path"), int(m.group("line")))
        if TAG_LIKELY in ln:
            if not inside:
                ln = ln.replace(f"**{TAG_LIKELY}**", f"**{TAG_PREEXISTING}**") \
                    if f"**{TAG_LIKELY}**" in ln else \
                    ln.replace(TAG_LIKELY, TAG_PREEXISTING)
        elif TAG_PREEXISTING not in ln:
            tag = TAG_LIKELY if inside else TAG_PREEXISTING
            ln = f"{ln.rstrip()} — **{tag}**"
        out.append(ln)
    return "\n".join(out)
