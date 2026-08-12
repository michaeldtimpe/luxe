"""Filesystem tools — scoped to repo root for safety."""

from __future__ import annotations

import fnmatch
import itertools
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from luxe.sdd import SddParseError
from luxe.spec_resolver import resolve_chain
from luxe.tools.base import ToolDef, ToolFn

_REPO_ROOT: Path | None = None
_MAX_FILE_SIZE = 256 * 1024  # whole-file read limit (no offset/limit given)
_MAX_READ_BYTES = 256 * 1024  # ceiling on what ANY single read returns

#: Optional ctx-derived override for the two limits above (2026-08-12).
#:
#: The fixed 256 KB predates any of the context tiers and is sized as though
#: every session ran at 256K. Measured against the calibrated token rate
#: (~1.67 chars/token on code, see luxe.context), ONE max-size read is:
#:
#:     small  8,192 -> 1920%    medium 32,768 -> 480%   (the DEFAULT + bench)
#:     large 65,536 ->  240%    xlarge 131,072 -> 120%
#:     huge 262,144 ->   60%
#:
#: i.e. there is no tier where it is a sensible single-result size — it is 60%
#: of the LARGEST window luxe can open. So the answer to "scale it with ctx"
#: is "yes, downward": budget a result as a fraction of the window instead.
#:
#: OPT-IN and OFF by default (`None` = use the constants above), because this
#: is a benchmark-path behaviour change: at num_ctx=32768 it takes the
#: whole-file limit from 256 KB to ~13 KB, so files that were read in one call
#: would start needing windows. Set via `set_read_budget()`; the chat
#: front-end computes it from the turn's num_ctx when enabled.
_READ_BUDGET: int | None = None

#: Share of the context window one tool result may occupy, and the floor below
#: which the budget stops shrinking (a 8K window would otherwise allow ~3 KB,
#: too small to read an ordinary source file at all).
READ_BUDGET_FRACTION = 0.25
READ_BUDGET_FLOOR = 8 * 1024
#: Characters per real token on code + JSON, from the live calibration
#: measurements (chars/4 runs ~2.4x low, so 4/2.4).
_CHARS_PER_TOKEN = 4 / 2.4


def budget_for_ctx(num_ctx: int) -> int:
    """Bytes one tool result may return in a `num_ctx`-token window."""
    if num_ctx <= 0:
        return _MAX_FILE_SIZE
    return max(READ_BUDGET_FLOOR,
               int(num_ctx * READ_BUDGET_FRACTION * _CHARS_PER_TOKEN))


def set_read_budget(max_bytes: int | None) -> None:
    """Override the read limits for this process. `None` restores the fixed
    constants, which is what the benchmark/maintain path always uses."""
    global _READ_BUDGET
    _READ_BUDGET = max_bytes if (max_bytes is None or max_bytes > 0) else None


def read_limit() -> int:
    """The active whole-file / per-result ceiling."""
    return _READ_BUDGET if _READ_BUDGET is not None else _MAX_FILE_SIZE


#: Above this, don't count lines for the too-large message — the count costs a
#: full scan and the message is just as actionable without it.
_LINE_COUNT_MAX_BYTES = 32 * 1024 * 1024
_MAX_RESULTS = 150


# --- write-time honesty guards --------------------------------------------
# Catch the three failure modes Phase 2 surfaced — placeholder text,
# role-name leaks, mass-deletion overwrites — at the moment of write rather
# than after the PR is opened. Cheaper feedback loop for the model: the
# tool returns an error, the agent gets a chance to retry with real code.

