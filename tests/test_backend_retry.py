"""Tests for src/luxe/backend.py — body-aware retry classification."""

from __future__ import annotations

import logging

import httpx
import pytest

from luxe import backend as backend_mod
from luxe.backend import Backend, BackendError, RetryDecision, classify_failure


# --- classify_failure -------------------------------------------------------

def test_4xx_never_retried():
    d = classify_failure(status_code=400, body="bad request", attempt=0)
    assert not d.retry
    assert "4xx" in d.reason


def test_5xx_loading_body_retries():
    d = classify_failure(status_code=503, body='{"error": "model is loading"}', attempt=0)
    assert d.retry
    assert "loading" in d.reason


def test_5xx_swapping_body_retries():
    d = classify_failure(status_code=503, body="server is swapping models", attempt=0)
    assert d.retry


def test_5xx_warming_body_retries():
    d = classify_failure(status_code=503, body="warming up", attempt=0)
    assert d.retry


def test_5xx_unavailable_body_fails_fast():
    d = classify_failure(status_code=503, body='{"error": "service unavailable"}', attempt=0)
    assert not d.retry
    assert "terminal" in d.reason


def test_5xx_oom_body_fails_fast():
    d = classify_failure(status_code=503, body="out of memory", attempt=0)
    assert not d.retry


def test_5xx_crashed_body_fails_fast():
    d = classify_failure(status_code=503, body="server crashed", attempt=0)
    assert not d.retry


def test_5xx_empty_body_in_warmup_window_retries():
    d = classify_failure(status_code=503, body="", elapsed_since_start_s=2.0, attempt=0)
    assert d.retry
    assert "warmup" in d.reason


def test_5xx_empty_body_after_warmup_fails_fast():
    d = classify_failure(status_code=503, body="", elapsed_since_start_s=10.0, attempt=0)
    assert not d.retry
    assert "post-warmup" in d.reason


def test_connection_error_retries():
    err = httpx.ConnectError("refused")
    d = classify_failure(exc=err, attempt=0)
    assert d.retry
    assert "ConnectError" in d.reason


def test_read_timeout_retries():
    err = httpx.ReadTimeout("slow")
    d = classify_failure(exc=err, attempt=0)
    assert d.retry


def test_last_attempt_never_retries():
    # Even a transient marker fails on the last attempt
    d = classify_failure(status_code=503, body="loading", attempt=2, max_attempts=3)
    assert not d.retry
    assert "exhausted" in d.reason


def test_backoff_grows():
    d0 = classify_failure(status_code=503, body="loading", attempt=0)
    d1 = classify_failure(status_code=503, body="loading", attempt=1)
    assert d1.delay_s > d0.delay_s


# --- Backend.chat retry behaviour ------------------------------------------

