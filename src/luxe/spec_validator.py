"""Spec predicate evaluator — validates a Spec against a working tree.

Lever 1 of SpecDD (see ~/.claude/plans/fluffy-brewing-lemur.md). Takes a
Spec and a repo path and returns per-requirement satisfaction. The
synthesizer uses this before declaring "done"; the cli reprompt block
uses this to construct structured "Requirement R2 unsatisfied" payloads.

Predicate kinds:
- regex_present: pattern matches in N+ added lines of the diff
- regex_absent: pattern must NOT appear in any added line
- tests_pass: shell command exits 0 in repo_path
- ast_query: stubbed at v1.4-prep — full tree-sitter integration deferred
- manual: returns unsatisfied with "needs human review" detail

Diff parsing mirrors `_diff_against_base` in cli.py (uses `git add -N` to
make untracked files visible). The added-line extraction is duplicated
from `_check_regex_present` in `benchmarks/maintain_suite/grade.py` — a
shared `src/luxe/diff.py` module would dedupe both call sites; deferred
until the integration shape is settled.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from luxe.spec import Requirement, Spec


@dataclass(frozen=True)
class RequirementResult:
    """Outcome of a single Requirement's predicate evaluation."""

    requirement: Requirement
    satisfied: bool
    detail: str


@dataclass(frozen=True)
class ValidationResult:
    """Aggregate result of running every Requirement in a Spec.

    `results` preserves Spec.requirements order. `all_satisfied` is the
    AND of every requirement; `unsatisfied` filters to the failing ones
    for use in structured reprompts.
    """

    spec: Spec
    results: list[RequirementResult] = field(default_factory=list)

    @property
    def all_satisfied(self) -> bool:
        return all(r.satisfied for r in self.results)

    @property
    def unsatisfied(self) -> list[RequirementResult]:
        return [r for r in self.results if not r.satisfied]


def validate(spec: Spec, repo_path: str | Path, base_sha: str) -> ValidationResult:
    """Evaluate every requirement in `spec` against the working tree.

    `repo_path` is the working-tree root (where the agent has been editing).
    `base_sha` is the fixture's reference state — diff predicates compare
    against this.

    No early-exit: every requirement is evaluated even if an earlier one
    failed, so the caller (synthesizer or reprompt block) can show the
    full set of unsatisfied items in a single pass.
    """
    repo = Path(repo_path)
    added_lines = _added_lines_from_diff(repo, base_sha)

    results: list[RequirementResult] = []
    for req in spec.requirements:
        if req.kind == "regex_present":
            results.append(_eval_regex_present(req, added_lines))
        elif req.kind == "regex_absent":
            results.append(_eval_regex_absent(req, added_lines))
        elif req.kind == "tests_pass":
            results.append(_eval_tests_pass(req, repo))
        elif req.kind == "ast_query":
            results.append(_eval_ast_query(req))
        elif req.kind == "manual":
            results.append(_eval_manual(req))
        else:
            # spec.py's __post_init__ should prevent this, but defensive
            # coverage in case the validator's kind list drifts from spec.py
            results.append(RequirementResult(
                requirement=req,
                satisfied=False,
                detail=f"unknown kind {req.kind!r}",
            ))

    return ValidationResult(spec=spec, results=results)


# --- diff parsing ----------------------------------------------------------


