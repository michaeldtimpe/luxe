"""Pre-flight repo survey + budget heuristics for review/refactor tasks.

`/review` and `/refactor` used to run with a hardcoded 60-minute task
wall and whatever `num_ctx` the agent's config specified, regardless
of whether the target was a tiny prototype or a 20k-LOC codebase. This
module turns a target repo into a compact `RepoSurvey` and then into
a `BudgetDecision` (tier, task wall, num_ctx, rationale) the caller
plugs into the Task at spawn time.

Heuristics are grounded in the zoleb (tiny) and elara (medium) runs
plus the Ollama A/B decode numbers in
`luxe/results/ab_ollama_vs_llamacpp/REPORT.md`:
    qwen2.5:7b       ≈ 33.5 tok/s
    qwen2.5-coder:14b ≈ 16.9 tok/s
    qwen2.5:32b       ≈  7.6 tok/s

A 7-subtask review at qwen2.5:32b spending ~1500 output tokens per
inspection subtask is ~3.3 min of pure decode per subtask before
tool-exec and prefill — 30-45 min total for small repos, 60-90 min
for mid-sized ones once context-prefill costs grow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Directories we never descend into. Ignore anything that's either
# vendored (node_modules, target/, .venv), generated (dist/, build/),
# or metadata (.git, .tox, __pycache__).
_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "target",
        "dist",
        "build",
        ".next",
        ".cache",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
    }
)

# Extension → language key. Determines which files count toward LOC
# and `primary_language`. Everything else shows up in `total_files`
# but not `source_files`.
_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "java",
    ".kts": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "c",
    ".cpp": "c",
    ".hpp": "c",
    ".cs": "csharp",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
}

# Hard cap on file count; past this, everything skips to tier `huge`
# with a token-conservative budget.
_MAX_FILES_SCANNED = 50_000


@dataclass
class RepoSurvey:
    """Compact summary of a repo for budget sizing."""

    total_files: int = 0
    source_files: int = 0
    total_loc: int = 0
    avg_source_bytes: int = 0
    largest_source_bytes: int = 0
    primary_language: str = "unknown"
    language_breakdown: dict[str, int] = field(default_factory=dict)
    has_tests_dir: bool = False
    has_git_history: bool = False
    # True when we stopped walking because we hit _MAX_FILES_SCANNED.
    hit_scan_cap: bool = False


def _should_skip_dir(name: str) -> bool:
    if name in _IGNORE_DIRS:
        return True
    # Dot-directories are usually hidden state — skip except .github
    # (which often has workflow YAML worth counting as source).
    return name.startswith(".") and name not in {".github"}


def analyze_repo(root: Path) -> RepoSurvey:
    """Walk `root` and produce a `RepoSurvey`. Uses `os.walk` (faster
    than `Path.rglob`) and stops at the file-count cap to keep the
    survey under ~1s even on generated-asset monorepos."""
    root = Path(root).resolve()
    survey = RepoSurvey()
    source_bytes_total = 0

    if (root / ".git").exists():
        survey.has_git_history = True

    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
        cur_path = Path(cur)
        if cur_path.name in {"tests", "test", "__tests__"}:
            survey.has_tests_dir = True

        for fname in files:
            survey.total_files += 1
            if survey.total_files > _MAX_FILES_SCANNED:
                survey.hit_scan_cap = True
                break

            ext = Path(fname).suffix.lower()
            lang = _LANG_BY_EXT.get(ext)
            if lang is None:
                continue

            fpath = cur_path / fname
            try:
                stat = fpath.stat()
            except OSError:
                continue

            survey.source_files += 1
            source_bytes_total += stat.st_size
            if stat.st_size > survey.largest_source_bytes:
                survey.largest_source_bytes = stat.st_size
            survey.language_breakdown[lang] = (
                survey.language_breakdown.get(lang, 0) + 1
            )

            # Line count — fast path via bytes scan rather than
            # splitlines(), which allocates a list.
            try:
                with fpath.open("rb") as fh:
                    survey.total_loc += sum(
                        chunk.count(b"\n")
                        for chunk in iter(lambda: fh.read(65_536), b"")
                    )
            except OSError:
                continue

        if survey.hit_scan_cap:
            break

    if survey.source_files:
        survey.avg_source_bytes = source_bytes_total // survey.source_files

    survey.primary_language = _pick_primary_language(
        survey.language_breakdown, survey.source_files
    )
    return survey


def _pick_primary_language(breakdown: dict[str, int], total: int) -> str:
    """60% threshold: if any language dominates, that's the primary;
    otherwise 'mixed'. No source files → 'unknown'."""
    if total == 0:
        return "unknown"
    top_lang, top_count = max(breakdown.items(), key=lambda kv: kv[1])
    if top_count / total >= 0.60:
        return top_lang
    return "mixed"


@dataclass
class BudgetDecision:
    """Output of `size_budgets`. `rationale` is a short human-readable
    one-liner the console prints at plan time so the user knows what
    the orchestrator picked."""

    tier: str
    task_max_wall_s: float
    num_ctx: int
    rationale: str


# Tier thresholds expressed as (max_loc_exclusive, tier_name,
# task_wall_s, num_ctx). Evaluated in order; first match wins.
_TIER_TABLE: list[tuple[int, str, float, int]] = [
    (500, "tiny", 1800.0, 8192),
    (2_000, "small", 2700.0, 8192),
    # Medium / large get 24k: prior 16k was too tight for multi-turn
    # review — sub-task prompt_tokens totals observed at 33k across 16
    # tool calls mean Ollama was silently dropping the oldest messages
    # when num_ctx = 16k. 24k at qwen2.5:32b Q4_K_M adds ~5 GB of KV
    # cache over 16k; acceptable on 64 GB unified memory.
    (10_000, "medium", 3600.0, 24_576),
    (50_000, "large", 5400.0, 24_576),
    # Anything larger — including `hit_scan_cap` cases — gets the
    # huge tier. 32k ctx on qwen2.5:32b Q4_K_M adds ~8 GB to KV
    # cache; acceptable on a 64 GB machine but worth keeping in mind.
    (10**9, "huge", 7200.0, 32_768),
]


def size_budgets(survey: RepoSurvey) -> BudgetDecision:
    """Pick a (tier, wall, ctx) triple from the survey. See the
    module docstring for the numbers' derivation."""
    loc = survey.total_loc
    for ceiling, tier, wall_s, ctx in _TIER_TABLE:
        if loc < ceiling:
            rationale = _rationale(survey, tier, wall_s, ctx)
            return BudgetDecision(
                tier=tier,
                task_max_wall_s=wall_s,
                num_ctx=ctx,
                rationale=rationale,
            )
    # Unreachable given the 10**9 ceiling on the last row, but keep
    # mypy happy.
    return BudgetDecision("huge", 7200.0, 32_768, "fallback")


def _rationale(
    survey: RepoSurvey, tier: str, wall_s: float, ctx: int
) -> str:
    lang = survey.primary_language
    files = survey.source_files
    loc = survey.total_loc
    wall_min = int(round(wall_s / 60))
    ctx_k = ctx // 1024
    cap_note = " (hit scan cap)" if survey.hit_scan_cap else ""
    return (
        f"{files} {lang} source file(s) · {loc:,} LOC · {tier}"
        f"{cap_note} → {wall_min} min wall, {ctx_k}k ctx"
    )
