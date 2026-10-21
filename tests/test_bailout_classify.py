"""Tests for maintain_suite run.py `_classify_bailout` labels.

Until 2026-10-21 a max-steps death was labelled `context_overflow` (the
classifier string-matched "max steps"), while a real window overflow — the
server's 400 — fell through to the generic `aborted`. The label sent one
round of analysis after a context problem that did not exist (lessons.md).
"""

from __future__ import annotations

from benchmarks.maintain_suite.run import FixtureState, _classify_bailout

# Verbatim from ~/.luxe/runs events (oMLX 0.7.0 on m1).
_OMLX_WINDOW_400 = (
    'Backend error: oMLX returned 400: {"error":{"message":"Prompt too long: '
    '37389 tokens exceeds max context window of 32768 tokens","type":'
    '"invalid_request_error","param":null,"code":null}} (4xx-400)'
)
_OMLX_MEMORY_507 = (
    'Backend error: oMLX returned 507: {"error":{"message":"Cannot free enough '
    'memory. Need 28.42GB, current usage 30.19GB, all loaded models are '
    'pinned.","type":"server_error","param":null,"code":null}} '
    '(5xx-empty-post-warmup)'
)
_OMLX_TEMPLATE_400 = (
    'Backend error: oMLX returned 400: {"error":{"message":"Chat template '
    'error: Cannot use chat template functions because '
    'tokenizer.chat_template is not set"}} (4xx-400)'
)


def _classify(abort_reason: str, **sm) -> tuple[str, str]:
    single_mode = {"aborted": True, "abort_reason": abort_reason,
                   "tool_calls_total": 12, **sm}
    return _classify_bailout(FixtureState(fixture_id="fx"),
                             {"single_mode": single_mode})


def test_max_steps_is_labelled_max_steps():
    kind, reason = _classify("Max steps reached (30)")
    assert kind == "max_steps"
    assert reason == "max steps reached (30)"


def test_server_window_rejection_is_context_overflow():
    kind, reason = _classify(_OMLX_WINDOW_400)
    assert kind == "context_overflow"
    assert "prompt too long" in reason


def test_other_backend_errors_stay_generic_aborted():
    assert _classify(_OMLX_MEMORY_507)[0] == "aborted"
    assert _classify(_OMLX_TEMPLATE_400)[0] == "aborted"


def test_window_words_outside_a_backend_error_are_not_overflow():
    # A guard's own abort text can mention the window without the server
    # having rejected anything.
    kind, _ = _classify("early_bail: context window pressure 0.91")
    assert kind == "aborted"


def test_stuck_loop_still_wins_over_max_steps_wording():
    kind, _ = _classify("stuck loop detected before max steps")
    assert kind == "stuck_no_output"