class _MockTransport(httpx.MockTransport):
    """Sequence of HTTP responses; advances by one per request."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            r = self._responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        super().__init__(handler)


def _backend(transport, **kw):
    backend = Backend(model="test", **kw)
    backend._client = httpx.Client(base_url=backend.base_url, transport=transport)
    return backend


def _ok_response(text: str = "hello") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{
                "message": {"content": text, "role": "assistant"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def _err_response(status: int, body: str = "") -> httpx.Response:
    return httpx.Response(status, text=body)


def test_chat_retries_loading_then_succeeds(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    transport = _MockTransport([
        _err_response(503, '{"error": "model is loading"}'),
        _ok_response("worked"),
    ])
    backend = _backend(transport, max_attempts=3)
    resp = backend.chat([{"role": "user", "content": "hi"}])
    assert resp.text == "worked"
    assert resp.retries == 1
    assert transport.calls == 2


def test_chat_fails_fast_on_4xx(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    transport = _MockTransport([_err_response(400, "bad request")])
    backend = _backend(transport, max_attempts=3)
    with pytest.raises(BackendError):
        backend.chat([{"role": "user", "content": "hi"}])
    assert transport.calls == 1  # no retry


def test_chat_fails_fast_on_terminal_5xx(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    transport = _MockTransport([_err_response(503, "out of memory")])
    backend = _backend(transport, max_attempts=3)
    with pytest.raises(BackendError):
        backend.chat([{"role": "user", "content": "hi"}])
    assert transport.calls == 1


def test_chat_exhausts_retries(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    transport = _MockTransport([
        _err_response(503, "loading"),
        _err_response(503, "loading"),
        _err_response(503, "loading"),
    ])
    backend = _backend(transport, max_attempts=3)
    with pytest.raises(BackendError):
        backend.chat([{"role": "user", "content": "hi"}])
    assert transport.calls == 3


def test_chat_invokes_on_retry_callback(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    transport = _MockTransport([
        _err_response(503, "loading"),
        _ok_response(),
    ])
    backend = _backend(transport, max_attempts=3)
    seen: list[RetryDecision] = []
    backend.chat([{"role": "user", "content": "hi"}], on_retry=lambda d, a: seen.append(d))
    assert len(seen) == 1
    assert seen[0].retry


# --- 429 is the one 4xx worth retrying (2026-08-17, openrouter) -------------

def test_429_is_transient():
    """A rate limit is normal backpressure from a metered provider, not a bug
    in the request — the same call succeeds moments later, which is exactly
    what the existing backoff is for."""
    d = classify_failure(status_code=429, body="rate limit exceeded", attempt=0)
    assert d.retry
    assert "429" in d.reason
    assert d.delay_s > 0


def test_402_out_of_credits_stays_terminal():
    """The counter-case. Retrying a request the account cannot pay for burns
    the retry budget to arrive at the same answer three times."""
    d = classify_failure(status_code=402, body="insufficient credits", attempt=0)
    assert not d.retry
    assert "4xx" in d.reason


def test_429_still_stops_on_the_last_attempt():
    d = classify_failure(status_code=429, body="", attempt=2, max_attempts=3)
    assert not d.retry


def test_chat_retries_a_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    transport = _MockTransport([
        _err_response(429, '{"error": "rate limit exceeded"}'),
        _ok_response("worked"),
    ])
    backend = _backend(transport, max_attempts=3)
    resp = backend.chat([{"role": "user", "content": "hi"}])
    assert resp.text == "worked"
    assert resp.retries == 1
    assert transport.calls == 2


def test_chat_does_not_retry_a_402(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    transport = _MockTransport([_err_response(402, "insufficient credits")])
    backend = _backend(transport, max_attempts=3)
    with pytest.raises(BackendError):
        backend.chat([{"role": "user", "content": "hi"}])
    assert transport.calls == 1


# --- the failure string names the engine that actually failed ---------------
#
# Every message below was hardcoded "oMLX" until 2026-08-24. Session
# 168f1825a1fd died on the OpenRouter backend with "oMLX stream failed:
# RemoteProtocolError … (exhausted-attempts)", naming a local serving stack
# that was never in the request path — the first thing a post-outage reader
# would chase. `acceptance/chat_bigread_2026_08_24/EVIDENCE.md` finding 1.
#
# The default is the literal string every message has always carried, so the
# benchmark/maintain path (`maintain.py` constructs with no kwargs) is
# byte-identical by construction. These tests pin BOTH halves of that.

def test_default_backend_still_says_omlx_on_a_terminal_status(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    backend = _backend(_MockTransport([_err_response(400, "bad request")]))
    with pytest.raises(BackendError) as e:
        backend.chat([{"role": "user", "content": "hi"}])
    assert str(e.value).startswith("oMLX returned 400:")


def test_default_backend_still_says_omlx_on_a_transport_failure(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    backend = _backend(_MockTransport([httpx.ConnectError("nope")]),
                       max_attempts=1)
    with pytest.raises(BackendError) as e:
        backend.chat([{"role": "user", "content": "hi"}])
    assert str(e.value).startswith("oMLX call failed: ConnectError")


def test_the_exhausted_tail_carries_the_label_on_both_paths(monkeypatch):
    """The `retries exhausted after N attempts` tail — reached when the loop
    ends without a raise. `max_attempts=0` is the only way to get there
    deterministically (`classify_failure` turns the LAST attempt terminal, so
    the in-loop raise normally wins)."""
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    for stream, phrase in ((False, "retries exhausted"),
                           (True, "stream retries exhausted")):
        plain = _backend(_MockTransport([]), max_attempts=0)
        with pytest.raises(BackendError) as e:
            plain.chat([{"role": "user", "content": "hi"}], stream=stream,
                       on_token=(lambda t: None) if stream else None)
        assert str(e.value) == f"oMLX {phrase} after 0 attempts (unknown)"

        labelled = _backend(_MockTransport([]), max_attempts=0,
                            engine_label="OpenRouter")
        with pytest.raises(BackendError) as e:
            labelled.chat([{"role": "user", "content": "hi"}], stream=stream,
                          on_token=(lambda t: None) if stream else None)
        assert str(e.value) == f"OpenRouter {phrase} after 0 attempts (unknown)"


def test_a_labelled_backend_names_that_engine_and_never_omlx(monkeypatch):
    """The bug this closes: the message must not assert a stack that was not
    in the request path."""
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    for responses, attempts in (
        ([_err_response(400, "bad request")], 3),
        ([httpx.ConnectError("nope")], 1),
        ([_err_response(503, "loading")] * 3, 3),
    ):
        backend = _backend(_MockTransport(list(responses)),
                           max_attempts=attempts,
                           engine_label="OpenRouter")
        with pytest.raises(BackendError) as e:
            backend.chat([{"role": "user", "content": "hi"}])
        assert "OpenRouter" in str(e.value)
        assert "oMLX" not in str(e.value)


def test_the_streaming_path_carries_the_label_too(monkeypatch):
    """The stream path is the one chat actually uses (`on_token` set), and it
    is where the 2026-08-24 message came from."""
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    backend = _backend(_MockTransport([httpx.RemoteProtocolError("peer closed")]),
                       max_attempts=1, engine_label="OpenRouter")
    with pytest.raises(BackendError) as e:
        backend.chat([{"role": "user", "content": "hi"}], stream=True,
                     on_token=lambda t: None)
    assert str(e.value).startswith("OpenRouter stream failed: RemoteProtocolError")
    assert "oMLX" not in str(e.value)


def test_the_streaming_path_default_is_unchanged(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
    backend = _backend(_MockTransport([httpx.RemoteProtocolError("peer closed")]),
                       max_attempts=1)
    with pytest.raises(BackendError) as e:
        backend.chat([{"role": "user", "content": "hi"}], stream=True,
                     on_token=lambda t: None)
    assert str(e.value).startswith("oMLX stream failed: RemoteProtocolError")


def test_an_empty_label_falls_back_to_the_default():
    """Never render "` returned 400`" with a blank where the stack goes."""
    assert Backend(engine_label="").engine_label == "oMLX"


# --- health() takes a per-call bound (2026-08-24) ---------------------------

def test_health_without_a_bound_is_unchanged():
    """Every pre-2026-08-24 caller passes nothing and keeps the client's own
    timeout — the probe request must be identical to what it was."""
    seen: list = []

    def handler(request):
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"data": []})

    backend = Backend(model="test")
    backend._client = httpx.Client(base_url=backend.base_url,
                                   timeout=httpx.Timeout(600.0),
                                   transport=httpx.MockTransport(handler))
    assert backend.health() is True
    assert seen[0]["read"] == 600.0


def test_health_with_a_bound_uses_it():
    """`unreachable_hint` runs this on a failed turn: a hung endpoint that
    accepts the socket and never answers must not hold it for 600s."""
    seen: list = []

    def handler(request):
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(200, json={"data": []})

    backend = Backend(model="test")
    backend._client = httpx.Client(base_url=backend.base_url,
                                   timeout=httpx.Timeout(600.0),
                                   transport=httpx.MockTransport(handler))
    assert backend.health(timeout_s=4.0) is True
    assert seen[0]["read"] == 4.0


def test_health_still_never_raises():
    backend = Backend(model="test")
    backend._client = httpx.Client(
        base_url=backend.base_url,
        transport=httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    assert backend.health(timeout_s=1.0) is False


# --- payload-suspect annotation (opt-in, observe-only, 2026-08-24) ----------
#
# `acceptance/chat_bigread_2026_08_24/EVIDENCE.md` case 1: a request whose
# prompt jumped ~40x in one step (two files read in one tool step) died with
# RemoteProtocolError, was classified `transient-*`, and the IDENTICAL bytes
# were re-dispatched twice more — 81.5s of wall, three billed attempts,
# terminal by construction.
#
# `LUXE_PAYLOAD_SUSPECT_RETRY=1` makes that shape SAY so. It does not act on
# it: the retry ladder is unchanged in both arms. See `luxe.sdd` for why
# (n=1 session, one provider, and >2x growth over the last accepted request is
# the ordinary shape of an early agent turn).

_BIG = 200_000          # chars — comfortably past the 64 KB floor
_SMALL = 2_000


@pytest.fixture
def suspect_on(monkeypatch):
    monkeypatch.setenv("LUXE_PAYLOAD_SUSPECT_RETRY", "1")


@pytest.fixture
def suspect_unset(monkeypatch):
    monkeypatch.delenv("LUXE_PAYLOAD_SUSPECT_RETRY", raising=False)


#: (kwargs, expected retry, reason substring). Every decision the function has
#: ever made, replayed below in BOTH arms with growth-shaped sizes attached.
_DECISION_TABLE = [
    (dict(status_code=400, body="bad request"), False, "4xx-400"),
    (dict(status_code=402, body="insufficient credits"), False, "4xx-402"),
    (dict(status_code=429, body="rate limit"), True, "429-rate-limited"),
    (dict(status_code=503, body="model is loading"), True, "5xx-transient-loading"),
    (dict(status_code=503, body="out of memory"), False, "5xx-terminal-out of memory"),
    (dict(status_code=503, body="", elapsed_since_start_s=2.0), True, "5xx-empty-warmup"),
    (dict(status_code=503, body="", elapsed_since_start_s=10.0), False,
     "5xx-empty-post-warmup"),
    (dict(status_code=302), False, "unexpected-302"),
    (dict(), False, "no-status-no-exception"),
    (dict(exc=ValueError("nope")), False, "unknown-error-ValueError"),
    (dict(exc=httpx.ConnectError("refused")), True, "transient-ConnectError"),
    (dict(exc=httpx.ReadTimeout("slow")), True, "transient-ReadTimeout"),
    (dict(exc=httpx.RemoteProtocolError("peer closed")), True,
     "transient-RemoteProtocolError"),
]


class TestEveryExistingDecisionIsUnchanged:
    """The hard constraint. This is the fleet's outage path — a retry
    regression here is worse than the bug being fixed."""

    @pytest.mark.parametrize("kw,retry,reason", _DECISION_TABLE)
    def test_unset_with_growth_sized_arguments(self, suspect_unset, kw, retry, reason):
        """Sizes present, lever off: byte-identical to passing nothing."""
        got = classify_failure(attempt=0, request_chars=_BIG,
                               last_accepted_chars=_SMALL, **kw)
        base = classify_failure(attempt=0, **kw)
        assert (got.retry, got.reason, got.delay_s) == (retry, reason, base.delay_s)
        assert got == base

    @pytest.mark.parametrize("kw,retry,reason", _DECISION_TABLE)
    def test_lever_on_without_sizes_is_also_unchanged(self, suspect_on, kw, retry, reason):
        """A caller that measures nothing can never be annotated."""
        d = classify_failure(attempt=0, **kw)
        assert (d.retry, d.reason) == (retry, reason)

    def test_exhausted_attempts_still_wins_over_everything(self, suspect_on):
        """The last-attempt short-circuit runs BEFORE any classification, and
        must keep doing so — a payload-suspect request on its final attempt is
        `exhausted-attempts`, exactly as today."""
        d = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=2,
                             max_attempts=3, request_chars=_BIG,
                             last_accepted_chars=_SMALL)
        assert not d.retry
        assert d.reason == "exhausted-attempts"

    def test_the_backoff_ladder_is_untouched(self, suspect_on):
        d0 = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=0,
                              request_chars=_BIG, last_accepted_chars=_SMALL)
        d1 = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=1,
                              request_chars=_BIG, last_accepted_chars=_SMALL)
        assert (d0.delay_s, d1.delay_s) == (1.0, 4.0)
        assert d0.retry and d1.retry


class TestTheAnnotationFires:
    def test_growth_over_the_floor_is_annotated(self, suspect_on):
        d = classify_failure(exc=httpx.RemoteProtocolError("peer closed"),
                             attempt=0, request_chars=_BIG,
                             last_accepted_chars=_SMALL)
        assert d.reason == "transient-RemoteProtocolError-payload-suspect"
        # OBSERVE-ONLY: the ladder is what it always was.
        assert d.retry is True
        assert d.delay_s == 1.0

    def test_the_same_case_is_plain_with_the_lever_off(self, suspect_unset):
        d = classify_failure(exc=httpx.RemoteProtocolError("peer closed"),
                             attempt=0, request_chars=_BIG,
                             last_accepted_chars=_SMALL)
        assert d.reason == "transient-RemoteProtocolError"

    @pytest.mark.parametrize("exc_type", [
        httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
        httpx.NetworkError, httpx.RemoteProtocolError,
    ])
    def test_every_transport_class_can_carry_it(self, suspect_on, exc_type):
        d = classify_failure(exc=exc_type("x"), attempt=0, request_chars=_BIG,
                             last_accepted_chars=_SMALL)
        assert d.reason.endswith("-payload-suspect")

    def test_the_prefix_is_preserved_so_existing_readers_still_match(self, suspect_on):
        """`transient-` and the exception name stay where they were; the
        annotation is a suffix. `scripts/bigread_drill.py` reads the reason out
        of the RetryDecision repr and must keep parsing."""
        d = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=0,
                             request_chars=_BIG, last_accepted_chars=_SMALL)
        assert d.reason.startswith("transient-RemoteProtocolError")
        assert repr(d).startswith("RetryDecision(retry=True, reason=")
        assert "delay_s=" in repr(d)


class TestTheAnnotationHoldsItsFire:
    """Each guard on its own. A false positive here is a lie in the corpus the
    promotion decision will be made from."""

    def test_no_baseline_is_not_suspicion(self, suspect_on):
        """Nothing accepted yet — there is no growth to claim, however big."""
        for baseline in (None, 0):
            d = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=0,
                                 request_chars=_BIG, last_accepted_chars=baseline)
            assert d.reason == "transient-RemoteProtocolError"

    def test_below_the_absolute_floor_is_not_suspicion(self, suspect_on):
        """A 40x jump from 100 chars to 4,000 endangers no window."""
        d = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=0,
                             request_chars=4_000, last_accepted_chars=100)
        assert d.reason == "transient-RemoteProtocolError"

    def test_a_big_but_flat_request_is_not_suspicion(self, suspect_on):
        d = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=0,
                             request_chars=_BIG, last_accepted_chars=_BIG)
        assert d.reason == "transient-RemoteProtocolError"

    @pytest.mark.parametrize("ratio,annotated", [
        (1.9, False), (1.99, False), (2.0, True), (2.5, True), (40.0, True),
    ])
    def test_the_growth_threshold(self, suspect_on, ratio, annotated):
        baseline = 100_000
        d = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=0,
                             request_chars=int(baseline * ratio),
                             last_accepted_chars=baseline)
        assert d.reason.endswith("-payload-suspect") is annotated

    def test_unmeasured_request_chars_is_not_suspicion(self, suspect_on):
        d = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=0,
                             request_chars=0, last_accepted_chars=_SMALL)
        assert d.reason == "transient-RemoteProtocolError"


class TestTheOptInGrammar:
    """Opt-IN, `agents/flags.py`'s spelling: only the exact string "1"."""

    @pytest.mark.parametrize("value", ["", "0", "true", "01", " 1", "on", "yes", "1 "])
    def test_near_misses_are_all_off(self, monkeypatch, value):
        monkeypatch.setenv("LUXE_PAYLOAD_SUSPECT_RETRY", value)
        assert backend_mod.payload_suspect_enabled() is False
        d = classify_failure(exc=httpx.RemoteProtocolError("x"), attempt=0,
                             request_chars=_BIG, last_accepted_chars=_SMALL)
        assert d.reason == "transient-RemoteProtocolError"

    def test_unset_is_off(self, suspect_unset):
        assert backend_mod.payload_suspect_enabled() is False

    def test_the_exact_string_one_is_on(self, suspect_on):
        assert backend_mod.payload_suspect_enabled() is True

    def test_it_is_read_at_call_time_not_import_time(self, monkeypatch):
        monkeypatch.delenv("LUXE_PAYLOAD_SUSPECT_RETRY", raising=False)
        assert backend_mod.payload_suspect_enabled() is False
        monkeypatch.setenv("LUXE_PAYLOAD_SUSPECT_RETRY", "1")
        assert backend_mod.payload_suspect_enabled() is True


