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

import codecs
import json
import re
from pathlib import Path

from luxe import gitcmd
from luxe.context import estimate_tokens
from luxe.gitkit import patterns
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

# first `path.ext:NN` / `path.ext line NN` ref on a finding line — the shared
# definition (a known source extension, so `requests.get 5` is not a ref)
_REF_RE = patterns.FILE_LINE_RE
# Every diff whose TEXT is parsed here pins the same host config the grading
# parsers do (`diff.noprefix` / `mnemonicprefix` rewrite the `+++ b/<path>`
# header this module keys on), plus `core.quotePath=false`: git C-quotes a
# non-ASCII path by default ("src/caf\303\251.py"), which then matched no
# changed file and silently fell out of the audit. `diff.srcPrefix` /
# `dstPrefix` (git >= 2.45) rewrite the same header.
_DIFF_PINS = (*gitcmd.DIFF_PARSE_PINS, "-c", "core.quotePath=false",
              "-c", "diff.srcPrefix=a/", "-c", "diff.dstPrefix=b/")
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


def _git_diff(repo: str | Path, *args: str) -> tuple[bool, str]:
    """`git diff …` with the parse pins and no external diff driver."""
    return _run_git([*_DIFF_PINS, "diff", "--no-ext-diff", *args], repo,
                    timeout=_DIFF_TIMEOUT)


def _unquote(path: str) -> str:
    """Undo git's C-style quoting (still applied, even with quotePath=false,
    to names holding a quote, backslash or control character)."""
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        raw = codecs.escape_decode(path[1:-1].encode("utf-8"))[0]
        return raw.decode("utf-8", errors="replace")
    return path


def merge_base(repo: str | Path, base_ref: str) -> str | None:
    """`git merge-base <base> HEAD`, or None when unresolvable."""
    ok, out = _run_git(["merge-base", base_ref, "HEAD"], repo)
    return out.strip() if ok and out.strip() else None


def changed_files(repo: str | Path, mb: str) -> list[str]:
    """Surviving changed files vs the merge-base (renames followed via -M; the
    NEW path is kept, deletions are dropped — there is nothing left to audit)."""
    # -z: NUL-separated and never quoted — "STATUS\0path\0" or, for a
    # rename/copy, "R100\0old\0new\0".
    ok, out = _git_diff(repo, "--name-status", "-z", "-M", f"{mb}..HEAD")
    if not ok:
        return []
    toks = out.split("\0")
    files: list[str] = []
    i = 0
    while i < len(toks):
        status = toks[i].strip()
        i += 1
        if not status:
            continue
        n_paths = 2 if status[:1] in ("R", "C") else 1
        paths = toks[i:i + n_paths]
        i += n_paths
        if status.startswith("D") or not paths:
            continue
        # renames/copies: audit the surviving (new) path
        files.append(paths[-1])
    return files