# Multi-word placeholder coverage — the model has been seen evading the
# tight 1-word "your X code here" form by writing "your real listener code
# here". \w+(\s+\w+){0,5} allows up to 6 noun phrases between trigger words.
_PLACEHOLDER_PATTERNS = (
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
)
# Agent role names. Matching is fuzzy — the model has been seen writing
# `worker_read_r.py` to sneak past exact-stem matching, so we also flag any
# path component whose tokens (split on _ and -) contain a role-name token.
_ROLE_NAME_TOKENS = frozenset({
    "worker_read", "worker_code", "worker_analyze",
    "drafter", "coder", "verifier", "linter",
    "architect", "micro_architect", "synthesizer", "validator",
})
# Single-token leak detection — split path components on `_` and `-`, then
# look for these multi-word role names AS substrings of the joined token list.
_ROLE_FUZZY_NEEDLES = (
    "worker_read", "worker_code", "worker_analyze",
    "micro_architect",
    # Single-word roles get added with prefix/suffix flexibility below.
)
_ROLE_SINGLE_TOKENS = frozenset({
    "drafter", "verifier", "linter", "architect", "synthesizer", "validator",
    # NOTE: "coder" intentionally omitted — too common in legitimate names
    # ("encoder", "decoder", "transcoder"). Compose with prefixes if needed.
})
# Mass-deletion thresholds: refuse to collapse a non-trivial file into a
# stub. Old must have ≥ N lines, new must have ≤ M lines, AND the size drop
# must be at least 10× — otherwise legitimate refactor-shrinks still pass.
_MASS_DELETE_OLD_LINES = 50
_MASS_DELETE_NEW_LINES = 5
_MASS_DELETE_RATIO = 10.0


def _check_placeholder_text(content: str) -> str | None:
    """Return error string if `content` contains a placeholder pattern."""
    for pat in _PLACEHOLDER_PATTERNS:
        m = pat.search(content)
        if m:
            return (
                f"refusing to write placeholder text {m.group(0)[:80]!r}. "
                "Replace with the real implementation, or read the existing "
                "code first if you don't know what to write."
            )
    return None


def _check_role_path(rel_path: str) -> str | None:
    """Return error if any path component contains an agent role label —
    fuzzy: catches `worker_read.js`, `worker_read_r.py`, `drafter_helper.js`,
    `my_verifier.py`, etc. Doesn't catch substrings inside other words
    (`coder` inside `encoder.py` is fine).
    """
    for part in rel_path.split("/"):
        stem = part.split(".", 1)[0].lower()
        # Normalize: tokenize on _ and -; reassemble for substring match
        # against multi-word needles like "worker_read".
        tokens = re.split(r"[-_]+", stem)
        joined = "_".join(tokens)
        # Multi-word role labels (substring match against rejoined form).
        for needle in _ROLE_FUZZY_NEEDLES:
            if needle in joined:
                return (
                    f"refusing to write to {rel_path!r}: path contains agent "
                    f"role label {needle!r}. Agent role names are internal "
                    "orchestration concepts; pick a project-appropriate name."
                )
        # Single-word role labels (must appear as a discrete token, not a
        # substring of an unrelated word).
        for tok in tokens:
            if tok in _ROLE_SINGLE_TOKENS:
                return (
                    f"refusing to write to {rel_path!r}: path token {tok!r} "
                    "is an agent role label. Agent role names are internal "
                    "orchestration concepts; pick a project-appropriate name."
                )
    return None


def _check_mass_deletion(old_text: str, new_text: str, rel: str) -> str | None:
    """Refuse to collapse a substantial file into a tiny stub."""
    old_lines = old_text.count("\n") + (1 if old_text and not old_text.endswith("\n") else 0)
    new_lines = new_text.count("\n") + (1 if new_text and not new_text.endswith("\n") else 0)
    if old_lines < _MASS_DELETE_OLD_LINES:
        return None
    if new_lines > _MASS_DELETE_NEW_LINES:
        return None
    if new_lines > 0 and (old_lines / new_lines) < _MASS_DELETE_RATIO:
        return None
    return (
        f"refusing to overwrite {rel!r}: would collapse {old_lines}-line "
        f"file to {new_lines}-line stub (mass-deletion blocked). Use "
        "edit_file for surgical changes, or write the FULL replacement "
        "content if rewriting is genuinely intended."
    )


def set_repo_root(path: str | Path) -> None:
    global _REPO_ROOT
    _REPO_ROOT = Path(path).resolve()


