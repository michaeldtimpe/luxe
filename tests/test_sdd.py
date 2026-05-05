"""Tests for src/luxe/sdd.py — `.sdd` parser (Lever 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe.sdd import (
    CANONICAL_SECTIONS,
    SddFile,
    SddParseError,
    parse_sdd,
    parse_sdd_file,
)


class TestParseTitle:
    def test_h1_wins(self):
        sf = parse_sdd("# luxe\n\n## Must\n- ship\n")
        assert sf.title == "luxe"

    def test_h2_does_not_count_as_h1(self):
        sf = parse_sdd("## Must\n- ship\n", path=Path("/tmp/agents.sdd"))
        assert sf.title == "agents"

    def test_falls_back_to_stem_when_no_h1(self):
        sf = parse_sdd("just prose, no headers\n", path=Path("/x/luxe.sdd"))
        assert sf.title == "luxe"

    def test_inline_default_when_no_h1_no_path(self):
        sf = parse_sdd("just prose\n")
        assert sf.title == "<inline>"

    def test_first_h1_wins_when_multiple(self):
        sf = parse_sdd("# first\n# second\n")
        assert sf.title == "first"

    def test_empty_h1_falls_back_to_stem(self):
        sf = parse_sdd("# \n", path=Path("/tmp/foo.sdd"))
        assert sf.title == "foo"


class TestSectionParsing:
    def test_must_section_collects_bullets(self):
        sf = parse_sdd(
            "# title\n"
            "## Must\n"
            "- statement one\n"
            "- statement two\n"
        )
        assert sf.must == ["statement one", "statement two"]

    def test_all_six_canonical_sections(self):
        sf = parse_sdd(
            "# t\n"
            "## Must\n- m1\n"
            "## Must not\n- mn1\n"
            "## Owns\n- src/luxe/**\n"
            "## Depends on\n- src/luxe/agents/\n"
            "## Forbids\n- tests/**\n"
            "## Done when\n- d1\n"
        )
        assert sf.must == ["m1"]
        assert sf.must_not == ["mn1"]
        assert sf.owns == ["src/luxe/**"]
        assert sf.depends_on == ["src/luxe/agents/"]
        assert sf.forbids == ["tests/**"]
        assert sf.done_when == ["d1"]

    def test_canonical_section_list_matches_dataclass_fields(self):
        # Smoke check: if someone adds a section to CANONICAL_SECTIONS,
        # they need to add the dataclass field too. Catches drift.
        sf = parse_sdd("")
        for name in CANONICAL_SECTIONS:
            assert hasattr(sf, name), f"SddFile missing attribute {name!r}"

    @pytest.mark.parametrize(
        "header",
        ["Must not", "must not", "MUST NOT", "Must_not", "must_not", "must-not"],
    )
    def test_must_not_normalization_variants(self, header):
        sf = parse_sdd(f"## {header}\n- x\n")
        assert sf.must_not == ["x"]

    @pytest.mark.parametrize(
        "header",
        ["Depends on", "depends_on", "depends-on", "DEPENDS ON"],
    )
    def test_depends_on_normalization_variants(self, header):
        sf = parse_sdd(f"## {header}\n- y\n")
        assert sf.depends_on == ["y"]

    @pytest.mark.parametrize(
        "header",
        ["Done when", "done_when", "done-when"],
    )
    def test_done_when_normalization_variants(self, header):
        sf = parse_sdd(f"## {header}\n- z\n")
        assert sf.done_when == ["z"]

    def test_unknown_section_silently_ignored(self):
        # Forward compat: tomorrow's SDD may add sections. Today's parser
        # should not reject tomorrow's files.
        sf = parse_sdd(
            "## Notes\n"
            "- some random thing\n"
            "## Must\n"
            "- real statement\n"
        )
        assert sf.must == ["real statement"]

    def test_preamble_prose_dropped(self):
        sf = parse_sdd(
            "# title\n"
            "\n"
            "Free-form preamble that is not a bullet.\n"
            "\n"
            "## Must\n"
            "- bullet\n"
        )
        assert sf.must == ["bullet"]

    def test_blank_lines_between_bullets_ok(self):
        sf = parse_sdd("## Must\n- one\n\n- two\n")
        assert sf.must == ["one", "two"]

    def test_prose_between_bullets_ignored(self):
        sf = parse_sdd(
            "## Must\n"
            "- bullet one\n"
            "Some clarifying prose.\n"
            "- bullet two\n"
        )
        assert sf.must == ["bullet one", "bullet two"]

    def test_empty_bullets_skipped(self):
        sf = parse_sdd("## Must\n- \n- real\n")
        assert sf.must == ["real"]

    def test_empty_section_yields_empty_list(self):
        sf = parse_sdd("## Must\n\n## Owns\n- src/**\n")
        assert sf.must == []
        assert sf.owns == ["src/**"]

    def test_all_sections_optional(self):
        sf = parse_sdd("# only-title\n")
        for name in CANONICAL_SECTIONS:
            assert getattr(sf, name) == []


class TestStrictness:
    def test_duplicate_section_raises(self):
        with pytest.raises(SddParseError, match="duplicate section"):
            parse_sdd("## Must\n- a\n## Must\n- b\n")

    def test_duplicate_section_after_normalization_raises(self):
        # `Must not` and `must_not` collide.
        with pytest.raises(SddParseError, match="duplicate section"):
            parse_sdd("## Must not\n- a\n## must_not\n- b\n")

    def test_error_carries_path(self):
        try:
            parse_sdd("## Must\n- a\n## Must\n- b\n", path=Path("/tmp/x.sdd"))
        except SddParseError as e:
            assert e.path == Path("/tmp/x.sdd")
            assert "/tmp/x.sdd" in str(e)
        else:
            pytest.fail("expected SddParseError")


class TestFileLoading:
    def test_parse_sdd_file_round_trip(self, tmp_path):
        p = tmp_path / "demo.sdd"
        p.write_text(
            "# demo\n"
            "## Must\n"
            "- ship it\n"
            "## Forbids\n"
            "- tests/**\n",
            encoding="utf-8",
        )
        sf = parse_sdd_file(p)
        assert sf.title == "demo"
        assert sf.must == ["ship it"]
        assert sf.forbids == ["tests/**"]
        assert sf.path == p.resolve()

    def test_parse_sdd_file_missing_raises(self, tmp_path):
        p = tmp_path / "nope.sdd"
        with pytest.raises(SddParseError, match="not a file"):
            parse_sdd_file(p)

    def test_parse_sdd_file_directory_raises(self, tmp_path):
        with pytest.raises(SddParseError, match="not a file"):
            parse_sdd_file(tmp_path)


class TestRealisticContents:
    def test_root_luxe_sdd_shape(self):
        # Mimics the dogfood `src/luxe/luxe.sdd` we'll author next.
        sf = parse_sdd(
            "# luxe\n"
            "\n"
            "Root invariants for the luxe codebase.\n"
            "\n"
            "## Must\n"
            "- temp=0.0 in production configs\n"
            "- single mono runner only\n"
            "## Must not\n"
            "- read origin/<branch> in offline cache\n"
            "- use MoE Instruct-2507\n"
            "## Owns\n"
            "- src/luxe/**\n"
            "## Forbids\n"
            "- src/swarm/**\n"
            "- src/micro/**\n"
            "- src/phased/**\n",
        )
        assert "temp=0.0 in production configs" in sf.must
        assert "use MoE Instruct-2507" in sf.must_not
        assert sf.owns == ["src/luxe/**"]
        assert sf.forbids == ["src/swarm/**", "src/micro/**", "src/phased/**"]
        assert sf.depends_on == []
        assert sf.done_when == []
