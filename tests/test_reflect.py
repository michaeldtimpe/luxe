"""Tests for the reflect verify primitive (Track 1).

Pure-function: no backend, no model, deterministic + offline. Covers the verdict
parser's substantiation guard + fail-closed behavior, specificity normalization, the
whole-conversation context builder, and the env gate.
"""

from __future__ import annotations

from luxe.agents import reflect as R


# --- parse_verdict ----------------------------------------------------------

def test_parse_verdict_substantiated_gap():
    v = R.parse_verdict(
        '{"gap": true, "deficiencies": [{"what": "did not book flight", '
        '"evidence": "no book_flight in actions", "specificity": "concrete_local"}]}'
    )
    assert v.gap is True
    assert len(v.deficiencies) == 1
    assert v.deficiencies[0].specificity == "concrete_local"
    assert v.ok


def test_parse_verdict_substantiation_guard():
    """gap asserted but with zero deficiencies must NOT flip the gap — the
    load-bearing guard against unsubstantiated false-gaps."""
    v = R.parse_verdict('{"gap": true, "deficiencies": []}')
    assert v.gap is False
    assert v.ok  # parsed cleanly; just no substantiated gap


def test_parse_verdict_clean_pass():
    v = R.parse_verdict('Sure, here you go: {"gap": false, "deficiencies": []} done.')
    assert v.gap is False
    assert v.ok


def test_parse_verdict_unparseable_fails_closed():
    v = R.parse_verdict("I think it's fine, no JSON here at all.")
    assert v.gap is False
    assert not v.ok
    assert v.error == "unparseable_verdict"


def test_parse_verdict_normalizes_unknown_specificity():
    v = R.parse_verdict(
        '{"gap": true, "deficiencies": [{"what": "x", "evidence": "y", "specificity": "weird"}]}'
    )
    assert v.deficiencies[0].specificity == "unknown"


def test_parse_verdict_handles_nested_braces_in_evidence():
    v = R.parse_verdict(
        '{"gap": true, "deficiencies": [{"what": "missing", '
        '"evidence": "code says {a: 1}", "specificity": "vague"}]}'
    )
    assert v.gap is True
    assert "{a: 1}" in v.deficiencies[0].evidence


# --- context builder (whole-conversation, robust to message-less turns) ------

def _msg(role, content, tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def test_multi_turn_context_collects_requests_and_actions():
    transcript = [
        _msg("system", "sys"),
        _msg("user", "find the file"),
        _msg("assistant", "Found it."),
        _msg("user", "now delete it"),
        _msg("assistant", ""),  # empty prose ignored
    ]
    decoded = [[["find(name='x')"]], [["rm(file='x')"]]]
    task, out = R.multi_turn_verify_context(transcript, decoded)
    assert "1. find the file" in task and "2. now delete it" in task
    assert "find(name='x')" in out and "rm(file='x')" in out
    assert "Found it." in out


def test_multi_turn_context_robust_to_message_less_reveal_turn():
    """A reveal turn has no user message; the builder must not crash and must
    still surface the assistant's actions for that turn."""
    transcript = [
        _msg("system", "sys"),
        _msg("user", "average score?"),
        _msg("assistant", "It's 91.67"),
        # (no user message for the reveal turn)
    ]
    decoded = [[["sum_values(numbers=[1,2])"]], [["mean(numbers=[1,2])"]]]
    task, out = R.multi_turn_verify_context(transcript, decoded)
    assert "1. average score?" in task
    # both turns' actions are surfaced even though there was only one user message
    assert "sum_values" in out and "mean" in out


def test_multi_turn_context_no_actions():
    transcript = [_msg("system", "s"), _msg("user", "do x")]
    task, out = R.multi_turn_verify_context(transcript, [[[]]])
    assert "(none)" in out  # the give-up shape: no actions


def test_multi_turn_context_skips_repair_nudge():
    """A Phase 2 repair nudge is injected as a `_luxe_repair`-marked user message; it
    must be invisible to a later verify (else it becomes a phantom 'ask')."""
    transcript = [
        _msg("system", "s"),
        _msg("user", "do x"),
        _msg("assistant", "can't"),
        {"role": "user", "content": "complete it now", "_luxe_repair": True},
    ]
    task, out = R.multi_turn_verify_context(transcript, [[[]]])
    assert "do x" in task
    assert "complete it now" not in task  # the injected nudge is not a user ask


# --- repair_nudge (Phase 2) -------------------------------------------------

def test_repair_nudge_carries_deficiencies():
    v = R.Verdict(gap=True, deficiencies=(
        R.Deficiency("book the flight", "no book_flight call", "concrete_local"),
        R.Deficiency("", "blank what is dropped", "vague"),
    ))
    nudge = R.repair_nudge(v)
    assert "book the flight" in nudge          # the verifier's cited unmet ask
    assert "blank what" not in nudge           # an empty `what` is not listed
    assert "did not fully carry out" in nudge  # the generic corrective frame


def test_repair_nudge_generic_when_no_deficiencies():
    nudge = R.repair_nudge(R.Verdict(gap=False))
    assert "Still not done" not in nudge
    assert nudge.strip()  # still a usable corrective message


def test_repair_nudge_has_no_benchmark_semantics():
    """Anti-overfit: the corrective nudge must not encode evaluator/benchmark phrasing."""
    nudge = R.repair_nudge(R.Verdict(gap=True, deficiencies=(R.Deficiency("x", "y", "vague"),)))
    low = nudge.lower()
    for banned in ("tool call", "state-checker", "checker", "warranted", "bfcl", "benchmark"):
        assert banned not in low


# --- env gate ---------------------------------------------------------------

def test_reflect_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LUXE_REFLECT", raising=False)
    assert R.reflect_enabled() is False


def test_reflect_enabled_when_flag_set(monkeypatch):
    monkeypatch.setenv("LUXE_REFLECT", "1")
    assert R.reflect_enabled() is True


# --- assembler dispatch -----------------------------------------------------

def test_verify_rejects_unknown_driver():
    import pytest
    with pytest.raises(ValueError):
        R.verify(backend=None, driver="nope", task="t", output="o")
