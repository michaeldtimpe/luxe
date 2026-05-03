"""Spec — programmatic Definition of Done for fixture goals.

This is Lever 1 of the SpecDD phase (see ~/.claude/plans/fluffy-brewing-lemur.md).
A Spec decomposes a prose goal into a list of Requirements, each with its own
done-when predicate kind. Synthesizer validates against the Spec before declaring
"done" rather than relying on diff-size heuristics.

Status note (2026-05-03): the SpecDD plan was authored under the assumption that
compound-goal shadowing was the bench's primary ceiling. Post-v1.3 audit
(`project_compound_goal_audit.md`) showed compound goals are addressed
correctly by the current model on the current bench. This data model still
ships at v1.4 because:
  1. **Programmatic Definition of Done** has architectural value beyond
     compound-goal rescue (replaces the diff-size heuristic in the v1.3
     reprompt with a per-requirement check).
  2. **Per-requirement grading** is bench-rigor work — the loose-grader
     audit (`project_loose_grader_audit.md`) found 5/10 fixtures' regex
     graders are looser than their goal text. Per-requirement schema
     forces fixture authors to enumerate each sub-deliverable.
  3. **Future Lever 2/3** depend on this data model for `.sdd` chains
     and methodology A/B.

Prose-mode rescue (the residual nothing-doc-config FAIL) is NOT a deliverable
of this Spec model — directive reprompt's 0/1 observed rescue rate suggests
prompt-based reprompt mechanisms don't reliably break the model's prose-mode
decoder loop. Structural fixes (tool-level intervention) are out of scope here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RequirementKind = Literal[
    "regex_present",   # pattern matches in N+ added lines of the diff
    "regex_absent",    # pattern must NOT appear in any added line
    "ast_query",       # tree-sitter symbol-index query (Lever 1 scaffolding)
    "tests_pass",      # shell command exits 0 (e.g., `npm test`, `pytest`)
    "manual",          # human-graded; predicate description is the rubric
]


@dataclass(frozen=True)
class Requirement:
    """One sub-deliverable of a fixture goal, with a checkable predicate.

    `id` is human-stable across spec revisions ("R1", "R2"); the synthesizer
    references requirements by id in structured reprompts. `must` is the
    normative statement (one line) that goes into the synthesizer's checklist.
    `done_when` is the human-readable predicate description that the
    synthesizer surfaces alongside `must` so the model knows HOW it's being
    graded — not just what's required. `kind` selects the predicate
    evaluator; `pattern` and `min_matches` are kind-specific.
    """

    id: str
    must: str
    done_when: str
    kind: RequirementKind
    pattern: str | None = None
    min_matches: int = 1
    command: str | None = None  # for tests_pass kind

    def __post_init__(self) -> None:
        if not self.id or not self.id.startswith("R"):
            raise ValueError(
                f"Requirement.id must start with 'R' (got {self.id!r}). "
                "Convention: R1, R2, ... in fixture-author order."
            )
        if not self.must.strip():
            raise ValueError("Requirement.must cannot be empty.")
        if self.kind in ("regex_present", "regex_absent") and not self.pattern:
            raise ValueError(
                f"Requirement {self.id} kind={self.kind} requires a `pattern`."
            )
        if self.kind == "tests_pass" and not self.command:
            raise ValueError(
                f"Requirement {self.id} kind=tests_pass requires a `command`."
            )
        if self.min_matches < 1:
            raise ValueError(
                f"Requirement {self.id} min_matches must be >= 1 "
                f"(got {self.min_matches})."
            )


@dataclass(frozen=True)
class Spec:
    """A fixture's goal as a structured set of requirements.

    `goal` retains the original prose for human readability and as the input
    the model sees in the task prompt. `requirements` is the validation list
    used by the synthesizer's spec-validation gate. `forbids` is path globs
    that any write tool must refuse to write — pre-write enforcement, scoped
    to the fixture run, separate from the broader `.sdd` Forbids in Lever 2.
    """

    goal: str
    requirements: list[Requirement] = field(default_factory=list)
    forbids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.goal.strip():
            raise ValueError("Spec.goal cannot be empty.")
        ids = [r.id for r in self.requirements]
        if len(ids) != len(set(ids)):
            dups = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(
                f"Spec has duplicate requirement ids: {dups}. "
                "Each requirement must have a unique id."
            )

    def requirement(self, req_id: str) -> Requirement:
        """Look up a requirement by id; raises KeyError on miss."""
        for r in self.requirements:
            if r.id == req_id:
                return r
        raise KeyError(
            f"Requirement {req_id!r} not in spec. "
            f"Available: {[r.id for r in self.requirements]}"
        )


def spec_from_yaml_dict(d: dict[str, Any]) -> Spec:
    """Construct a Spec from a fixture-YAML dict.

    Schema (added at Lever 1 to fixtures.yaml):
        goal: <prose>
        requirements:
          - id: R1
            must: <one-line statement>
            done_when: <predicate description>
            kind: regex_present | regex_absent | ast_query | tests_pass | manual
            pattern: <regex>           # for regex_*/ast_query kinds
            min_matches: 3             # for regex_present
            command: "npm test"        # for tests_pass
        forbids:
          - "**/*_test.py"             # path globs (Lever 2 will add `.sdd`-resolved Forbids; this is fixture-scoped)

    Empty `requirements` list is valid (transitional — fixtures not yet
    migrated keep their `expected_outcome` and have no spec).
    """
    goal = d.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("yaml dict missing or empty `goal` field")

    raw_reqs = d.get("requirements") or []
    if not isinstance(raw_reqs, list):
        raise ValueError("`requirements` must be a list (or omitted)")

    requirements = [_requirement_from_yaml_dict(r) for r in raw_reqs]

    forbids = d.get("forbids") or []
    if not isinstance(forbids, list) or not all(isinstance(f, str) for f in forbids):
        raise ValueError("`forbids` must be a list of glob strings")

    return Spec(goal=goal, requirements=requirements, forbids=list(forbids))


def _requirement_from_yaml_dict(d: dict[str, Any]) -> Requirement:
    """Construct a Requirement from a YAML sub-dict; raise on schema errors."""
    if not isinstance(d, dict):
        raise ValueError(f"requirement entry must be a dict, got {type(d).__name__}")
    required_keys = {"id", "must", "done_when", "kind"}
    missing = required_keys - d.keys()
    if missing:
        raise ValueError(
            f"requirement {d.get('id', '<unnamed>')} missing keys: {sorted(missing)}"
        )
    kind = d["kind"]
    valid_kinds = {"regex_present", "regex_absent", "ast_query", "tests_pass", "manual"}
    if kind not in valid_kinds:
        raise ValueError(
            f"requirement {d['id']} kind={kind!r} not in {sorted(valid_kinds)}"
        )

    return Requirement(
        id=d["id"],
        must=d["must"],
        done_when=d["done_when"],
        kind=kind,
        pattern=d.get("pattern"),
        min_matches=int(d.get("min_matches", 1)),
        command=d.get("command"),
    )


def spec_to_yaml_dict(spec: Spec) -> dict[str, Any]:
    """Serialize a Spec back to a YAML-friendly dict.

    Round-trips with `spec_from_yaml_dict` for any spec constructed from a
    valid YAML dict. Used for fixture-author tooling (e.g., generating a
    starter spec from an existing `expected_outcome` clause).
    """
    out: dict[str, Any] = {"goal": spec.goal}
    if spec.requirements:
        out["requirements"] = [_requirement_to_yaml_dict(r) for r in spec.requirements]
    if spec.forbids:
        out["forbids"] = list(spec.forbids)
    return out


def _requirement_to_yaml_dict(r: Requirement) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": r.id,
        "must": r.must,
        "done_when": r.done_when,
        "kind": r.kind,
    }
    if r.pattern is not None:
        out["pattern"] = r.pattern
    if r.kind == "regex_present":
        out["min_matches"] = r.min_matches
    if r.command is not None:
        out["command"] = r.command
    return out