def get_repo_root() -> Path | None:
    """Return the currently-set repo root, or None if not yet configured.

    Use this from sibling tool modules (shell.py, git.py, analysis.py)
    instead of `from luxe.tools.fs import _REPO_ROOT`. The import-style
    binds the module-level name once at import time, so subsequent calls
    to set_repo_root() don't propagate — any tool using the imported
    name silently fails with "Repo root not set" forever. The getter
    closes that latent bug (caught by test_tools.py's bash chain-rejection
    suite on 2026-05-02; all three sibling tool modules switched to it
    in the same commit).
    """
    return _REPO_ROOT


def _safe(rel: str) -> Path:
    if _REPO_ROOT is None:
        raise RuntimeError("Repo root not set — call set_repo_root() first")
    resolved = (_REPO_ROOT / rel).resolve()
    if not str(resolved).startswith(str(_REPO_ROOT)):
        raise PermissionError(f"Path escapes repo root: {rel}")
    return resolved


def _check_spec_forbids(rel: str, *, creating: bool) -> str | None:
    """Return error if `rel` matches a `.sdd` Forbids glob in the chain.

    SpecDD Lever 2 enforcement: pre-write tool-side guard. The model
    cannot evade by renaming once a `Forbids:` rule exists in an
    ancestor `.sdd` — the rule fires on every write attempt regardless
    of how the path was constructed.

    `creating` (kwarg-only, required) distinguishes two policy classes:
      - `Forbids` globs always fire (create or edit)
      - `Forbids creating` globs fire only when `creating=True`, i.e.
        the call site has determined the write would create a new file
        at this path (path does not exist as a file)

    Edits to existing files pass `creating=False`. The error message
    shape differs by class so the model's planner gets a clear recovery
    gradient — "wrong operation" (find an existing file) is a different
    signal than "wrong location" (find a different file entirely).

    Returns None when:
      - no repo root is set (test envs that bypass set_repo_root)
      - no `.sdd` files exist in the chain (the common case for repos
        that haven't adopted SpecDD)
      - the path is allowed by the chain

    A malformed `.sdd` upstream raises SddParseError; we convert that
    to a tool-level error so the model sees one actionable message
    rather than a stack trace. Repeat-fires are fine — broken `.sdd`
    is an authoring bug, not a bench-loop concern.
    """
    if _REPO_ROOT is None:
        return None
    target = (_REPO_ROOT / rel).resolve()
    try:
        chain = resolve_chain(_REPO_ROOT, target)
    except SddParseError as e:
        # NOTE: must come before the ValueError catch — SddParseError
        # subclasses ValueError, so the order matters.
        return f"Cannot evaluate Forbids: malformed .sdd — {e}"
    except ValueError:
        # target outside repo_root; _safe() will reject this independently
        return None

    forbidden, sdd, glob = chain.is_forbidden(rel, creating=creating)
    if not forbidden or sdd is None:
        return None
    try:
        sdd_rel = sdd.path.relative_to(_REPO_ROOT)
    except ValueError:
        sdd_rel = sdd.path
    # Distinguish create-only matches: their recovery gradient is "edit
    # an existing file", not "write somewhere else". The existing-file
    # check (creating implies the path doesn't exist) means a glob can
    # only have matched via `forbids_create` if `creating=True` AND the
    # match wasn't on a `forbids` glob. Walk the source SddFile to
    # determine which list matched — cheaper than threading the class
    # back through `is_forbidden`.
    is_create_only = creating and any(g == glob for g in sdd.forbids_create) and not any(
        g == glob for g in sdd.forbids
    )
    if is_create_only:
        return (
            f"refusing to create {rel!r}: forbidden-on-create by {sdd_rel} "
            f"(matches glob {glob!r}). Edit an existing file instead of "
            f"creating a new one — search the repo for the relevant "
            f"existing file and edit it."
        )
    return (
        f"refusing to write {rel!r}: forbidden by {sdd_rel} "
        f"(matches glob {glob!r}). This worker is scoped by .sdd "
        f"contracts; do not write files outside the allowed paths."
    )


