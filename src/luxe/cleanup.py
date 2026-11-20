"""Cleanup turn — let the model drop its own scratch files before the commit.

Default ON since 2026-10-18; `LUXE_CLEANUP_TURN=0` (exact string) disables
it, and then `maintain` never calls in here. With no candidate, no model call
is made and the tree is untouched. Why it exists: on the 2026-10
tuning pass Qwen3.8-27B verified its work by writing throwaway scripts and
running them, then left them behind (`health_check_tmp.py`, "delete after
use"), and the `orphan_file` gate failed an otherwise-correct change 3/3
(acceptance/tuning_2026_10/REPORT.md).

Split the same way gitkit's deep pipeline is: the MODEL detects (one tool-less
chat call naming DELETE/KEEP per file), PYTHON acts (deletes only paths from
the candidate list it offered — a reply can never reach another file).

Candidates are files new since `base_sha` (committed, staged, or untracked)
that are either
  - generated caches (`__pycache__/`, `*.pyc`), or
  - code/test files whose stem no other file references (`git grep -w`,
    untracked files included) — the `orphan_file` gate's second prong, or
  - data files (2026-11-20: anything that is not code, docs or config, e.g. a
    `fake.gguf` test fixture) whose file NAME no other file mentions
    (`git grep -F`). `LUXE_IDLE_NARROW=wide` committed
    `test_pe_scan/fake.gguf` in 3/3 strict-flag runs because neither prong
    above could see it (acceptance/idle_wide_rerun_2026_11).
Docs and config (`.md`, `.json`, `.yaml`, `Dockerfile`, dotfiles, …) are never
candidates: document tasks add standalone files legitimately.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from luxe.agents.prompts import CLEANUP_TURN_SYSTEM, CLEANUP_TURN_TEMPLATE
from luxe.secrets import scrubbed_env

_CODE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".swift",
    ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".rb", ".php", ".cs", ".scala", ".sh",
})
_GREP_GLOBS = [f"*{e}" for e in sorted(_CODE_EXTS)]
#: Never offered as a data candidate: docs and config are deliverables a
#: document/manage task adds on purpose. Extensionless files (Makefile,
#: Dockerfile, LICENSE) and dotfiles are excluded separately.
_DOC_CONFIG_EXTS = frozenset({
    ".md", ".markdown", ".rst", ".txt", ".adoc", ".org",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".env", ".lock", ".properties", ".xml", ".html", ".css", ".scss",
    ".sql", ".ipynb",
})

#: Per-file preview shown to the model, and the cap on candidates offered.
PREVIEW_CHARS = 2000
MAX_CANDIDATES = 12


@dataclass(frozen=True)
class Candidate:
    path: str
    kind: str  # "generated" | "unreferenced" | "data"


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=30,
                           # no secrets for hooks/config commands (tools.sdd)
                           env=scrubbed_env())
    except (OSError, subprocess.TimeoutExpired):
        return 2, ""
    return p.returncode, p.stdout


def _new_paths(repo: Path, base_sha: str) -> list[str]:
    paths: list[str] = []
    rc, out = _git(repo, "diff", "--name-status", "--no-renames", base_sha)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0] == "A":
                paths.append(parts[1].strip())
    rc, out = _git(repo, "ls-files", "--others", "--exclude-standard")
    if rc == 0:
        paths.extend(ln.strip() for ln in out.splitlines() if ln.strip())
    return sorted(set(paths))


def _is_generated(path: str) -> bool:
    return "__pycache__/" in path or path.endswith((".pyc", ".pyo"))


def _referenced_elsewhere(repo: Path, path: str) -> bool:
    stem = Path(path).stem
    rc, out = _git(repo, "grep", "-l", "-w", "--untracked", "-e", stem,
                   "--", *_GREP_GLOBS)
    if rc >= 2:
        return True  # fail closed: an unknown is never offered for deletion
    hits = {ln.strip() for ln in out.splitlines() if ln.strip()}
    hits.discard(path)
    return any(not _is_generated(h) for h in hits)


def _is_data(path: str) -> bool:
    name = Path(path).name
    suffix = Path(path).suffix.lower()
    return (bool(suffix) and not name.startswith(".")
            and suffix not in _CODE_EXTS and suffix not in _DOC_CONFIG_EXTS)


def _name_mentioned_elsewhere(repo: Path, path: str) -> bool:
    """Does any OTHER file (tracked or untracked, any type) contain this
    file's name? Fail closed like `_referenced_elsewhere`."""
    name = Path(path).name
    rc, out = _git(repo, "grep", "-l", "-F", "-I", "--untracked", "-e", name)
    if rc >= 2:
        return True
    hits = {ln.strip() for ln in out.splitlines() if ln.strip()}
    hits.discard(path)
    return any(not _is_generated(h) for h in hits)


