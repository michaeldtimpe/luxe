"""What project (if any) is this chat session about?

`luxe chat` is startable from anywhere — including a directory that has nothing
to do with code. Three outcomes, resolved once at startup and re-resolvable via
`/project`:

- **git** — cwd is inside a git work tree. The PROJECT ROOT is the git root, not
  cwd: starting in `src/luxe/chat/` should give the model the whole repo, and
  `git ls-files` then makes indexing ~1s.
- **dir** — no git, but cwd carries a project marker (`pyproject.toml`,
  `package.json`, …). Treat cwd as the project and index it.
- **none** — neither. No index is built at all: indexing `$HOME` cost 210s for
  coverage the model can't rely on. Read tools still work (the model can open
  any path the user names), `bm25_search`/`find_symbol` are simply not offered,
  and `/index` or `/project <path>` opts in later.

Resolution never walks above `$HOME` — a git repo somewhere above your home
directory is not "the project you're in".
"""

from __future__ import annotations

from luxe import gitcmd

import subprocess
from dataclasses import dataclass
from pathlib import Path

# Files that mean "this directory is a project" when git doesn't say so.
PROJECT_MARKERS = (
    "pyproject.toml", "setup.py", "package.json", "Cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "Gemfile", "composer.json", "CMakeLists.txt",
    "Makefile", ".luxe",
)

GIT = "git"
DIR = "dir"
NONE = "none"


@dataclass(frozen=True)
class Project:
    """The resolved subject of a chat session."""

    root: str            # absolute path; the file-tool root either way
    kind: str = NONE     # git | dir | none
    marker: str = ""     # what identified it (".git", "pyproject.toml", …)

    @property
    def is_project(self) -> bool:
        """True when there's a codebase worth indexing and locking."""
        return self.kind in (GIT, DIR)

    @property
    def label(self) -> str:
        if self.kind == GIT:
            return "git repo"
        if self.kind == DIR:
            return f"project ({self.marker})"
        return "no project"


def _git_root(path: Path) -> str:
    """The work-tree root containing `path`, or "" — asked of git itself, so
    worktrees, submodules, and `.git` files all resolve correctly."""
    try:
        proc = gitcmd.run(path, "rev-parse", "--show-toplevel", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _contains_home(candidate: Path, home: Path) -> bool:
    """True when `candidate` IS the home directory or an ancestor of it."""
    return candidate == home or home.is_relative_to(candidate)


def _has_marker(path: Path) -> str:
    for marker in PROJECT_MARKERS:
        if (path / marker).exists():
            return marker
    return ""


def resolve(start: str | Path, *, home: Path | None = None) -> Project:
    """Resolve the project for a starting directory.

    A non-existent or non-directory `start` degrades to `none` at that path
    rather than raising — the caller has already printed something friendlier.
    """
    home_path = (home or Path.home()).resolve()
    try:
        cwd = Path(start).expanduser().resolve()
    except OSError:
        return Project(root=str(start), kind=NONE)
    if not cwd.is_dir():
        return Project(root=str(cwd), kind=NONE)

    # `$HOME` itself is never "the project", even if it happens to be a git repo
    # (dotfile repos are common) — indexing it is the thing we're avoiding. Same
    # for anything ABOVE home (`/`, `/Users`): a repo that contains your home
    # directory is not the project you're in. A repo OUTSIDE home
    # (`/Volumes/work/proj`) is perfectly fine.
    if cwd != home_path:
        root = _git_root(cwd)
        if root and not _contains_home(Path(root).resolve(), home_path):
            return Project(root=str(Path(root).resolve()), kind=GIT, marker=".git")
        marker = _has_marker(cwd)
        if marker:
            return Project(root=str(cwd), kind=DIR, marker=marker)

    return Project(root=str(cwd), kind=NONE)