def _too_large_message(rel: str, size: int, path: Path) -> str:
    """The oversized-read rejection, phrased as a next call rather than a wall.

    The bare "File too large (N bytes, limit M)" it replaced named no way
    forward, and the tool's own description already promises "use offset/limit
    for large files" — so a model that believed the description retried the
    same unwindowed read, got the same refusal, and fell back to answering
    from whatever grep had already returned. Observed on a 442 KB auth.log
    (session 0e524f033300, 2026-08-11): two rejected reads, then a capped
    8,192-token monologue. State the window, and the model takes it."""
    lines_note = ""
    if size <= _LINE_COUNT_MAX_BYTES:
        try:
            with path.open("rb") as fh:
                n = sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 20), b""))
            lines_note = f", {n:,} lines"
        except OSError:
            pass
    return (
        f"File too large to read whole ({size:,} bytes, limit "
        f"{read_limit():,}{lines_note}). Read it in windows instead — this "
        f"call returns the first 500 lines:\n"
        f'    read_file(path="{rel}", offset=0, limit=500)\n'
        f"Advance `offset` by `limit` for each further window. To find "
        f"specific content without paging the whole file, use "
        f'grep(pattern="...", path="{rel}").'
    )


def _read_file(args: dict[str, Any]) -> tuple[str, str | None]:
    path = _safe(args["path"])
    if not path.is_file():
        return "", f"File not found: {args['path']}"
    size = path.stat().st_size
    # Both are model-supplied and both feed `itertools.islice`, which REJECTS
    # negatives with a ValueError — unlike the list slicing this replaced,
    # where `lines[-1:]` quietly meant "the last line" and `lines[:-5]` meant
    # "all but the last five". Neither was ever a sensible reading of "start
    # line" / "max lines", so clamp rather than reproduce them: a negative
    # offset starts at the top, a negative limit is no limit at all (which on
    # an oversized file yields the actionable refusal, i.e. "pass a limit").
    # tools.sdd: a tool returns its errors, it does not raise them.
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    raw_limit = args.get("limit")
    try:
        limit = int(raw_limit) if raw_limit else None
    except (TypeError, ValueError):
        limit = None
    if limit is not None and limit < 0:
        limit = None

    # Reject obvious binary files — reading them with errors="replace" returns
    # gigabytes of garbage that pollutes the model's context. Null bytes in
    # the first 8 KB is a strong signal: text formats don't contain them, and
    # PNG/JPG/zip/elf/etc. all do. Lets the model see UTF-8/UTF-16 source
    # files without false positives (those don't have null bytes in code).
    # Read 8 KB, not the whole file then slice — the windowed path below
    # accepts files far past _MAX_FILE_SIZE, so this must stay bounded.
    try:
        with path.open("rb") as fh:
            head = fh.read(8192)
    except OSError as e:
        return "", str(e)
    if b"\x00" in head:
        return "", (
            f"File appears to be binary ({args['path']}): null bytes in "
            f"first 8 KB. Use ls / file / a hex dumper if you need to "
            "inspect binary content; this tool is for text source only."
        )

    # The size gate applies to UNWINDOWED reads only. A caller that asked for a
    # window has already bounded what comes back, and `_MAX_READ_BYTES` below
    # bounds it again regardless of how large `limit` is.
    if size > read_limit() and limit is None:
        return "", _too_large_message(args["path"], size, path)

    # Stream the window rather than materialising the file: `limit` is allowed
    # against arbitrarily large files now, so `read_text()` is no longer safe.
    try:
        with path.open("r", errors="replace") as fh:
            for _ in itertools.islice(fh, offset):
                pass
            lines = list(itertools.islice(fh, limit)) if limit else fh.readlines()
    except OSError as e:
        return "", str(e)

    numbered: list[str] = []
    used = 0
    truncated = False
    for i, line in enumerate(lines):
        entry = f"{i + offset + 1}\t{line}"
        if used + len(entry) > read_limit():
            truncated = True
            break
        numbered.append(entry)
        used += len(entry)
    out = "".join(numbered)
    if truncated and not numbered:
        # A SINGLE line longer than the whole byte budget — minified JS, a
        # one-line JSON dump, an embedded base64 blob. Advancing the window
        # cannot help: any offset/limit lands on the same oversized line, so
        # the usual "continue with offset=N" advice would send the model round
        # the identical call forever. Name the real shape of the file instead.
        return "", (
            f"Line {offset + 1} of {args['path']} is larger than the "
            f"{read_limit():,}-byte read budget on its own, so no window "
            f"can return it — the file is probably minified or single-line. "
            f'Use grep(pattern="...", path="{args["path"]}") to pull out the '
            f"parts you need."
        )
    if truncated:
        nxt = offset + len(numbered)
        # Suggest the window that just FIT rather than echoing the caller's
        # `limit` back — a model that asked for 10**9 lines gets a usable
        # number instead of its own absurd one restated as advice.
        out += (
            f"\n[truncated at {read_limit():,} bytes — {len(numbered):,} of "
            f"{len(lines):,} requested lines returned; continue with "
            f'read_file(path="{args["path"]}", offset={nxt}, '
            f"limit={max(1, len(numbered))})]\n"
        )
    return out, None