class TestPromptChars:
    def test_it_measures_messages_only(self):
        small = backend_mod.prompt_chars(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        big = backend_mod.prompt_chars(
            {"model": "m", "messages": [{"role": "user", "content": "x" * 5_000}]})
        assert big - small >= 4_990

    def test_tools_do_not_count(self):
        """A per-role constant that cannot grow mid-turn would only dilute the
        jump this is looking for."""
        msgs = [{"role": "user", "content": "hi"}]
        assert backend_mod.prompt_chars({"messages": msgs}) == \
            backend_mod.prompt_chars({"messages": msgs, "tools": [{"x": "y" * 900}]})

    def test_an_unmeasurable_body_is_zero_not_an_exception(self):
        assert backend_mod.prompt_chars({"messages": [{"c": object()}]}) == 0
        assert backend_mod.prompt_chars({}) == 0
        assert backend_mod.prompt_chars({"messages": []}) == 0


class TestTheBackendTracksWhatWasAccepted:
    """Where the size state lives: on the Backend instance, updated only on a
    request the server actually answered."""

    def test_the_baseline_is_not_measured_at_all_when_off(
        self, monkeypatch, suspect_unset
    ):
        monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
        b = _backend(_MockTransport([_ok_response()]))
        b.chat([{"role": "user", "content": "hi"}])
        assert b._last_accepted_prompt_chars is None

    def test_a_success_sets_the_baseline(self, monkeypatch, suspect_on):
        monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
        b = _backend(_MockTransport([_ok_response()]))
        b.chat([{"role": "user", "content": "hi"}])
        assert b._last_accepted_prompt_chars > 0

    def test_a_failure_does_not_set_the_baseline(self, monkeypatch, suspect_on):
        monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
        b = _backend(_MockTransport([_err_response(400, "bad")]))
        with pytest.raises(BackendError):
            b.chat([{"role": "user", "content": "hi"}])
        assert b._last_accepted_prompt_chars is None

    def test_the_incident_shape_end_to_end(self, monkeypatch, suspect_on, caplog):
        """Case 1, replayed: one small accepted turn, then a step that reads
        two files and dies at the transport. The failure now SAYS the prompt
        grew — and still burns the full ladder, because this lever observes."""
        monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
        transport = _MockTransport([
            _ok_response(),
            httpx.RemoteProtocolError("peer closed"),
            httpx.RemoteProtocolError("peer closed"),
            httpx.RemoteProtocolError("peer closed"),
        ])
        b = _backend(transport, max_attempts=3)
        b.chat([{"role": "user", "content": "hi"}])
        with caplog.at_level(logging.WARNING, logger="luxe.backend"):
            with pytest.raises(BackendError) as e:
                b.chat([{"role": "user", "content": "x" * 200_000}])
        assert transport.calls == 4          # 1 success + the unchanged ladder
        assert "exhausted-attempts" in str(e.value)
        log = caplog.text
        assert "payload-suspect" in log
        assert "prompt grew" in log
        # The annotated reason rides the existing `decision=` line, which is
        # what reaches a session's debug.log.
        assert "reason='transient-RemoteProtocolError-payload-suspect'" in log

    def test_the_same_shape_on_the_streaming_path(self, monkeypatch, suspect_on, caplog):
        """The path chat actually uses — and the one the incident ran on."""
        monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
        transport = _MockTransport([
            httpx.Response(200, text=(
                'data: {"choices":[{"delta":{"content":"ok"},'
                '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')),
            httpx.RemoteProtocolError("peer closed"),
            httpx.RemoteProtocolError("peer closed"),
        ])
        b = _backend(transport, max_attempts=2)
        b.chat([{"role": "user", "content": "hi"}], stream=True,
               on_token=lambda t: None)
        assert b._last_accepted_prompt_chars > 0
        with caplog.at_level(logging.WARNING, logger="luxe.backend"):
            with pytest.raises(BackendError):
                b.chat([{"role": "user", "content": "x" * 200_000}], stream=True,
                       on_token=lambda t: None)
        assert "payload-suspect" in caplog.text

    def test_the_annotation_names_the_two_sizes(self, monkeypatch, suspect_on, caplog):
        """The observation has to be usable evidence: the log line carries the
        ratio and both measurements, not just a label."""
        monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
        b = _backend(_MockTransport([
            _ok_response(),
            httpx.RemoteProtocolError("peer closed"),
            httpx.RemoteProtocolError("peer closed"),
        ]), max_attempts=2)
        b.chat([{"role": "user", "content": "hi"}])
        with caplog.at_level(logging.WARNING, logger="luxe.backend"):
            with pytest.raises(BackendError):
                b.chat([{"role": "user", "content": "x" * 200_000}])
        line = next(ln for ln in caplog.text.splitlines() if "payload-suspect" in ln
                    and "prompt grew" in ln)
        assert "last accepted" in line and "this request 200" in line

    def test_with_the_lever_off_the_same_run_says_nothing_new(
        self, monkeypatch, suspect_unset, caplog
    ):
        """The OFF arm of the incident replay: same dispatch count, same
        decisions, and not one new word in the log."""
        monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)
        transport = _MockTransport([
            _ok_response(),
            httpx.RemoteProtocolError("peer closed"),
            httpx.RemoteProtocolError("peer closed"),
            httpx.RemoteProtocolError("peer closed"),
        ])
        b = _backend(transport, max_attempts=3)
        b.chat([{"role": "user", "content": "hi"}])
        with caplog.at_level(logging.WARNING, logger="luxe.backend"):
            with pytest.raises(BackendError) as e:
                b.chat([{"role": "user", "content": "x" * 200_000}])
        assert transport.calls == 4
        assert "payload-suspect" not in caplog.text
        assert "reason='transient-RemoteProtocolError'" in caplog.text
        assert str(e.value).startswith("oMLX call failed: RemoteProtocolError")