def find_candidates(repo: Path, base_sha: str) -> list[Candidate]:
    """Files this change added that look like scratch. Never raises."""
    if not base_sha:
        return []
    out: list[Candidate] = []
    for p in _new_paths(repo, base_sha):
        if _is_generated(p):
            out.append(Candidate(p, "generated"))
        elif (Path(p).suffix.lower() in _CODE_EXTS
              and not Path(p).stem.startswith("_")
              and not _referenced_elsewhere(repo, p)):
            out.append(Candidate(p, "unreferenced"))
        elif _is_data(p) and not _name_mentioned_elsewhere(repo, p):
            out.append(Candidate(p, "data"))
    return out[:MAX_CANDIDATES]


def build_messages(goal: str, candidates: list[Candidate],
                   repo: Path) -> list[dict[str, Any]]:
    blocks = []
    for c in candidates:
        if c.kind == "generated":
            blocks.append(f"--- {c.path} (generated cache file)")
            continue
        if c.kind == "data" and _looks_binary(repo / c.path):
            try:
                size = (repo / c.path).stat().st_size
            except OSError:
                size = 0
            blocks.append(f"--- {c.path} (binary data file, {size:,} bytes)")
            continue
        try:
            text = (repo / c.path).read_text(errors="replace")
        except OSError:
            text = ""
        more = "\n[…]" if len(text) > PREVIEW_CHARS else ""
        blocks.append(f"--- {c.path}\n{text[:PREVIEW_CHARS]}{more}")
    user = CLEANUP_TURN_TEMPLATE.format(goal=goal, files="\n\n".join(blocks))
    return [{"role": "system", "content": CLEANUP_TURN_SYSTEM},
            {"role": "user", "content": user}]


def _looks_binary(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return True


#: `DELETE <path>`, optionally followed by a reason (`DELETE x.py -- scratch`).
#: The trailing-text form used to be dropped silently: 3 of 2,422 cleanup turns
#: had an intended DELETE ignored, and one left `verify_strict.py` behind to
#: fail the `orphan_file` gate (acceptance/cleanup_data_check_2026_11). Safe to
#: widen: only paths from the offered candidate list are ever deleted.
_DELETE_RE = re.compile(r"^\s*[-*]?\s*DELETE\s+`?([^\s`]+?)`?(?:[\s:,;].*)?$", re.MULTILINE)


def parse_deletions(reply: str, candidates: list[Candidate]) -> list[str]:
    """Paths the reply marks DELETE, restricted to the offered candidates."""
    offered = {c.path for c in candidates}
    seen: list[str] = []
    for m in _DELETE_RE.finditer(reply or ""):
        p = m.group(1)
        if p in offered and p not in seen:
            seen.append(p)
    return seen


def delete_paths(repo: Path, paths: list[str]) -> list[str]:
    """Remove each path from the working tree AND the index; prune emptied
    dirs. Returns the paths actually removed."""
    removed: list[str] = []
    root = repo.resolve()
    for p in paths:
        target = (repo / p).resolve()
        if root not in target.parents:
            continue
        _git(repo, "rm", "-q", "--cached", "--ignore-unmatch", "--", p)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
        removed.append(p)
        parent = target.parent
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    return removed


def run_cleanup_turn(backend: Any, *, goal: str, repo: Path, base_sha: str,
                     num_ctx: int | None = None,
                     max_tokens: int = 8192) -> dict[str, Any]:
    """Offer the candidates, apply the DELETE lines. Never raises: any failure
    is reported in the returned record and leaves the tree untouched."""
    repo = Path(repo)
    cands = find_candidates(repo, base_sha)
    rec: dict[str, Any] = {"candidates": [c.path for c in cands],
                           "deleted": [], "error": None}
    if not cands:
        return rec
    try:
        resp = backend.chat(build_messages(goal, cands, repo), tools=None,
                            max_tokens=max_tokens, temperature=0.0,
                            num_ctx=num_ctx)
    except Exception as e:  # noqa: BLE001 — a failed cleanup never fails the run
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
        return rec
    rec["reply"] = (resp.text or "")[:2000]
    rec["deleted"] = delete_paths(repo, parse_deletions(resp.text or "", cands))
    return rec