def _oversize_note(p: Path) -> str:
    """`" (586 KB — too large to read whole; use limit= or grep)"` for a file
    `read_file` will refuse, `""` for everything else.

    Listings used to return bare names, so the ONLY way to learn a file was
    unreadable was to call `read_file` and be refused — one full model
    round-trip per discovery, and the model then has to decide what to do with
    a refusal it did not see coming. Session 0e524f033300 (2026-08-11) spent
    two refused reads on a 442 KB log and then stopped using tools entirely;
    a later probe spent ten calls learning that a 320 KB bundle was one line.
    The size was knowable at `glob` time in both cases.

    Deliberately annotates ONLY files past the limit: a tree with nothing
    oversized produces byte-identical output to before, so the benchmark path
    is untouched except in exactly the case where the note would have helped.
    """
    try:
        if not p.is_file():
            return ""
        size = p.stat().st_size
    except OSError:
        return ""
    if size <= read_limit():
        return ""
    return (f"  ({size / 1024:,.0f} KB — too large to read whole; "
            f"use read_file limit= or grep)")


def _list_dir(args: dict[str, Any]) -> tuple[str, str | None]:
    path = _safe(args.get("path", "."))
    if not path.is_dir():
        return "", f"Not a directory: {args.get('path', '.')}"
    try:
        entries = sorted(path.iterdir())
    except OSError as e:
        # Unreadable directory — a dead network mount times out here
        # (OSError(ETIMEDOUT)). Say so plainly instead of handing the model a
        # raw errno; it can then try a different path.
        return "", f"Cannot read directory {args.get('path', '.')}: {e}"
    lines = []
    for e in entries[:_MAX_RESULTS]:
        suffix = "/" if e.is_dir() else ""
        lines.append(f"{e.name}{suffix}{_oversize_note(e)}")
    result = "\n".join(lines)
    if len(entries) > _MAX_RESULTS:
        result += f"\n... ({len(entries) - _MAX_RESULTS} more)"
    return result, None


def _glob(args: dict[str, Any]) -> tuple[str, str | None]:
    if _REPO_ROOT is None:
        return "", "Repo root not set"
    pattern = args["pattern"]
    matches, stopped = _glob_matches_tolerant(_REPO_ROOT, pattern)
    lines = [f"{m.relative_to(_REPO_ROOT)}{_oversize_note(m)}"
             for m in matches[:_MAX_RESULTS]]
    result = "\n".join(lines)
    if len(matches) > _MAX_RESULTS:
        result += f"\n... ({len(matches) - _MAX_RESULTS} more)"
    if stopped:
        result += f"\n(scan stopped early — {stopped}; results may be incomplete)"
    return result, None


def _glob_matches_tolerant(root: Path, pattern: str) -> tuple[list[Path], str]:
    """`root.glob(pattern)`, sorted, that survives an unreadable directory.

    pathlib's glob generator swallows only `PermissionError`; anything else
    (a timed-out network mount raises `OSError(ETIMEDOUT)`) propagates AND
    kills the generator — it cannot be resumed. So we collect what arrived
    before the failure and report why the list may be short, instead of
    turning a partial answer into a tool error. See `luxe.fswalk`.
    """
    out: list[Path] = []
    it = root.glob(pattern)
    stopped = ""
    while True:
        try:
            out.append(next(it))
        except StopIteration:
            break
        except OSError as e:
            stopped = str(e)
            break
    return sorted(out), stopped


