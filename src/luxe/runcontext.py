"""Context blocks for the maintain main pass: validation menu + context pack.

Both opt-in (exact string "1"), both OFF by default, and with both off
`maintain` passes no `extra_context` at all, so the request is byte-identical
to the pre-2026-11 path by construction. Neither is read anywhere else: the
benchmark harness reaches them only through `luxe maintain`, chat never.

- `LUXE_VALIDATION_MENU=1` (write tasks only): the repo's detected test
  command (`pr.detect_test_command`, until now used only by the PR step after
  the run), the check tools the run's own tool surface exposes, and a closing
  "Checks" section in the report. Origin: deepsense.ai harness pattern 4, and
  the 2026-08-19 Gemma 4 hand-verify (zero lint/typecheck/git_diff calls on
  any fixture).
- `LUXE_CONTEXT_PACK=1`: a capped block of POINTERS assembled with no model
  call: top BM25 files for the goal (the index `maintain._build_indices`
  already built), each file's last commit subjects, tests matched by naming
  convention, and the most recently changed paths. Paths and subjects only,
  never file contents. Origin: harness pattern 2 (just-in-time context).

All wording lives in `agents/prompts.py` (agents.sdd); this module only
fills it in. Every git call is scrubbed (tools.sdd: git luxe runs for itself
gets `scrubbed_env`), stdin-detached, and time-capped; a failure degrades to
a shorter block, never an exception into the pipeline.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from pathlib import Path, PurePosixPath

from luxe.agents.prompts import (
    CONTEXT_PACK_FILES,
    CONTEXT_PACK_HEADER,
    CONTEXT_PACK_RECENT,
    CONTEXT_PACK_TESTS,
    CONTEXT_PACK_TRUNCATED,
    VALIDATION_MENU_NO_TEST_LINE,
    VALIDATION_MENU_TEMPLATE,
    VALIDATION_MENU_TEST_LINE,
    VALIDATION_MENU_TOOL_LINE,
)
from luxe import search as search_mod
from luxe.secrets import scrubbed_env

MENU_ENV = "LUXE_VALIDATION_MENU"
PACK_ENV = "LUXE_CONTEXT_PACK"

#: Tools that check work rather than produce it, in the order the menu lists
#: them. Only those the run's tool surface actually exposes are named.
CHECK_TOOLS = ("lint", "typecheck", "lint_js", "typecheck_ts", "lint_rust",
               "vet_go", "git_diff")

#: Context-pack limits. The cap is in characters (the read-budget work showed
#: every KB costs window); sections fill in order and stop at the cap.
PACK_MAX_CHARS = 2048
PACK_TOP_FILES = 5
PACK_COMMITS_PER_FILE = 3
PACK_MAX_TESTS = 5
PACK_RECENT_COMMITS = 20
PACK_RECENT_FILES = 10
_SUBJECT_CHARS = 72
_GIT_TIMEOUT_S = 15

_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs"})


def menu_enabled(environ: dict | None = None) -> bool:
    return (environ if environ is not None else os.environ).get(MENU_ENV) == "1"


def pack_enabled(environ: dict | None = None) -> bool:
    return (environ if environ is not None else os.environ).get(PACK_ENV) == "1"


# --- validation menu --------------------------------------------------------

def validation_menu(test_command: str, tool_names: list[str] | set[str]) -> str:
    """The menu block for a run whose tool surface is `tool_names`."""
    test_line = (VALIDATION_MENU_TEST_LINE.format(command=test_command)
                 if test_command.strip() else VALIDATION_MENU_NO_TEST_LINE)
    present = [t for t in CHECK_TOOLS if t in set(tool_names)]
    tool_line = VALIDATION_MENU_TOOL_LINE.format(
        tools=", ".join(present) if present else "none")
    return VALIDATION_MENU_TEMPLATE.format(test_line=test_line,
                                           tool_line=tool_line)


# --- context pack -----------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=repo,
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=_GIT_TIMEOUT_S,
                           env=scrubbed_env())
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 else ""


def is_test_path(path: str) -> bool:
    pp = PurePosixPath(path)
    if any(part.lower() in _TEST_DIRS for part in pp.parts[:-1]):
        return True
    stem = pp.name.split(".")[0].lower()
    name = pp.name.lower()
    return (stem.startswith("test_") or stem.endswith("_test")
            or ".test." in name or ".spec." in name or stem == "test")


def _core_stem(path: str) -> str:
    """`tests/test_calc.py` → `calc`; `src/calc.test.ts` → `calc`."""
    stem = PurePosixPath(path).name.split(".")[0].lower()
    for affix in ("test_", "spec_"):
        if stem.startswith(affix):
            stem = stem[len(affix):]
    for affix in ("_test", "_spec", "-test", "-spec"):
        if stem.endswith(affix):
            stem = stem[: -len(affix)]
    return stem


def related_tests(files: list[str], tracked: list[str]) -> list[str]:
    """Tracked test files whose core stem matches a non-test file in `files`."""
    stems = {_core_stem(f) for f in files if not is_test_path(f)}
    stems.discard("")
    stems.discard("index")  # matches every JS package; pure noise
    out = [t for t in tracked if is_test_path(t) and _core_stem(t) in stems]
    return sorted(dict.fromkeys(out))[:PACK_MAX_TESTS]


def _file_commits(repo: Path, path: str) -> list[str]:
    out = _git(repo, "log", f"-n{PACK_COMMITS_PER_FILE}",
               "--format=%ad %s", "--date=short", "--", path)
    return [line[:_SUBJECT_CHARS] for line in out.splitlines() if line.strip()]


def _recent_files(repo: Path) -> list[str]:
    out = _git(repo, "log", f"-n{PACK_RECENT_COMMITS}", "--name-only",
               "--format=")
    counts = Counter(line for line in out.splitlines() if line.strip())
    exists = [p for p, _ in counts.most_common() if (repo / p).exists()]
    return exists[:PACK_RECENT_FILES]


def context_pack(repo_path: str | Path, goal: str, index=None) -> str:
    """The pointer block for `goal`. "" when there is nothing to point at.

    `index` defaults to the BM25 index `maintain._build_indices` installed.
    """
    repo = Path(repo_path)
    if index is None:
        index = search_mod.get_index()
    hits = [p for p, _ in index.search(goal, k=PACK_TOP_FILES)] if index else []
    tracked = _git(repo, "ls-files").splitlines()

    sections: list[tuple[str, list[str]]] = []
    if hits:
        lines = []
        for path in hits:
            commits = _file_commits(repo, path)
            lines.append(f"- {path}")
            lines.extend(f"    {c}" for c in commits)
        sections.append((CONTEXT_PACK_FILES, lines))
    tests = related_tests(hits, tracked)
    if tests:
        sections.append((CONTEXT_PACK_TESTS, [f"- {t}" for t in tests]))
    recent = _recent_files(repo)
    if recent:
        sections.append((CONTEXT_PACK_RECENT, [f"- {r}" for r in recent]))
    if not sections:
        return ""

    out = CONTEXT_PACK_HEADER
    budget = PACK_MAX_CHARS - len(CONTEXT_PACK_TRUNCATED) - 2
    for title, lines in sections:
        head = f"\n\n{title}"
        if len(out) + len(head) + 1 + len(lines[0]) > budget:
            out += f"\n\n{CONTEXT_PACK_TRUNCATED}"
            return out
        out += head
        for line in lines:
            if len(out) + 1 + len(line) > budget:
                out += f"\n{CONTEXT_PACK_TRUNCATED}"
                return out
            out += f"\n{line}"
    return out
