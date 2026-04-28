"""Tests for the validator's structured-envelope output contract."""

from __future__ import annotations

import json

from luxe.agents.validator import (
    ValidatorEnvelope,
    ValidatorFinding,
    ValidatorRemoved,
    parse_envelope,
)


def _wrap_json(d: dict) -> str:
    return json.dumps(d)


def test_parse_clean_verified():
    text = _wrap_json({
        "status": "verified",
        "verified": [
            {"path": "src/foo.py", "line": 42, "snippet": "x = 1\ny = 2",
             "severity": "high", "description": "off-by-one"}
        ],
        "removed": [],
        "summary": "all clean",
    })
    env = parse_envelope(text, input_finding_count=1)
    assert env.status == "verified"
    assert len(env.verified) == 1
    assert env.verified[0].path == "src/foo.py"
    assert env.verified[0].line == 42
    assert env.verified[0].severity == "high"


def test_parse_cleared_no_findings():
    text = _wrap_json({"status": "cleared", "verified": [], "removed": [], "summary": "nothing to do"})
    env = parse_envelope(text, input_finding_count=0)
    assert env.is_cleared
    assert env.verified == []


def test_parse_ambiguous_when_majority_removed():
    # Validator returned 1 verified, 5 removed → ambiguous
    text = _wrap_json({
        "status": "verified",
        "verified": [{"path": "a.py", "line": 1, "snippet": "x", "severity": "low", "description": "y"}],
        "removed": [
            {"original": f"finding {i}", "reason": "file_not_found"}
            for i in range(5)
        ],
        "summary": "",
    })
    env = parse_envelope(text, input_finding_count=6)
    # Even though model claimed "verified", >50% removed → override to ambiguous
    assert env.is_ambiguous


def test_parse_handles_markdown_fence():
    text = "```json\n" + _wrap_json({"status": "cleared", "verified": [], "removed": [], "summary": ""}) + "\n```"
    env = parse_envelope(text, input_finding_count=0)
    assert env.is_cleared


def test_parse_handles_prose_preamble():
    text = "Here is the result:\n\n" + _wrap_json({
        "status": "verified",
        "verified": [{"path": "a.py", "line": 1, "snippet": "x", "severity": "low", "description": "y"}],
        "removed": [],
        "summary": "",
    }) + "\n\nLet me know if you have questions."
    env = parse_envelope(text, input_finding_count=1)
    assert env.status == "verified"
    assert len(env.verified) == 1


def test_parse_malformed_falls_back_to_ambiguous():
    text = "no json here, just prose about findings"
    env = parse_envelope(text, input_finding_count=3)
    assert env.is_ambiguous
    assert env.removed and env.removed[0].reason == "malformed"


def test_parse_malformed_with_no_input_findings_is_cleared():
    # Edge case: no input findings AND model returned no JSON → cleared (legit)
    env = parse_envelope("", input_finding_count=0)
    assert env.is_cleared


def test_parse_status_corrects_cleared_with_findings():
    # Model said "cleared" but emitted findings — sanity-correct.
    text = _wrap_json({
        "status": "cleared",
        "verified": [{"path": "a.py", "line": 1, "snippet": "x", "severity": "low", "description": "y"}],
        "removed": [],
        "summary": "",
    })
    env = parse_envelope(text, input_finding_count=1)
    assert env.status == "verified"


def test_parse_skips_malformed_finding_entries():
    # Model emitted a verified entry that's not a dict
    text = _wrap_json({
        "status": "verified",
        "verified": [
            {"path": "a.py", "line": 1, "snippet": "x", "severity": "low", "description": "y"},
            "bogus string entry",
            {"path": "b.py", "line": "not-an-int"},
        ],
        "removed": [],
        "summary": "",
    })
    env = parse_envelope(text, input_finding_count=3)
    assert len(env.verified) == 1
    assert env.verified[0].path == "a.py"


def test_envelope_helper_properties():
    e1 = ValidatorEnvelope(status="cleared")
    assert e1.is_cleared and not e1.is_ambiguous

    e2 = ValidatorEnvelope(status="ambiguous")
    assert e2.is_ambiguous and not e2.is_cleared

    e3 = ValidatorEnvelope(status="verified")
    assert not e3.is_cleared and not e3.is_ambiguous