#: ripgrep's per-file match cap and the byte ceiling on what grep returns.
_GREP_MAX_COUNT = 150
_GREP_MAX_BYTES = 32768


def _grep_result(stdout: str) -> str:
    """Grep output, with any truncation SAID OUT LOUD.

    Two caps silently reshaped this result before 2026-08-12: ripgrep's
    `--max-count` (per file) and a `[:32768]` slice. Neither left a trace, so a
    model counting occurrences of something with 1,286 matches was handed
    exactly 150 lines and no reason to doubt them — a wrong answer that looks
    like a complete one. Announcing the cut is the difference between "the
    answer is 150" and "at least 150; narrow the search to count".
    """
    if not stdout:
        return "(no matches)"
    body = stdout[:_GREP_MAX_BYTES]
    notes = []
    if len(stdout) > _GREP_MAX_BYTES:
        body = body[:body.rfind("\n") + 1] or body   # don't end mid-line
        notes.append(f"output truncated at {_GREP_MAX_BYTES:,} bytes")
    # `--max-count` is PER FILE, so the cap shows up as any single file
    # contributing exactly that many lines. Counted over the FULL stdout, not
    # the byte-truncated body: when the byte cap bites first it can cut the
    # output below 150 lines per file and hide the fact that ripgrep capped it
    # too, reporting one truncation while concealing the other.
    per_file: dict[str, int] = {}
    for line in stdout.splitlines():
        per_file[line.split(":", 1)[0]] = per_file.get(line.split(":", 1)[0], 0) + 1
    capped = [f for f, n in per_file.items() if n >= _GREP_MAX_COUNT]
    if capped:
        notes.append(
            f"{len(capped)} file(s) hit the {_GREP_MAX_COUNT}-match-per-file "
            f"cap ({', '.join(sorted(capped)[:3])}"
            f"{'…' if len(capped) > 3 else ''}) — this is NOT the total count"
        )
    if notes:
        body += (f"\n[{'; '.join(notes)}. Narrow with a more specific pattern "
                 f"or `glob`, or count with bash.]\n")
    return body


def _grep(args: dict[str, Any]) -> tuple[str, str | None]:
    if _REPO_ROOT is None:
        return "", "Repo root not set"
    pattern = args["pattern"]
    file_glob = args.get("glob", "")
    try:
        cmd = ["rg", "--no-heading", "-n", "--max-count=150", pattern]
        if file_glob:
            cmd.extend(["--glob", file_glob])
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=_REPO_ROOT, timeout=30,
            # stdin=DEVNULL is LOAD-BEARING (2026-08-12). Invoked with no path
            # argument, ripgrep decides between "search cwd" and "read stdin"
            # by inspecting stdin: an inherited OPEN PIPE makes it read that
            # pipe, hit EOF, and exit 1 with no output — which this function
            # then reports as "(no matches)". Silently, on every search.
            # That is the state of every non-interactive luxe: a benchmark
            # launched from a script, CI, and the headless chat form README
            # documents (`printf 'msg\n/quit\n' | luxe chat`). Passing an
            # explicit "." search path would also fix it but prefixes every
            # result with "./", changing the output format the benchmark and
            # the golden request see; DEVNULL keeps the format byte-identical.
            stdin=subprocess.DEVNULL,
        )
        return _grep_result(proc.stdout), None
    except FileNotFoundError:
        lines = []
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return "", f"Invalid pattern: {e}"
        for root, _, files in os.walk(_REPO_ROOT):
            for f in files:
                if file_glob and not fnmatch.fnmatch(f, file_glob):
                    continue
                fp = Path(root) / f
                try:
                    for i, line in enumerate(fp.open(errors="replace"), 1):
                        if regex.search(line):
                            rel = fp.relative_to(_REPO_ROOT)
                            lines.append(f"{rel}:{i}:{line.rstrip()}")
                            if len(lines) >= _MAX_RESULTS:
                                return "\n".join(lines), None
                except (OSError, UnicodeDecodeError):
                    continue
        return "\n".join(lines) if lines else "(no matches)", None


