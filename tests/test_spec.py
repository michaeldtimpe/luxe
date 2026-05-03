"""Tests for src/luxe/spec.py — SpecDD Lever 1 data model.

Covers Requirement and Spec construction, validation, lookup, YAML round-trip.
"""

from __future__ import annotations

import pytest

from luxe.spec import (
    Requirement,
    Spec,
    spec_from_yaml_dict,
    spec_to_yaml_dict,
)


class TestRequirementConstruction:
    def test_minimal_regex_requirement(self):
        r = Requirement(
            id="R1",
            must="A module docstring exists at the top of pe_scan.py",
            done_when="regex `^\"\"\"` matches in added lines",
            kind="regex_present",
            pattern=r'^"""',
            min_matches=1,
        )
        assert r.id == "R1"
        assert r.kind == "regex_present"
        assert r.min_matches == 1

    def test_tests_pass_requirement(self):
        r = Requirement(
            id="R1",
            must="Existing test suite passes",
            done_when="`npm test` exits 0",
            kind="tests_pass",
            command="npm test",
        )
        assert r.command == "npm test"
        assert r.pattern is None

    def test_manual_requirement_no_pattern_required(self):
        r = Requirement(
            id="R2",
            must="Implementation is idiomatic",
            done_when="reviewer judgement",
            kind="manual",
        )
        assert r.pattern is None
        assert r.command is None

    def test_regex_absent_requires_pattern(self):
        with pytest.raises(ValueError, match="requires a `pattern`"):
            Requirement(
                id="R1",
                must="No placeholder TODOs",
                done_when="pattern absent in added lines",
                kind="regex_absent",
                pattern=None,
            )

    def test_regex_present_requires_pattern(self):
        with pytest.raises(ValueError, match="requires a `pattern`"):
            Requirement(
                id="R1", must="x", done_when="x", kind="regex_present", pattern=None,
            )

    def test_tests_pass_requires_command(self):
        with pytest.raises(ValueError, match="requires a `command`"):
            Requirement(
                id="R1", must="x", done_when="x", kind="tests_pass",
            )

    def test_id_must_start_with_R(self):
        with pytest.raises(ValueError, match="must start with 'R'"):
            Requirement(
                id="X1", must="x", done_when="x", kind="manual",
            )

    def test_id_cannot_be_empty(self):
        with pytest.raises(ValueError, match="must start with 'R'"):
            Requirement(
                id="", must="x", done_when="x", kind="manual",
            )

    def test_must_cannot_be_empty(self):
        with pytest.raises(ValueError, match="must cannot be empty"):
            Requirement(
                id="R1", must="   ", done_when="x", kind="manual",
            )

    def test_min_matches_must_be_positive(self):
        with pytest.raises(ValueError, match="min_matches must be >= 1"):
            Requirement(
                id="R1",
                must="x",
                done_when="x",
                kind="regex_present",
                pattern="x",
                min_matches=0,
            )


class TestSpecConstruction:
    def test_empty_requirements_allowed(self):
        s = Spec(goal="prose goal")
        assert s.goal == "prose goal"
        assert s.requirements == []
        assert s.forbids == []

    def test_single_requirement(self):
        r = Requirement(
            id="R1", must="x", done_when="y", kind="manual",
        )
        s = Spec(goal="goal", requirements=[r])
        assert len(s.requirements) == 1
        assert s.requirements[0].id == "R1"

    def test_goal_cannot_be_empty(self):
        with pytest.raises(ValueError, match="goal cannot be empty"):
            Spec(goal="")
        with pytest.raises(ValueError, match="goal cannot be empty"):
            Spec(goal="   ")

    def test_duplicate_requirement_ids_rejected(self):
        r1 = Requirement(id="R1", must="x", done_when="y", kind="manual")
        r1_dup = Requirement(id="R1", must="z", done_when="w", kind="manual")
        with pytest.raises(ValueError, match=r"duplicate requirement ids: \['R1'\]"):
            Spec(goal="g", requirements=[r1, r1_dup])

    def test_requirement_lookup_by_id(self):
        r1 = Requirement(id="R1", must="x", done_when="y", kind="manual")
        r2 = Requirement(id="R2", must="z", done_when="w", kind="manual")
        s = Spec(goal="g", requirements=[r1, r2])
        assert s.requirement("R1") is r1
        assert s.requirement("R2") is r2

    def test_requirement_lookup_miss_raises(self):
        s = Spec(goal="g", requirements=[
            Requirement(id="R1", must="x", done_when="y", kind="manual"),
        ])
        with pytest.raises(KeyError, match="R99"):
            s.requirement("R99")

    def test_forbids_globs_preserved(self):
        s = Spec(goal="g", forbids=["**/*_test.py", "secrets/**"])
        assert s.forbids == ["**/*_test.py", "secrets/**"]