def _added_lines_from_diff(repo: Path, base_sha: str) -> list[tuple[str, str]]:
    """Return [(filename, line_body)] for every '+'-prefixed line in the diff.

    Untracked files are made visible via `git add -N .` (intent-to-add):
    without this, files newly created by `write_file` are absent from
    `git diff <base_sha>` until staged. PR cycle's later real `git add .`
    still works correctly. Same fix as cli.py's `_diff_against_base`.

    Returns an empty list when base_sha is unset or the repo has no diff.
    """
    if not base_sha:
        return []

    subprocess.run(
        ["git", "add", "-N", "."],
        cwd=str(repo), capture_output=True, text=True,
    )
    proc = subprocess.run(
        ["git", "diff", base_sha, "--"],
        cwd=str(repo), capture_output=True, text=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return []

    out: list[tuple[str, str]] = []
    current_file = ""
    for line in proc.stdout.splitlines():
        if line.startswith("+++"):
            # `+++ b/path/to/file` or `+++ /dev/null`
            current_file = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((current_file, line[1:]))
    return out


# --- per-kind evaluators ---------------------------------------------------


def _eval_regex_present(
    req: Requirement,
    added_lines: list[tuple[str, str]],
) -> RequirementResult:
    """Pattern must hit in ≥ min_matches distinct added lines.

    Mirrors `_check_regex_present` in benchmarks/maintain_suite/grade.py
    but operates on per-Requirement inputs. The detail message identifies
    the requirement so the synthesizer can surface "R2 unsatisfied: only
    1 match, needed ≥3".
    """
    assert req.pattern is not None  # spec.py validates this
    rx = re.compile(req.pattern)
    matches = [fname for fname, body in added_lines if rx.search(body)]

    if len(matches) >= req.min_matches:
        if req.min_matches == 1:
            first = matches[0] if matches else "(no file)"
            return RequirementResult(
                requirement=req,
                satisfied=True,
                detail=f"{req.id} matched in added line of {first}",
            )
        return RequirementResult(
            requirement=req,
            satisfied=True,
            detail=(
                f"{req.id} matched in {len(matches)} added lines "
                f"(needed ≥{req.min_matches}); first: {matches[0]}"
            ),
        )

    return RequirementResult(
        requirement=req,
        satisfied=False,
        detail=(
            f"{req.id} pattern matched {len(matches)}× in "
            f"{len(added_lines)} added lines (needed ≥{req.min_matches})"
        ),
    )


def _eval_regex_absent(
    req: Requirement,
    added_lines: list[tuple[str, str]],
) -> RequirementResult:
    """Pattern must NOT appear in any added line.

    Used for "no placeholder TODOs", "no role_name leaks", etc. Returns
    the first matching line as evidence so the synthesizer can show
    exactly what should be removed.
    """
    assert req.pattern is not None
    rx = re.compile(req.pattern)
    for fname, body in added_lines:
        if rx.search(body):
            return RequirementResult(
                requirement=req,
                satisfied=False,
                detail=(
                    f"{req.id} forbidden pattern matched in {fname}: "
                    f"{body.strip()[:120]}"
                ),
            )
    return RequirementResult(
        requirement=req,
        satisfied=True,
        detail=f"{req.id} pattern absent across {len(added_lines)} added lines",
    )


def _eval_tests_pass(req: Requirement, repo: Path) -> RequirementResult:
    """Shell out to req.command; pass iff exit 0.

    Mirrors `_check_tests_pass` in grade.py. Captures the last 30 lines
    of output for the detail field — enough to surface the failure
    reason without flooding the synthesizer prompt.
    """
    assert req.command is not None  # spec.py validates this
    try:
        proc = subprocess.run(
            ["bash", "-lc", req.command],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=600.0,
        )
    except subprocess.TimeoutExpired:
        return RequirementResult(
            requirement=req,
            satisfied=False,
            detail=f"{req.id} `{req.command}` timed out (>600s)",
        )

    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    tail = "\n".join(out.splitlines()[-30:])
    if proc.returncode == 0:
        return RequirementResult(
            requirement=req,
            satisfied=True,
            detail=f"{req.id} `{req.command}` exited 0",
        )
    return RequirementResult(
        requirement=req,
        satisfied=False,
        detail=f"{req.id} `{req.command}` rc={proc.returncode}; tail:\n{tail}"[:1500],
    )


def _eval_ast_query(req: Requirement) -> RequirementResult:
    """Stubbed at v1.4-prep; returns unsatisfied with deferral notice.

    Full implementation will use `src/luxe/symbols.py`'s `SymbolIndex`
    to evaluate a tree-sitter query against the post-edit working tree.
    Deferred because integration requires plumbing the index reference
    through the validate() entry point (current global-state pattern in
    symbols.py is convenient for the in-loop tool but awkward for a
    validation-time query). Tracked in lessons.md when ast_query
    requirements are first authored in fixtures.
    """
    return RequirementResult(
        requirement=req,
        satisfied=False,
        detail=(
            f"{req.id} ast_query not yet implemented at v1.4-prep; "
            "requirement reports unsatisfied to surface this clearly."
        ),
    )


def _eval_manual(req: Requirement) -> RequirementResult:
    """Always returns unsatisfied — manual requirements need human review.

    Synthesizer should treat manual requirements specially: surface them
    in the final report for human attention but not block the run on them
    (otherwise every fixture with a manual requirement loops forever).
    The cli reprompt block should NOT cycle on manual requirements.
    """
    return RequirementResult(
        requirement=req,
        satisfied=False,
        detail=(
            f"{req.id} manual review required: {req.done_when}"
        ),
    )