# Prose extensions the CHAT front-end exempts from the placeholder guard
# (make_prose_aware_write_fns): in notes/docs/session transcripts,
# "# TODO: implement …" or "# paste your token here" is CONTENT the user asked
# to save, not a stub sneaking past review — the guard exists for code writes.
# Code extensions stay guarded everywhere; the benchmark/maintain path uses
# the module TOOL_FNS (prose_exempt=False) and is byte-identical.
_PROSE_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".adoc", ".log", ".csv", ".tsv",
})


def _is_prose_path(rel: str) -> bool:
    return Path(rel).suffix.lower() in _PROSE_EXTENSIONS


def make_prose_aware_write_fns() -> dict[str, ToolFn]:
    """Chat-only write/edit variants: prose files skip the placeholder guard.

    All other guards (role-path, mass-deletion, `_safe` scoping, SpecDD
    Forbids) apply unchanged. Swapped in per-turn through `run_single`'s
    extra-tool seam by the chat front-end in write mode — never the default.
    """
    def _w(args: dict[str, Any]) -> tuple[str, str | None]:
        return _write_file(args, prose_exempt=True)

    def _e(args: dict[str, Any]) -> tuple[str, str | None]:
        return _edit_file(args, prose_exempt=True)
    return {"write_file": _w, "edit_file": _e}


#: Tools `make_read_only_role` strips, and what to say when one is called
#: anyway. Keyed by name so the message can be specific about what the model
#: was trying to do — "no write tool" and "no shell" need different next steps
#: from the user.
_WRITE_GATED_HINTS: dict[str, str] = {
    "write_file": "create or overwrite files",
    "edit_file": "modify files",
    "bash": "run shell commands",
}


def make_write_gated_fns() -> dict[str, ToolFn]:
    """Chat-only read-only-mode stubs for the stripped mutation tools.

    Registered as FNS with no matching DEFS, so the tools stay absent from the
    surface the model is offered (`/write` is still the gate) while a
    hallucinated call gets an error that explains itself instead of
    `Unknown tool: edit_file` — which is false, and which ended a turn with a
    fully-written file body discarded (session 0e524f033300 run -14,
    2026-08-11). Same shape as `make_bash_fn(restricted_hint=True)`: the
    front-end owns the wording of its own toggles, and the benchmark path,
    which passes no extra tools, never sees any of it.
    """
    def _make(name: str, what: str) -> ToolFn:
        def _gated(args: dict[str, Any]) -> tuple[str, str | None]:
            return "", (
                f"{name} is DISABLED: this session is read-only, so you cannot "
                f"{what}. The tool exists and works — it is gated, not missing. "
                f"Nothing was changed on disk. Do not retry this call or look "
                f"for another way to write; tell the user to run /write to "
                f"enable write mode, then continue."
            )
        return _gated
    return {name: _make(name, what) for name, what in _WRITE_GATED_HINTS.items()}


def _write_file(args: dict[str, Any], *, prose_exempt: bool = False,
                ) -> tuple[str, str | None]:
    rel = args["path"]
    content = args["content"]

    # Honesty guards — applied before any I/O so a refusal costs nothing.
    if (err := _check_role_path(rel)):
        return "", err
    if not (prose_exempt and _is_prose_path(rel)):
        if (err := _check_placeholder_text(content)):
            return "", err

    # `_safe` rejects path-escape attempts; convert to a tool error so
    # the call site doesn't have to worry about PermissionError leaking
    # past the dispatch wrapper. Done up front because the Forbids check
    # below needs `path.is_file()` to compute the create-vs-edit signal.
    try:
        path = _safe(rel)
    except (PermissionError, ValueError) as e:
        return "", str(e)
    creating = not path.is_file()

    # SpecDD Lever 2: tool-side Forbids enforcement. Cheap directory
    # walk; no-op when no `.sdd` exists in the chain. `creating`
    # routes `Forbids creating` checks (v1.6) — see _check_spec_forbids.
    if (err := _check_spec_forbids(rel, creating=creating)):
        return "", err

    # Mass-deletion check needs the existing content (if file exists).
    if path.is_file():
        try:
            existing = path.read_text(errors="replace")
        except OSError:
            existing = ""
        if (err := _check_mass_deletion(existing, content, rel)):
            return "", err

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content)
    except Exception as e:
        return "", str(e)
    return f"Wrote {len(content)} bytes to {rel}", None