class TestYamlRoundTrip:
    def test_minimal_spec_from_yaml(self):
        d = {"goal": "do a thing"}
        s = spec_from_yaml_dict(d)
        assert s.goal == "do a thing"
        assert s.requirements == []
        assert s.forbids == []

    def test_spec_with_requirements_from_yaml(self):
        d = {
            "goal": "Add docstring AND types",
            "requirements": [
                {
                    "id": "R1",
                    "must": "module docstring at file top",
                    "done_when": "regex `^\"\"\"` in added lines",
                    "kind": "regex_present",
                    "pattern": r'^"""',
                    "min_matches": 1,
                },
                {
                    "id": "R2",
                    "must": "all top-level params typed",
                    "done_when": "regex matches in added lines",
                    "kind": "regex_present",
                    "pattern": r"def \w+\(",
                    "min_matches": 2,
                },
            ],
            "forbids": ["**/*_test.py"],
        }
        s = spec_from_yaml_dict(d)
        assert len(s.requirements) == 2
        assert s.requirement("R2").min_matches == 2
        assert s.forbids == ["**/*_test.py"]

    def test_round_trip_preserves_essentials(self):
        d = {
            "goal": "x",
            "requirements": [
                {
                    "id": "R1",
                    "must": "y",
                    "done_when": "z",
                    "kind": "regex_present",
                    "pattern": "p",
                    "min_matches": 3,
                },
                {
                    "id": "R2",
                    "must": "tests pass",
                    "done_when": "exit 0",
                    "kind": "tests_pass",
                    "command": "pytest",
                },
            ],
            "forbids": ["a/**"],
        }
        s = spec_from_yaml_dict(d)
        out = spec_to_yaml_dict(s)
        s2 = spec_from_yaml_dict(out)
        assert s == s2

    def test_unknown_kind_rejected(self):
        d = {
            "goal": "x",
            "requirements": [
                {
                    "id": "R1",
                    "must": "y",
                    "done_when": "z",
                    "kind": "magic_pixie_dust",
                },
            ],
        }
        with pytest.raises(ValueError, match="not in"):
            spec_from_yaml_dict(d)

    def test_missing_keys_rejected(self):
        d = {"goal": "x", "requirements": [{"id": "R1"}]}
        with pytest.raises(ValueError, match="missing keys"):
            spec_from_yaml_dict(d)

    def test_missing_goal_rejected(self):
        with pytest.raises(ValueError, match="missing or empty `goal`"):
            spec_from_yaml_dict({})

    def test_requirements_not_a_list_rejected(self):
        with pytest.raises(ValueError, match="must be a list"):
            spec_from_yaml_dict({"goal": "x", "requirements": "not a list"})

    def test_forbids_must_be_string_globs(self):
        with pytest.raises(ValueError, match="list of glob strings"):
            spec_from_yaml_dict({"goal": "x", "forbids": [1, 2, 3]})

    def test_yaml_omits_empty_requirements_and_forbids(self):
        s = Spec(goal="just prose")
        out = spec_to_yaml_dict(s)
        assert "requirements" not in out
        assert "forbids" not in out
        assert out == {"goal": "just prose"}

    def test_yaml_includes_min_matches_only_for_regex_present(self):
        # regex_present should serialize min_matches; tests_pass should not.
        r1 = Requirement(
            id="R1",
            must="x",
            done_when="y",
            kind="regex_present",
            pattern="p",
            min_matches=2,
        )
        r2 = Requirement(
            id="R2",
            must="x",
            done_when="y",
            kind="tests_pass",
            command="cmd",
        )
        s = Spec(goal="g", requirements=[r1, r2])
        out = spec_to_yaml_dict(s)
        req_dicts = {r["id"]: r for r in out["requirements"]}
        assert req_dicts["R1"]["min_matches"] == 2
        assert "min_matches" not in req_dicts["R2"]
