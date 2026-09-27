"""Backend robustness fixes (2026-09-26 review).

Each section pins one failure a live run or the review found:

  1. A 200 whose body is an ERROR object (oMLX: "JSON keepalive request
     failed (500): Model output contains an unrecoverable tool call…",
     delivered after the keepalive-200) crashed with `KeyError: 'choices'`,
     so the run's abort_reason read "Backend error: 'choices'" and said
     nothing about what happened.
  2. The stream path retried a request whose tokens had ALREADY reached
     `on_token` — the live tail replayed from the start and a metered
     provider billed the generation twice. An OpenRouter mid-stream
     `{"error": …}` chunk was ignored and read as a normal completion.
  3. (Kept, deliberately) the empty-5xx warmup window is per REQUEST, so a
     fast empty 5xx retries at any Backend age — pinned so it is not "fixed"
     to a per-Backend clock by accident.
  4. A progress stall was retryable, so a keepalive-sending wedged server held
     one request for up to max_attempts × stall_timeout_s.
  5. `Backend` never closed its httpx client.

MockTransport throughout except the stall case, which is wall-clock and uses a
local stub server (never the real oMLX on :8000).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from luxe.backend import (
    Backend,
    BackendError,
    ProgressStall,
    classify_failure,
)


class _Seq(httpx.MockTransport):
    """Sequence of responses (or exceptions); advances one per request."""

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


def _backend(transport, **kw) -> Backend:
    b = Backend(model="test", api_key="k", **kw)
    b._client.close()
    b._client = httpx.Client(base_url=b.base_url, transport=transport)
    return b


def _ok(text: str = "hello") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": text, "role": "assistant"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("luxe.backend.time.sleep", lambda s: None)


MSG = [{"role": "user", "content": "hi"}]

# The live 2026-09-26 shape: keepalive padding, then an error object.
_OMLX_ERR = (
    " " * 40
    + json.dumps({"error": {
        "message": "Model output contains an unrecoverable tool call "
                   "(incomplete_tool_call)",
        "type": "server_error", "code": "incomplete_tool_call"}})
)


# --- 1. error objects and malformed bodies on a 200 ------------------------


class TestErrorBodyOn200:
    def test_error_object_raises_backend_error_naming_the_server_message(self):
        t = _Seq([httpx.Response(200, text=_OMLX_ERR)])
        with pytest.raises(BackendError) as e:
            _backend(t, max_attempts=3).chat(MSG)
        msg = str(e.value)
        assert "unrecoverable tool call" in msg
        assert "incomplete_tool_call" in msg          # the body slice
        assert "'choices'" not in msg

    def test_error_object_is_not_retried(self):
        """Terminal by design: the same prompt reproduces the same output,
        and a retry would spend a full generation to get there."""
        t = _Seq([httpx.Response(200, text=_OMLX_ERR), _ok()])
        with pytest.raises(BackendError):
            _backend(t, max_attempts=3).chat(MSG)
        assert t.calls == 1

    def test_error_object_logs_a_decision_line(self, caplog):
        t = _Seq([httpx.Response(200, text=_OMLX_ERR)])
        with caplog.at_level(logging.WARNING, logger="luxe.backend"):
            with pytest.raises(BackendError):
                _backend(t).chat(MSG)
        lines = [r.getMessage() for r in caplog.records]
        assert any(re.match(r"^backend test status=200 body=.* decision="
                            r"RetryDecision\(retry=False, reason='200-error-body'",
                            ln) for ln in lines), lines

    def test_a_string_error_is_reported_too(self):
        t = _Seq([httpx.Response(200, json={"error": "boom from server"})])
        with pytest.raises(BackendError, match="boom from server"):
            _backend(t).chat(MSG)

    @pytest.mark.parametrize("body", [
        "not json at all",
        "{}",
        '{"choices": []}',
        '{"choices": [null]}',
        '{"choices": [{"message": null}]}',
        '[1, 2]',
    ])
    def test_malformed_bodies_raise_backend_error_with_a_slice(self, body):
        t = _Seq([httpx.Response(200, text=body)])
        with pytest.raises(BackendError) as e:
            _backend(t, max_attempts=3).chat(MSG)
        assert "200-malformed-body" in str(e.value)
        assert t.calls == 1

    def test_null_usage_is_tolerated_not_an_error(self):
        t = _Seq([httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": None,
        })])
        resp = _backend(t).chat(MSG)
        assert resp.text == "ok"
        assert resp.timing.prompt_tokens == 0

    def test_a_tool_call_without_a_name_is_malformed_not_a_keyerror(self):
        t = _Seq([httpx.Response(200, json={
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"arguments": "{}"}}]}}],
        })])
        with pytest.raises(BackendError, match="200-malformed-body"):
            _backend(t).chat(MSG)


# --- 2. streaming: no replay after output; error chunks are errors --------


def _sse(*chunks) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)


class _BreakingStream(httpx.SyncByteStream):
    """Yields `head`, then fails the way a dropped connection does."""

    def __init__(self, head: bytes):
        self.head = head

    def __iter__(self):
        yield self.head
        raise httpx.RemoteProtocolError("peer closed connection mid-body")


def _content(t: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": t}}]}


class TestStreamDoesNotReplay:
    def test_failure_after_tokens_is_not_retried(self):
        t = _Seq([
            httpx.Response(200, stream=_BreakingStream(
                _sse(_content("Hel"), _content("lo")))),
            httpx.Response(200, content=_sse(
                _content("Hello"),
                {"choices": [{"delta": {}, "finish_reason": "stop"}]})),
        ])
        seen: list[str] = []
        with pytest.raises(BackendError) as e:
            _backend(t, max_attempts=3).chat(
                MSG, stream=True, on_token=seen.append)
        assert t.calls == 1                  # not re-dispatched (not re-billed)
        assert seen == ["Hel", "lo"]         # the live tail was not replayed
        assert "RemoteProtocolError" in str(e.value)
        assert "after-output" in str(e.value)

    def test_failure_after_reasoning_only_is_not_retried(self):
        t = _Seq([
            httpx.Response(200, stream=_BreakingStream(_sse(
                {"choices": [{"delta": {"reasoning": "thinking…"}}]}))),
            _ok(),
        ])
        b = _backend(t, max_attempts=3)
        thoughts: list[str] = []
        b.on_reasoning = thoughts.append
        with pytest.raises(BackendError):
            b.chat(MSG, stream=True, on_token=lambda s: None)
        assert t.calls == 1
        assert thoughts == ["thinking…"]

    def test_failure_before_any_output_still_retries(self):
        """Unchanged: nothing reached the UI or the bill, so a retry is safe."""
        t = _Seq([
            httpx.ConnectError("refused"),
            httpx.Response(200, content=_sse(
                _content("fine"),
                {"choices": [{"delta": {}, "finish_reason": "stop"}]})),
        ])
        seen: list[str] = []
        resp = _backend(t, max_attempts=3).chat(
            MSG, stream=True, on_token=seen.append)
        assert resp.text == "fine" and resp.retries == 1
        assert seen == ["fine"]

    def test_the_after_output_decision_is_logged(self, caplog):
        t = _Seq([httpx.Response(200, stream=_BreakingStream(_sse(_content("x"))))])
        with caplog.at_level(logging.WARNING, logger="luxe.backend"):
            with pytest.raises(BackendError):
                _backend(t, max_attempts=3).chat(
                    MSG, stream=True, on_token=lambda s: None)
        assert any(
            "exception=RemoteProtocolError decision=RetryDecision(retry=False, "
            "reason='transient-RemoteProtocolError-after-output'" in r.getMessage()
            for r in caplog.records)


class TestStreamErrorChunk:
    # OpenRouter's documented mid-stream error shape.
    _ERR = {"id": "gen-1", "object": "chat.completion.chunk",
            "error": {"code": 502, "message": "Provider returned error"},
            "choices": [{"index": 0, "delta": {"content": ""},
                         "finish_reason": "error"}]}

    def test_an_error_chunk_raises_instead_of_completing(self):
        t = _Seq([httpx.Response(200, content=_sse(_content("par"), self._ERR))])
        with pytest.raises(BackendError, match="Provider returned error"):
            _backend(t, max_attempts=3).chat(
                MSG, stream=True, on_token=lambda s: None)
        assert t.calls == 1

    def test_an_error_chunk_before_any_token_is_also_an_error(self):
        t = _Seq([httpx.Response(200, content=_sse(self._ERR))])
        with pytest.raises(BackendError, match="stream-error-chunk"):
            _backend(t, max_attempts=3).chat(
                MSG, stream=True, on_token=lambda s: None)
        assert t.calls == 1


# --- 3. the warmup window is per request, at any Backend age --------------


class TestWarmupWindowIsPerRequest:
    def test_a_long_lived_backend_still_retries_a_fast_empty_5xx(self, monkeypatch):
        t = _Seq([httpx.Response(503, text=""), _ok("up")])
        b = _backend(t, max_attempts=3)
        real = time.monotonic
        # The Backend has existed "an hour"; the request itself is fresh.
        monkeypatch.setattr("luxe.backend.time.monotonic", lambda: real() + 3600)
        resp = b.chat(MSG)
        assert resp.text == "up" and t.calls == 2

    def test_the_same_holds_on_the_stream_path(self):
        t = _Seq([
            httpx.Response(503, text=""),
            httpx.Response(200, content=_sse(
                _content("up"),
                {"choices": [{"delta": {}, "finish_reason": "stop"}]})),
        ])
        resp = _backend(t, max_attempts=3).chat(
            MSG, stream=True, on_token=lambda s: None)
        assert resp.text == "up" and t.calls == 2


# --- 4. a progress stall is not retried ------------------------------------


def test_classify_a_progress_stall_is_terminal():
    d = classify_failure(exc=ProgressStall("stalled"), attempt=0, max_attempts=3)
    assert not d.retry
    assert d.reason == "progress-stall"


def test_a_plain_read_timeout_still_retries():
    d = classify_failure(exc=httpx.ReadTimeout("slow"), attempt=0, max_attempts=3)
    assert d.retry and d.reason == "transient-ReadTimeout"


def _serve_keepalive_forever():
    hits = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits["n"] += 1
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                while True:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv.shutdown, hits


def test_a_wedged_keepalive_server_is_dispatched_once_not_max_attempts():
    url, stop, hits = _serve_keepalive_forever()
    try:
        b = Backend(base_url=url, model="probe", api_key="k", max_attempts=3,
                    stall_timeout_s=0.4)
        t0 = time.monotonic()
        with pytest.raises(BackendError, match="progress-stall"):
            b.chat(MSG)
        elapsed = time.monotonic() - t0
    finally:
        stop()
    assert hits["n"] == 1
    assert elapsed < 3.0


# --- 5. the client is closable ---------------------------------------------


def test_backend_is_a_context_manager_that_closes_its_client():
    with Backend(model="m", api_key="k") as b:
        assert not b._client.is_closed
    assert b._client.is_closed


def test_close_is_idempotent():
    b = Backend(model="m", api_key="k")
    b.close()
    b.close()
    assert b._client.is_closed


# --- 6. one retry loop: both paths log the same parseable line -------------

_BIGREAD_RE = re.compile(
    r"^backend (\S+) exception=(\S+) decision=RetryDecision\(retry=(True|False),\s*"
    r"reason='([^']+)',\s*delay_s=([\d.]+)\)$")


@pytest.mark.parametrize("stream", [False, True])
def test_both_paths_log_the_same_exception_line(caplog, stream):
    t = _Seq([httpx.ConnectError("refused")])
    with caplog.at_level(logging.WARNING, logger="luxe.backend"):
        with pytest.raises(BackendError):
            _backend(t, max_attempts=1).chat(
                MSG, stream=stream,
                on_token=(lambda s: None) if stream else None)
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == ["backend test exception=ConnectError decision=RetryDecision("
                    "retry=False, reason='exhausted-attempts', delay_s=0.0)"]
    assert _BIGREAD_RE.match(msgs[0])