def _edit_file(args: dict[str, Any], *, prose_exempt: bool = False,
               ) -> tuple[str, str | None]:
    rel = args["path"]
    if (err := _check_role_path(rel)):
        return "", err
    # SpecDD Lever 2: tool-side Forbids — symmetric with _write_file.
    # `creating=False` always: edit_file requires the file to exist
    # (enforced two lines down), so it's structurally never a create.
    if (err := _check_spec_forbids(rel, creating=False)):
        return "", err

    path = _safe(rel)
    if not path.is_file():
        return "", f"File not found: {rel}"
    try:
        text = path.read_text()
    except Exception as e:
        return "", str(e)
    old = args["old_string"]
    new = args["new_string"]

    # Block placeholder text from sneaking in via edits.
    if not (prose_exempt and _is_prose_path(rel)):
        if (err := _check_placeholder_text(new)):
            return "", err

    count = text.count(old)
    if count == 0:
        return "", f"old_string not found in {rel}"
    if count > 1 and not args.get("replace_all", False):
        return "", f"old_string matches {count} times — use replace_all or provide more context"
    new_text = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)

    if (err := _check_mass_deletion(text, new_text, rel)):
        return "", err

    path.write_text(new_text)
    return f"Edited {rel} ({count} replacement{'s' if count > 1 else ''})", None


def read_only_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="read_file",
            # NOTE: "Use offset/limit for large files" was aspirational until
            # 2026-08-11 — the size gate rejected the read before offset/limit
            # were consulted, so a model that believed this description got
            # refused twice (see tests/test_read_file_large.py). The windowed
            # path now makes it true. Wording deliberately UNCHANGED: this
            # description is in every benchmark request body and the golden
            # snapshot pins it (tests/test_golden_request.py).
            description="Read a file's contents with line numbers. Use offset/limit for large files.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from repo root"},
                    "offset": {"type": "integer", "description": "Start line (0-based)"},
                    "limit": {"type": "integer", "description": "Max lines to return"},
                },
                "required": ["path"],
            },
        ),
        ToolDef(
            name="list_dir",
            description="List directory contents. Directories end with /.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path (default: repo root)"},
                },
                "required": [],
            },
        ),
        ToolDef(
            name="glob",
            description="Find files matching a glob pattern (e.g. **/*.py).",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                },
                "required": ["pattern"],
            },
        ),
        ToolDef(
            name="grep",
            description="Search file contents with regex. Uses ripgrep if available.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex search pattern"},
                    "glob": {"type": "string", "description": "File glob filter (e.g. *.py)"},
                },
                "required": ["pattern"],
            },
        ),
    ]


def mutation_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name="write_file",
            description=(
                "Create a new file, or overwrite an existing one, with the given "
                "content. Missing parent directories are created automatically — "
                "use this to scaffold brand-new files and whole directory trees, "
                "not only to rewrite files that already exist."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from repo root"},
                    "content": {"type": "string", "description": "File content to write"},
                },
                "required": ["path", "content"],
            },
        ),
        ToolDef(
            name="edit_file",
            description="Replace a string in a file. old_string must be unique unless replace_all is true.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path from repo root"},
                    "old_string": {"type": "string", "description": "Text to find"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        ),
    ]


READ_ONLY_FNS: dict[str, ToolFn] = {
    "read_file": _read_file,
    "list_dir": _list_dir,
    "glob": _glob,
    "grep": _grep,
}

MUTATION_FNS: dict[str, ToolFn] = {
    "write_file": _write_file,
    "edit_file": _edit_file,
}

CACHEABLE = {"read_file", "list_dir", "glob", "grep"}
