"""Tests for src/luxe/backend.py — body-aware retry classification."""

from __future__ import annotations

import httpx
import pytest

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
