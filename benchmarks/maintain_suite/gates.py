"""Strict gates for the maintain_suite grader.

A gate fires on the diff itself and overrides whatever the expected_outcome
check would have credited: destructive_diff, role_name_leak,
placeholder_diff and tool_output_echo (`apply_strict_gates`, before outcome
credit), vacuous_test and orphan_file (after a passing outcome check).
grade.py's `grade_fixture` is the one caller; the git readers that build a
gate's inputs (`_diff_hunks`, `_doc_headings`, ...) stay in grade.py.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from benchmarks.maintain_suite.proc import run as _run, run_git as _run_git


# Task types that write; `apply_strict_gates` skips every other type.
_WRITE_TASK_TYPES = {"implement", "bugfix", "document", "manage"}

# path -> (base-side titles, HEAD-side titles) outside fenced code; a side is
# None when that revision has no such file (added / deleted / renamed).
# grade.py's `_doc_headings` builds it.
DocHeadings = dict[str, tuple[frozenset[str] | None, frozenset[str] | None]]


# --- strict gates ----------------------------------------------------------
# When any of these triggers on a write-mode task, the run is marked ERROR
# regardless of test/regex outcome. Each gate closes a specific failure mode
# observed in Phase 2: models passing checks via destruction, role-name leaks,
# or placeholder text gaming the test runner.

# Threshold for the destructive-deletion gate: lines_deleted / max(1, lines_added).
# A write-mode PR that deletes 10× more than it adds is almost always a model
# wiping a working file rather than implementing a feature.
_DESTRUCTIVE_DELETION_RATIO = 5.0
# Below this threshold, allow any deletion ratio — small diffs (e.g. removing
# a 12-line dead function) shouldn't trip the gate.
_DESTRUCTIVE_MIN_DELETIONS = 30

# --- destructive_diff, prongs 2 and 3 (2026-08-19) -------------------------
# The ratio prong only sees churn whose deletions DWARF its additions, so a
# REPLACE-IN-PLACE destruction has ratio 1.0 and walks straight through it.
# Two such diffs passed at 4/5 with an empty gate list in the 2026-08-17
# bake-off (neon-rain-document-modules +132/-132; the-game-document-
# architecture ~+9/-9). Prong 2 therefore asks a different question — not
# "how lopsided is the churn?" but "did information LEAVE the repo?".
#
# Discriminator: token-level information loss. A rename, a reflow, a reindent
# and a move-to-another-file are all high-churn but PRESERVE the vocabulary of
# what they touch; a destruction does not. Alternatives considered and
# rejected:
#   * absolute deletion volume with a small-net-change window — fires on every
#     legitimate large refactor (26 of 400 real commits here sit in that
#     window), which is exactly the false positive that costs more than a
#     missed detection;
#   * deleted-LINE-not-present-among-added-lines — a rewrap or a reindent
#     changes every line it touches, so a pure reformat scores 100% "lost"
#     (measured: 0.05 under the word readings this ships instead);
#   * net-change-only heuristics — replace-in-place and reflow are
#     indistinguishable under them.
# Loss is read TWICE and both readings must agree, because each one alone has
# a shape that fools it. Calibrated against 400 commits of this repo's own
# history, over every per-file candidate in the replace band (>= 30 deleted
# lines, additions between 0.5x and 2x deletions):
#
#   loss reading        legit max   code overwrite   doc overwrite   mass rename
#   occurrences (bag)   0.58        0.71             0.84            0.33
#   distinct words      0.57 *      0.94             0.88            0.91
#
#   * one legitimate diff read 0.73 on distinct words: a `uv.lock` dependency
#     bump, whose vocabulary is content hashes. Generated files are skipped
#     outright — an agent that runs `npm install` must not fail for rewriting
#     a lockfile — and 0.57 is the max over everything that remains.
#
# Occurrence-counting is fooled by shared keywords: `def`, `return`, `import`
# repeat often enough to prop up the "preserved" side, so a module overwritten
# with unrelated stubs reads only 0.71 — under any threshold that still clears
# real refactors. Distinct-word loss is fooled the other way: a mechanical
# rename of many identifiers retires most of a file's vocabulary at once and
# reads 0.91, though nothing was destroyed. A genuine replace-in-place is the
# only shape that fails BOTH, so both must fire.
#
# On the 400-commit audit the joint criterion fires ZERO times, and the
# closest legitimate diff (a RESUME.md restructure: 0.57 / 0.58) gets 71% of
# the way to the nearer of the two thresholds.
_DESTRUCTIVE_LOST_VOCAB = 0.80
_DESTRUCTIVE_LOST_OCCURRENCES = 0.60
# Absolute floor, in distinct words: a 30-line block of closing braces or
# boilerplate carries almost no vocabulary and can never fire on rounding.
_DESTRUCTIVE_MIN_LOST_TOKENS = 20
# Machine-generated content. Its churn is not the model destroying work, and
# its vocabulary (hashes, integrity strings) is unique by construction.
_GENERATED_FILE_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "uv.lock", "poetry.lock", "pdm.lock", "cargo.lock", "gemfile.lock",
    "composer.lock", "go.sum", "requirements.lock",
})
_GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", ".lock")
# The band that says "replaced", not "grew" or "pruned". Above 2x the diff is
# growth; below 0.5x it is a net removal, which is ordinary in reverts,
# extractions and dead-code deletion (the audit's biggest losses all sit
# there) and whose extreme end prong 1 already owns.
_DESTRUCTIVE_MAX_EXPANSION = 2.0
_DESTRUCTIVE_MIN_REFILL = 0.5

# Prong 3 (document/manage, markdown only): a section heading deleted along
# with its body, in a file that did NOT grow. Requiring the BODY to go too
# means a pure retitle can never fire; the retitle escape below means a renamed
# section doesn't either; and the no-growth cap encodes the actual failure
# ("deleted the Features section to MAKE ROOM for How It Works") — a diff that
# grows the file well past what it removed delivered documentation and pruned
# along the way, which is a weaker signal than a straight trade. On the same
# 400-commit audit the cap alone dropped 43% of the doc-section candidates,
# all of them RESUME.md-style roll-forwards, while the real 2026-08-17 failure
# sits at expansion 1.00. The 13 that still fire on that audit are human doc
# restructures of this repo's OWN handoff files (RESUME.md, agents.md,
# README.md) — a population no fixture resembles: every document/manage
# fixture goal in the suite is additive, and three of them say so in words
# ("Preserve the existing content; do not replace or restructure the file").
# That is the scope this prong is defended on; if a legitimate fixture run
# ever trips it, scope it per-fixture rather than loosening the threshold.
_MARKDOWN_EXTS = frozenset({".md", ".markdown", ".mdown", ".mkd"})
_DOC_SECTION_MIN_BODY_LOSS = 3
_DOC_SECTION_MAX_EXPANSION = 1.2
_MD_HEADING_RX = re.compile(r"^\s{0,3}(?:#{1,6})\s+(.+?)\s*#*\s*$")
# A fenced code block's opening/closing line. Indent is NOT capped at 3: a
# fence nested in a list item sits deeper, and its `# comment` lines are just
# as much not-a-heading. 2026-10-21: a README that turned a ```bash block of
# `# First-time setup: ...` comments into numbered steps (every command kept)
# fired this prong on a shell comment — 2 of the harness_ab false failures.
_MD_FENCE_RX = re.compile(r"^\s*(`{3,}|~{3,})")

# Content words: >=2 chars, starting with a letter or underscore. Bare numbers
# and punctuation carry no information for the loss ratio.
_TOKEN_RX = re.compile(r"[a-z_][a-z0-9_]+")

# --- tool_output_echo ------------------------------------------------------
# `read_file` renders `f"{i + offset + 1}\t{line}"` (src/luxe/tools/fs.py:394):
# a bare decimal, one tab, the raw line, numbering strictly consecutive with no
# padding. A run of that in ADDED lines means a tool result was piped into a
# writer. 10 consecutive numbered lines is the floor — ordered markdown lists
# use `1. `, not `1\t`, and a real 10-line consecutive `<int><TAB>` run outside
# tab-separated data is implausible. Data extensions are skipped outright
# because a sequential id column is normal there.
_ECHO_MIN_RUN = 10
_ECHO_SKIP_EXTS = frozenset({".tsv", ".tab", ".csv", ".psv", ".dat"})
_NUMBERED_LINE_RX = re.compile(r"^(\d+)\t")

# Role names that the orchestrator uses internally; if any leaks into a path
# component of a changed file, the model has confused agent role labels with
# project module names. Seen in Phase 2: src/worker_read.js,
# src/input/worker_analyze/reset.py.
# Multi-word role labels — substring match after tokenizing on _ and -.
_ROLE_FUZZY_NEEDLES = (
    "worker_read", "worker_code", "worker_analyze", "micro_architect",
)
# Single-token role labels — discrete-token match (so "encoder" doesn't
# trip "coder"). "coder" is intentionally excluded.
_ROLE_SINGLE_TOKENS = frozenset({
    "drafter", "verifier", "linter", "architect", "synthesizer", "validator",
})

# Placeholder strings the model has emitted as "implementations". Wider
# patterns than the v1 set — Phase 2 showed the model evading by adding
# extra adjectives ("your real listener code here") or trigger verb variants
# ("attach the listener here").
_PLACEHOLDER_PATTERNS = [
    re.compile(r"<paste\b[^<>]*\bhere\s*>", re.IGNORECASE),
    re.compile(
        r"(?://|#)\s*your\s+(?:real\s+|own\s+|actual\s+)?\w+(?:\s+\w+){0,5}\s+(?:code|here|implementation|logic)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?://|#)\s*(?:add|implement|insert|paste|reset|attach|wire|hook)\s+"
        r"(?:the\s+|a\s+|an\s+)?\w+(?:\s+\w+){0,5}\s+here\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?://|#)\s*(?:fill\s+in|put|place)\s+(?:the\s+|your\s+)?\w+(?:\s+\w+){0,3}\s+here\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?://|#)\s*todo:?\s*(?:implement|add|finish|complete|fill|wire|hook)\s",
               re.IGNORECASE),
    re.compile(r"(?://|#)\s*real\s+\w+(?:\s+\w+){0,3}\s+(?:goes|belongs)\s+here\b",
               re.IGNORECASE),
]


def check_destructive_deletion(
    additions: int,
    deletions: int,
    *,
    diff_hunks: "dict[str, tuple[list[str], list[str]]] | None" = None,
    task_type: str = "",
    doc_headings: "DocHeadings | None" = None,
) -> tuple[bool, str]:
    """True if the diff destroys existing content. Returns (triggered, detail).

    Three prongs, cheapest first. Prong 1 is the original lopsided-churn test.
    Prongs 2 and 3 need `diff_hunks` (per-file added/deleted lines); without it
    they are skipped, so every legacy caller keeps its exact behaviour.
    """
    # Prong 1 — lopsided churn: deletions dwarf additions.
    if deletions >= _DESTRUCTIVE_MIN_DELETIONS:
        if additions == 0:
            return True, f"deleted {deletions} lines, added 0"
        ratio = deletions / max(1, additions)
        if ratio >= _DESTRUCTIVE_DELETION_RATIO:
            return True, (f"deleted {deletions}, added {additions} "
                          f"(ratio {ratio:.1f}× — destructive)")

    if not diff_hunks:
        return False, ""

    # Prong 2 — replace-in-place: same-sized churn that loses information.
    ok, detail = _check_replace_in_place(diff_hunks)
    if ok:
        return True, detail

    # Prong 3 — a documented markdown section deleted to make room.
    ok, detail = _check_doc_section_loss(diff_hunks, task_type, doc_headings)
    if ok:
        return True, detail
    return False, ""


def _content_tokens(lines: list[str]) -> "Counter[str]":
    """Multiset of content words in `lines`.

    Words only (>=2 chars, must start with a letter or underscore): bare
    numbers and punctuation carry no information, so deleting 30 lines of
    closing braces can never read as information loss, and the line-number
    prefixes this file's sibling gate hunts for never inflate the added side.
    """
    counts: "Counter[str]" = Counter()
    for line in lines:
        counts.update(_TOKEN_RX.findall(line.lower()))
    return counts


def _vocabulary(lines: list[str]) -> set[str]:
    """The DISTINCT content words in `lines` — see the calibration table above
    for why distinct beats occurrence-counting here."""
    return set(_content_tokens(lines))


def _is_generated(path: str) -> bool:
    name = Path(path).name.lower()
    return name in _GENERATED_FILE_NAMES or name.endswith(_GENERATED_SUFFIXES)


def _check_replace_in_place(
    diff_hunks: "dict[str, tuple[list[str], list[str]]]",
) -> tuple[bool, str]:
    """Per-file: >= _DESTRUCTIVE_MIN_DELETIONS lines removed, refilled to
    roughly the same size, and most of the removed vocabulary is nowhere in
    the diff.

    Added tokens are pooled across EVERY file in the diff while deletions are
    weighed per file, so extract-a-module refactors (delete here, re-add
    there) preserve their vocabulary and stay clean.
    """
    all_added: list[str] = []
    for added, _deleted in diff_hunks.values():
        all_added.extend(added)
    added_counts = _content_tokens(all_added)
    added_vocab = set(added_counts)

    for path, (added, deleted) in sorted(diff_hunks.items()):
        if len(deleted) < _DESTRUCTIVE_MIN_DELETIONS:
            continue
        if _is_generated(path):
            continue
        if not (_DESTRUCTIVE_MIN_REFILL * len(deleted) <= len(added)
                <= _DESTRUCTIVE_MAX_EXPANSION * len(deleted)):
            # Outside the band this is growth or a net removal, not a
            # replace-in-place.
            continue
        deleted_counts = _content_tokens(deleted)
        deleted_vocab = set(deleted_counts)
        occurrences = sum(deleted_counts.values())
        if not deleted_vocab or not occurrences:
            continue
        lost_vocab = deleted_vocab - added_vocab
        if len(lost_vocab) < _DESTRUCTIVE_MIN_LOST_TOKENS:
            continue
        vocab_frac = len(lost_vocab) / len(deleted_vocab)
        occ_frac = sum((deleted_counts - added_counts).values()) / occurrences
        if (vocab_frac >= _DESTRUCTIVE_LOST_VOCAB
                and occ_frac >= _DESTRUCTIVE_LOST_OCCURRENCES):
            return True, (
                f"{path}: replaced in place (+{len(added)}/-{len(deleted)}) — "
                f"{len(lost_vocab)} of {len(deleted_vocab)} distinct content "
                f"words ({vocab_frac:.0%}, {occ_frac:.0%} of occurrences) "
                f"appear nowhere in the diff's added lines; this removes "
                f"information rather than reformatting it"
            )
    return False, ""


def _headings(lines: list[str]) -> list[str]:
    """ATX markdown heading titles found in `lines`."""
    out: list[str] = []
    for line in lines:
        m = _MD_HEADING_RX.match(line)
        if m:
            title = m.group(1).strip()
            if title:
                out.append(title)
    return out


def _unfenced_headings(text: str) -> frozenset[str]:
    """Titles of the ATX headings in a whole markdown file that sit OUTSIDE
    fenced code blocks.

    Diff hunks can't answer this on their own — a hunk can open mid-fence — so
    prong 3 reads the file at each side of the diff and keeps only titles that
    are real headings there. A title present both as a heading and as a fenced
    comment counts as a heading: the filter only ever removes candidates.
    """
    out: set[str] = set()
    fence = ""
    for line in text.splitlines():
        m = _MD_FENCE_RX.match(line)
        if m:
            marker = m.group(1)
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) \
                    and not line.strip()[len(marker):].strip():
                fence = ""
            continue
        if fence:
            continue
        hm = _MD_HEADING_RX.match(line)
        if hm and hm.group(1).strip():
            out.add(hm.group(1).strip())
    return frozenset(out)


def _heading_key(title: str) -> tuple[str, frozenset[str]]:
    tokens = frozenset(_TOKEN_RX.findall(title.lower()))
    return "".join(sorted(re.findall(r"[a-z0-9]+", title.lower()))), tokens


def _check_doc_section_loss(
    diff_hunks: "dict[str, tuple[list[str], list[str]]]",
    task_type: str,
    doc_headings: "DocHeadings | None" = None,
) -> tuple[bool, str]:
    """document/manage only: a markdown section heading AND its body deleted.

    `the-game-document-architecture` passed at 4/5 with an empty gate list on
    a ~+9/-9 diff whose whole content was deleting `## Features` to make room
    for `## How It Works` — ratio 1.0, far under prong 1's volume floor, and
    the added prose satisfied the regex grader. Every document/manage fixture
    goal in this suite is additive ("Preserve the existing content; do not
    replace or restructure the file"), so losing a documented section is a
    failure of the task by construction.

    Retitles are NOT losses: a deleted heading survives if an added heading
    matches it after alnum-squashing ("Quick Start" -> "Quickstart") or shares
    at least half its words. And the body must go too — a diff that only
    rewrites heading lines can never trip this.

    `doc_headings` (from `_doc_headings`) maps path -> (base titles, HEAD
    titles) outside fenced code. When a side is known, a `#` line that is not
    a real heading on that side is body text, not a section. A missing entry
    or side keeps the pre-2026-10-21 reading of every `#` line as a heading.
    """
    if task_type not in ("document", "manage"):
        return False, ""
    for path, (added, deleted) in sorted(diff_hunks.items()):
        if Path(path).suffix.lower() not in _MARKDOWN_EXTS:
            continue
        base_real, head_real = (doc_headings or {}).get(path, (None, None))
        deleted_titles = _headings(deleted)
        if base_real is not None:
            deleted_titles = [t for t in deleted_titles if t in base_real]
        if not deleted_titles:
            continue
        body_lost = len(deleted) - len(deleted_titles)
        if body_lost < _DOC_SECTION_MIN_BODY_LOSS:
            continue
        if len(added) > _DOC_SECTION_MAX_EXPANSION * len(deleted):
            # The file grew well past what it lost — documentation was
            # delivered, not traded away.
            continue
        added_titles = _headings(added)
        if head_real is not None:
            added_titles = [t for t in added_titles if t in head_real]
        added_keys = [_heading_key(t) for t in added_titles]
        for title in deleted_titles:
            squashed, tokens = _heading_key(title)
            survived = False
            for a_squashed, a_tokens in added_keys:
                if squashed and squashed == a_squashed:
                    survived = True
                    break
                if tokens and len(tokens & a_tokens) >= len(tokens) / 2:
                    survived = True
                    break
            if not survived:
                return True, (
                    f"{path}: section `{title}` deleted with "
                    f"{body_lost} line(s) of body and not retitled among the "
                    f"added headings — existing documentation removed"
                )
    return False, ""


def check_tool_output_echo(
    diff_hunks: "dict[str, tuple[list[str], list[str]]] | None",
) -> tuple[bool, str]:
    r"""True if added lines carry `read_file`'s own line-number rendering.

    `read_file` emits `f"{i + offset + 1}\t{line}"` (src/luxe/tools/fs.py) —
    a bare decimal, one tab, the raw line, numbering strictly consecutive with
    no padding. Writing that back into a file is unambiguous corruption:
    `neon-rain-document-modules` rewrote ARCHITECTURE.md as `1\t# Architecture`,
    `2\t`, `3\t## Overview`, ... and scored 4/5 with an empty gate list.

    Kept separate from `destructive_diff` because the remediation differs —
    this one says the model piped a tool result into a writer, not that it
    deleted work — and because the vocabulary survives the corruption, so the
    information-loss prong cannot see it.
    """
    if not diff_hunks:
        return False, ""
    for path, (added, _deleted) in sorted(diff_hunks.items()):
        if Path(path).suffix.lower() in _ECHO_SKIP_EXTS:
            # Tab-separated data with a sequential id column is the one place
            # `<int>\t<text>` runs are normal.
            continue
        run = 0
        best = 0
        prev: int | None = None
        for line in added:
            m = _NUMBERED_LINE_RX.match(line)
            if m is None:
                run = 0
                prev = None
                continue
            n = int(m.group(1))
            run = run + 1 if prev is not None and n == prev + 1 else 1
            prev = n
            best = max(best, run)
        if best >= _ECHO_MIN_RUN:
            return True, (
                f"{path}: {best} consecutively numbered `<n>\\t` added lines — "
                f"read_file's line-number rendering written back into the file"
            )
    return False, ""


def check_role_name_leak(file_paths: list[str]) -> tuple[bool, str]:
    """True if any path component contains an agent role label (fuzzy)."""
    leaks: list[str] = []
    for path in file_paths:
        for part in path.split("/"):
            stem = part.split(".", 1)[0].lower()
            tokens = re.split(r"[-_]+", stem)
            joined = "_".join(tokens)
            if any(needle in joined for needle in _ROLE_FUZZY_NEEDLES):
                leaks.append(path)
                break
            if any(t in _ROLE_SINGLE_TOKENS for t in tokens):
                leaks.append(path)
                break
    if leaks:
        return True, f"role-name leak in {len(leaks)} path(s): {leaks[:3]}"
    return False, ""


def check_placeholder_text(diff_added_text: str) -> tuple[bool, str]:
    """True if any added line matches a known placeholder pattern."""
    for pat in _PLACEHOLDER_PATTERNS:
        m = pat.search(diff_added_text)
        if m:
            return True, f"placeholder match: {m.group(0)[:80]!r}"
    return False, ""


def _looks_like_test_file(path: str) -> bool:
    """Heuristic: matches common test conventions across JS/TS/Python.

    Catches *test*.{js,ts,jsx,tsx,py}, files in tests/__tests__/spec dirs,
    test_*.py, *_test.{py,go}. Tight enough to skip non-test files like
    "src/utils/test-helpers.js" (which lives under src/ not tests/).
    """
    p = path.lower()
    parts = p.split("/")
    test_dirs = {"tests", "test", "__tests__", "spec", "specs", "__test__"}
    if any(part in test_dirs for part in parts[:-1]):
        return True
    name = parts[-1]
    if name.startswith("test_") and name.endswith((".py", ".go", ".rs")):
        return True
    for suffix in (".test.js", ".test.jsx", ".test.ts", ".test.tsx",
                   ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx",
                   "_test.py", "_test.go", "_test.rs"):
        if name.endswith(suffix):
            return True
    return False


def check_vacuous_test(
    repo_path: Path,
    base_sha: str,
    command: str,
    changed_files: list[str],
    timeout: float = 600.0,
) -> tuple[bool, str]:
    """Vacuous-test gate: a new test that passes against the unmodified base
    isn't actually testing the implementation.

    Strategy: spin up a git worktree at base_sha, copy the new/modified test
    files from HEAD into it, then run the test command. If rc=0, the test
    file isn't exercising any new behaviour — mark vacuous.

    A pass at base only indicts a test the command actually RAN. A test
    file the command never loads (neon-rain's `npm test` runs one named
    file; a model's new `tests/foo.test.js` is invisible to it) passes at
    base trivially, and scoring that as vacuous failed correct
    implementations. So each copied test is probed: its content is swapped
    for a line no language parses and the command re-run. rc still 0 → the
    command never loaded it → UNWIRED (reported in the detail, no
    penalty). Only tests the probe proves were run can make the gate fire.

    Returns (vacuous, detail); detail is non-empty for an unwired-only
    result too. Returns (False, "") on infrastructure errors (worktree
    creation failed, etc.) — fail-open so a flaky check doesn't downgrade
    legitimate passes.
    """
    if not base_sha or not command:
        return False, ""
    test_paths = [p for p in changed_files if _looks_like_test_file(p)]
    if not test_paths:
        # No test files in the diff — the implementation must be carrying
        # the existing test suite. Not a vacuous-test concern.
        return False, ""

    import tempfile
    with tempfile.TemporaryDirectory(prefix="luxe-vacuous-") as td:
        wt = Path(td) / "worktree"
        rc, _out = _run(["git", "worktree", "add", "--detach",
                         str(wt), base_sha], cwd=repo_path, timeout=60)
        if rc != 0:
            return False, ""  # fail-open

        try:
            # Copy each new/modified test file from HEAD into the worktree.
            # Existing tests at base SHA stay as-is (the gate cares whether
            # the *new* test passes against unmodified base implementation).
            for rel in test_paths:
                src = repo_path / rel
                if not src.is_file():
                    continue
                dst = wt / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())

            rc, out = _run(["bash", "-lc", command], cwd=wt, timeout=timeout)
            tail = "\n".join((out or "").splitlines()[-15:])[:600]
            if rc != 0:
                return False, ""
            ran, unwired = _probe_tests_run(wt, command, test_paths, timeout)
            if ran:
                return True, (
                    f"new test files {ran!r} passed against base SHA "
                    f"({base_sha[:8]}) — test isn't exercising new code; tail:\n{tail}"
                )
            return False, (f"unwired: `{command}` does not run {unwired!r} "
                           "(sabotaged copies still passed) — not penalized")
        finally:
            # Best-effort cleanup; tempdir context will reap whatever's left.
            _run(["git", "worktree", "remove", "--force", str(wt)],
                 cwd=repo_path, timeout=60)


# One line that is a syntax error in every language the suite's test commands
# load (JS/TS, Python, Go, Rust, Ruby...): a loaded file with this body fails.
_UNPARSEABLE = "@@@ luxe vacuous-test probe: this file must not parse ((( @@@\n"


def _probe_tests_run(wt: Path, command: str, test_paths: list[str],
                     timeout: float) -> tuple[list[str], list[str]]:
    """Split `test_paths` into (run by `command`, never loaded by it).

    Each file in turn is replaced by `_UNPARSEABLE` in the base worktree and
    the command re-run; a still-zero rc means the command never read it.
    The file's HEAD content is restored before the next probe.
    """
    ran: list[str] = []
    unwired: list[str] = []
    for rel in test_paths:
        dst = wt / rel
        if not dst.is_file():
            continue
        original = dst.read_bytes()
        try:
            dst.write_text(_UNPARSEABLE)
            rc, _out = _run(["bash", "-lc", command], cwd=wt, timeout=timeout)
        finally:
            dst.write_bytes(original)
        (unwired if rc == 0 else ran).append(rel)
    return ran, unwired


# Source-code extensions for the orphan-file gate. Files outside this set
# (markdown, configs, lockfiles, etc.) are legitimate standalone additions
# and aren't subject to the orphan check.
_SOURCE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".swift",
    ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".rb", ".php", ".cs", ".scala",
})


def _is_source_path(path: str) -> bool:
    """A code source file (not a test, doc, or config)."""
    if _looks_like_test_file(path):
        return False
    return Path(path).suffix.lower() in _SOURCE_EXTS


def _added_files_in_diff(repo_path: Path, base_sha: str) -> list[str]:
    """Return paths of files newly added (status A) in base_sha..HEAD."""
    _rc, out = _run_git(["git", "diff", "--name-status", base_sha, "HEAD"],
                        cwd=repo_path)
    added: list[str] = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0] == "A":
            added.append(parts[1].strip())
    return added


def check_orphan_file(
    repo_path: Path,
    base_sha: str,
    task_type: str,
) -> tuple[bool, str]:
    """Detect new source files that nothing imports.

    Catches the "orphan-file" exploit where a model adds a from-scratch
    source file (often in a parallel language to the project's actual
    stack) that satisfies the regex/test grader, but nothing imports it
    and the existing tests still pass because the existing implementation
    is unchanged. Seen in the granite-3b bake-off (2026-04-30): model
    added `src/input/HtmlInputHandler.ts` next to the existing `.js`
    file in a JS-only project; npm test passed because nothing references
    the new TypeScript file.

    Two-prong detection. A NEW source file is orphan iff EITHER:
      1. A sibling file with the same stem in the same directory already
         existed (e.g. `Foo.ts` next to `Foo.js`). Strong duplicate signal.
      2. The file's stem isn't referenced anywhere else in the post-edit
         repo — no import, require, or path-string mentions it.

    Only applies to implement/bugfix tasks. document/manage tasks
    legitimately add standalone files (CONFIG.md, ARCHITECTURE.md,
    SECURITY-AUDIT.md) that aren't expected to be imported.

    Returns (triggered, detail). Fail-open on infrastructure errors.
    """
    if task_type not in ("implement", "bugfix"):
        return False, ""
    if not base_sha:
        return False, ""

    new_files = [p for p in _added_files_in_diff(repo_path, base_sha)
                 if _is_source_path(p)]
    if not new_files:
        return False, ""

    orphans: list[str] = []
    for new_path in new_files:
        new_stem = Path(new_path).stem
        if not new_stem or new_stem.startswith("_"):
            # __init__.py, _internal.py, etc. — legitimate bare-stem cases.
            continue

        # Prong 1: same-directory sibling with same stem (different ext).
        new_dir = (repo_path / new_path).parent
        if new_dir.is_dir():
            for sibling in new_dir.iterdir():
                if not sibling.is_file():
                    continue
                try:
                    rel = str(sibling.relative_to(repo_path))
                except ValueError:
                    continue
                if rel == new_path:
                    continue
                if sibling.stem == new_stem and _is_source_path(rel):
                    orphans.append(
                        f"{new_path} duplicates existing {rel} "
                        "(same stem, same dir — orphan duplicate)"
                    )
                    break
            else:
                # No same-stem sibling — fall through to Prong 2.
                pass
            if orphans and orphans[-1].startswith(new_path):
                continue

        # Prong 2: any reference to the stem elsewhere in source files?
        # `git grep -w` does a whole-word match on the stem across tracked
        # source files. If the only match is the new file itself, it's
        # orphan; if zero matches, also orphan.
        # git grep speaks rc 0 = matches, 1 = clean no-match, >=2 = ERROR —
        # the same exit grammar whose conflation broke the grep tool
        # (2026-08-12 audit F1). Only rc 1 may mean "orphan".
        rc, out = _run_git(
            ["git", "grep", "-l", "-w", "--", new_stem,
             "*.py", "*.js", "*.jsx", "*.ts", "*.tsx", "*.mjs", "*.cjs",
             "*.rs", "*.go", "*.java", "*.kt", "*.swift",
             "*.cpp", "*.cc", "*.cxx", "*.c", "*.h", "*.hpp",
             "*.rb", "*.php", "*.cs", "*.scala"],
            cwd=repo_path, ok_codes=(0, 1),
        )
        matches: set[str] = set()
        if rc == 0 and out.strip():
            matches = {ln.strip() for ln in out.splitlines() if ln.strip()}
        matches.discard(new_path)
        if not matches:
            orphans.append(
                f"{new_path} (stem `{new_stem}` not referenced anywhere "
                "else in the post-edit repo — orphan)"
            )

    if orphans:
        return True, "; ".join(orphans[:3])
    return False, ""


def apply_strict_gates(
    *,
    task_type: str,
    file_paths: list[str],
    additions: int,
    deletions: int,
    diff_added_text: str,
    diff_hunks: dict[str, tuple[list[str], list[str]]] | None = None,
    doc_headings: "DocHeadings | None" = None,
) -> list[tuple[str, str]]:
    """Run every gate; return list of (gate_name, detail) for each triggered
    gate. Empty list = no gates triggered. Read-mode tasks (review,
    summarize) skip these checks since they shouldn't produce diffs in the
    first place.

    `diff_hunks` maps path -> (added_lines, deleted_lines) from `_diff_hunks`.
    It is optional so a caller with only the shortstat keeps the pre-2026-08-19
    behaviour exactly; the replace-in-place, doc-section and tool_output_echo
    checks need it and are skipped without it. `doc_headings` (optional, from
    `_doc_headings`) lets the doc-section check ignore `#` lines inside fenced
    code.
    """
    if task_type not in _WRITE_TASK_TYPES:
        return []
    triggered: list[tuple[str, str]] = []
    ok, detail = check_destructive_deletion(
        additions, deletions, diff_hunks=diff_hunks, task_type=task_type,
        doc_headings=doc_headings)
    if ok:
        triggered.append(("destructive_diff", detail))
    ok, detail = check_role_name_leak(file_paths)
    if ok:
        triggered.append(("role_name_leak", detail))
    ok, detail = check_placeholder_text(diff_added_text)
    if ok:
        triggered.append(("placeholder_diff", detail))
    ok, detail = check_tool_output_echo(diff_hunks)
    if ok:
        triggered.append(("tool_output_echo", detail))
    return triggered