def diff_stats(repo: str | Path, mb: str) -> tuple[int, int, int]:
    """(changed files, added lines, deleted lines) via numstat (binary files
    count as 0/0)."""
    ok, out = _git_diff(repo, "--numstat", "-M", f"{mb}..HEAD")
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
    Files that vanished or have an unrecognized language are skipped; nothing
    is pruned (a change inside a vendored dir is still part of the change)."""
    from luxe.gitkit.deep import file_recs_for
    return file_recs_for(repo, files, prune=False)


def changed_hunks(repo: str | Path, mb: str) -> dict[str, list[tuple[int, int]]]:
    """Per-file NEW-side line ranges of every changed hunk (U0 → exact spans)."""
    ok, out = _git_diff(repo, "-U0", "-M", f"{mb}..HEAD")
    if not ok:
        return {}
    hunks: dict[str, list[tuple[int, int]]] = {}
    cur: str | None = None
    for ln in out.splitlines():
        if ln.startswith("+++ "):
            path = _unquote(ln[4:].rstrip("\t").strip())
            cur = None if path == "/dev/null" else path.removeprefix("b/")
            continue
        m = _HUNK_RE.match(ln)
        if m and cur:
            start = int(m.group("start"))
            count = int(m.group("count") or "1")
            if count > 0:
                hunks.setdefault(cur, []).append((start, start + count - 1))
    return hunks


def _resolve_hunk_path(hunks: dict[str, list[tuple[int, int]]],
                       path: str) -> str | None:
    """Map a finding's cited path onto a changed file. Exact (after dropping
    `./`, quotes and backticks) first; else the UNIQUE changed file the cited
    path is a suffix of (`pkg/mod.py` → `src/pkg/mod.py`), else the UNIQUE
    changed file with that basename (`mod.py`). Ambiguous → None: a guess
    that picks the wrong file would invent a classification."""
    p = path.strip().strip("`'\"")
    while p.startswith("./"):
        p = p[2:]
    if p in hunks:
        return p
    suffix = [k for k in hunks if k.endswith("/" + p)]
    if len(suffix) == 1:
        return suffix[0]
    if "/" not in p:
        base = [k for k in hunks if k.rsplit("/", 1)[-1] == p]
        if len(base) == 1:
            return base[0]
    return None


def in_changed_hunk(hunks: dict[str, list[tuple[int, int]]],
                    path: str, line: int) -> bool:
    key = _resolve_hunk_path(hunks, path)
    for lo, hi in hunks.get(key, []) if key is not None else []:
        if lo <= line <= hi:
            return True
    return False


def file_diffs(repo: str | Path, mb: str) -> dict[str, str] | None:
    """The whole `-U10` diff fetched ONCE and split per file (in git's order),
    so per-chunk `<change_diff>` blocks are slices of it instead of one git
    diff (+ one numstat) per chunk. None when git failed."""
    ok, diff = _git_diff(repo, f"-U{_DIFF_CONTEXT_LINES}", "-M", f"{mb}..HEAD")
    if not ok:
        return None
    out: dict[str, str] = {}
    sections: list[list[str]] = []
    for ln in diff.splitlines():
        if ln.startswith("diff --git ") or not sections:
            sections.append([])
        sections[-1].append(ln)
    for sec in sections:
        path = None
        for ln in sec[1:12]:
            if ln.startswith("+++ ") and ln[4:].strip() != "/dev/null":
                path = _unquote(ln[4:].rstrip("\t").strip()).removeprefix("b/")
                break
            if ln.startswith("rename to "):
                path = _unquote(ln[len("rename to "):].strip())
                break
        if path is None:
            for ln in sec[1:12]:
                if ln.startswith("--- ") and ln[4:].strip() != "/dev/null":
                    path = _unquote(ln[4:].rstrip("\t").strip()).removeprefix("a/")
                    break
        if path is None and sec and sec[0].startswith("diff --git "):
            path = sec[0].rsplit(" b/", 1)[-1]
        if path is not None:
            out[path] = (out[path] + "\n" if path in out else "") + "\n".join(sec)
    return out


def change_diff_block(repo: str | Path, mb: str, *, base_label: str,
                      max_tokens: int, files: list[str] | None = None,
                      stats: tuple[int, int, int] | None = None,
                      per_file: dict[str, str] | None = None) -> str:
    """The `<change_diff>` data block: `git diff -U10 <mb>..HEAD` (optionally
    scoped to `files` for a chunk), token-capped with an explicit truncation
    notice. Pure data — the directive lives in GIT_AUDIT_DIFF_* hints.
    Callers building one block per chunk pass the precomputed `stats` and
    `per_file` (`file_diffs`) so git runs once per audit, not per chunk."""
    if per_file is not None:
        want = set(files) if files else None
        diff = "\n".join(text for path, text in per_file.items()
                         if want is None or path in want)
    else:
        args = [f"-U{_DIFF_CONTEXT_LINES}", "-M", f"{mb}..HEAD"]
        if files:
            args += ["--", *files]
        ok, diff = _git_diff(repo, *args)
        if not ok:
            diff = f"(diff unavailable: {diff})"
    n, adds, dels = stats if stats is not None else diff_stats(repo, mb)
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


# Sections whose lines are NOT findings (structure advice, coverage notes,
# summaries) — the prior never tags them.
_NON_FINDING_SECTION_RE = re.compile(
    r"^#{1,4}\s.*\b(structur|refactor|coverage gaps|summary|files checked|"
    r"no findings)", re.IGNORECASE)


def apply_tag_priors(report: str,
                     hunks: dict[str, list[tuple[int, int]]]) -> str:
    """Render the hunk-overlap prior onto every finding-shaped line: one whose
    first file:line ref falls OUTSIDE every changed hunk can never stay
    `likely-introduced` (rewritten to the pre-existing tag), and an untagged
    finding line gets the prior's default. Applies in every findings-bearing
    section — the model's `## Bugs & security` AND the Python-rendered deep
    report's `## Area: …` / `## Additional findings` (which it used to skip)
    — but never in structural / coverage / summary sections or inside code
    fences. The model's tag refines, never invents."""
    out: list[str] = []
    in_findings = True
    in_fence = False
    for ln in (report or "").splitlines():
        if patterns.is_fence(ln):
            in_fence = not in_fence
            out.append(ln)
            continue
        if not in_fence and ln.startswith("#"):
            level = patterns.heading_level(ln)
            if level == 1:
                in_findings = True
            elif level:
                in_findings = not _NON_FINDING_SECTION_RE.match(ln)
        if in_fence or not in_findings or ln.startswith("#"):
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
