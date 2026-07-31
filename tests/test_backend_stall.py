"""Progress-deadline tests for `Backend` (B6).

`httpx.Timeout` is a PER-READ deadline: it fires only after N seconds of
total silence. oMLX deliberately breaks that silence — it emits keepalive
bytes so long prefills don't trip the read timeout:

  - streaming:     SSE chunks with `"model": "keepalive"`, `delta.content ""`
  - non-streaming: a bare space byte under `Transfer-Encoding: chunked`

Those keepalives reset the read clock, so before this guard a request whose
generation had stopped (model unloaded mid-flight, worker crash, OOM
recovery) hung *forever* with no error and no log line — observed at 23
minutes against a 600s timeout, and reproduced here in seconds.

The fix is a deadline on PROGRESS rather than on bytes. These tests pin both
directions, because the whole difficulty is telling a stalled request apart
from a legitimately slow one: keepalives must not keep a dead request alive,
and must not kill a live one.

Real sockets, not MockTransport — the behaviour under test is wall-clock.
Budgets are kept tiny so the file stays fast.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from luxe.backend import Backend, BackendError


KEEPALIVE_CHUNK = {
    "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 0,
    "model": "keepalive",
    "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                 "finish_reason": None}],
}


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _serve(script):
    """Run `script(handler)` for each POST. Returns (base_url, shutdown)."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                script(self)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", srv.shutdown


def _run(base_url, *, stream, deadline=15.0, **kw):
    """Call chat() on a worker thread; report the outcome or that it hung."""
    b = Backend(base_url=base_url, model="probe", api_key="k", max_attempts=1, **kw)
    out = {}

    def call():
        t0 = time.monotonic()
        try:
            b.chat([{"role": "user", "content": "hi"}],
                   stream=stream, on_token=(lambda d: None) if stream else None)
            out["result"] = "returned"
        except Exception as e:                                   # noqa: BLE001
            out["result"] = e
        out["elapsed"] = time.monotonic() - t0

    t = threading.Thread(target=call, daemon=True)
    t.start()
    t.join(deadline)
    if t.is_alive():
        out["result"] = "HUNG"
        out["elapsed"] = deadline
    return out


# --- the bug: keepalives must not keep a dead request alive ---------------


def test_non_stream_whitespace_keepalives_do_not_defeat_the_deadline():
    """A bare `b' '` every 200ms used to hang forever. It must now abort."""
    def script(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.end_headers()
        while True:                                # trickle, never a body
            h.wfile.write(b" ")
            h.wfile.flush()
            time.sleep(0.2)

    url, stop = _serve(script)
    try:
        out = _run(url, stream=False, stall_timeout_s=1.5)
    finally:
        stop()
    assert out["result"] != "HUNG", "whitespace keepalives still defeat the deadline"
    assert isinstance(out["result"], BackendError)
    assert "stall" in str(out["result"]).lower()
    assert out["elapsed"] < 8.0, f"took {out['elapsed']:.1f}s"


def test_stream_keepalive_chunks_do_not_defeat_the_deadline():
    """`"model": "keepalive"` SSE chunks carry no content — not progress."""
    def script(h):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()
        while True:
            h.wfile.write(_sse(KEEPALIVE_CHUNK))
            h.wfile.flush()
            time.sleep(0.2)

    url, stop = _serve(script)
    try:
        out = _run(url, stream=True, stall_timeout_s=1.5)
    finally:
        stop()
    assert out["result"] != "HUNG", "keepalive chunks still defeat the deadline"
    assert isinstance(out["result"], BackendError)
    assert "stall" in str(out["result"]).lower()
    assert out["elapsed"] < 8.0, f"took {out['elapsed']:.1f}s"


def test_stall_after_tokens_uses_the_tighter_decode_bound():
    """Once tokens flow, a gap is unambiguous — don't wait out the prefill budget.

    This is the exact shape of the incident: real tokens, then the model is
    unloaded, then keepalives forever.
    """
    def script(h):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()
        h.wfile.write(_sse({"choices": [{"index": 0, "delta": {"content": "hello"}}]}))
        h.wfile.flush()
        while True:
            h.wfile.write(_sse(KEEPALIVE_CHUNK))
            h.wfile.flush()
            time.sleep(0.2)

    url, stop = _serve(script)
    try:
        # Generous prefill budget, tight decode budget: the decode bound must win.
        out = _run(url, stream=True, stall_timeout_s=60.0, decode_stall_timeout_s=1.5)
    finally:
        stop()
    assert out["result"] != "HUNG"
    assert isinstance(out["result"], BackendError)
    assert out["elapsed"] < 8.0, (
        f"decode-phase stall took {out['elapsed']:.1f}s — the tighter bound did not apply"
    )


# --- the no-regression side: keepalives must not kill a LIVE request ------


def test_keepalives_during_a_slow_prefill_do_not_abort_a_healthy_response():
    """The whole point of keepalives: a long prefill then a real answer.

    Guards against 'fixing' B6 by simply capping wall-clock time, which would
    break the dense/tunnel case keepalives exist to serve.
    """
    def script(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.end_headers()
        for _ in range(10):                        # 2s of "prefill"
            h.wfile.write(b" ")
            h.wfile.flush()
            time.sleep(0.2)
        h.wfile.write(json.dumps({
            "choices": [{"message": {"content": "done", "role": "assistant"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        }).encode())

    url, stop = _serve(script)
    try:
        # Budget comfortably exceeds the simulated 2s prefill. This is the
        # contract: keepalives covering a prefill SHORTER than the budget must
        # not abort it. (A budget below the real prefill time is supposed to
        # fire — that is the knob doing its job, not a bug.)
        out = _run(url, stream=False, stall_timeout_s=6.0)
    finally:
        stop()
    assert not isinstance(out["result"], Exception), out["result"]
    assert out["result"] == "returned"


def test_stream_keepalives_between_tokens_do_not_abort_a_healthy_stream():
    """Keepalives interleaved with real tokens keep resetting progress."""
    def script(h):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.end_headers()
        for i in range(4):
            for _ in range(4):
                h.wfile.write(_sse(KEEPALIVE_CHUNK))
                h.wfile.flush()
                time.sleep(0.2)
            h.wfile.write(_sse({"choices": [{"index": 0,
                                             "delta": {"content": f"tok{i} "}}]}))
            h.wfile.flush()
        h.wfile.write(_sse({"choices": [{"index": 0, "delta": {},
                                         "finish_reason": "stop"}]}))
        h.wfile.write(b"data: [DONE]\n\n")
        h.wfile.flush()

    url, stop = _serve(script)
    try:
        out = _run(url, stream=True, stall_timeout_s=60.0, decode_stall_timeout_s=1.5)
    finally:
        stop()
    assert not isinstance(out["result"], Exception), out["result"]


# --- unchanged behaviour ---------------------------------------------------


def test_silent_server_still_read_times_out():
    """No bytes at all is still the plain httpx read timeout — not a stall."""
    def script(h):
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.end_headers()
        time.sleep(30)

    url, stop = _serve(script)
    try:
        out = _run(url, stream=False, timeout_s=1.0, stall_timeout_s=60.0)
    finally:
        stop()
    assert isinstance(out["result"], BackendError)
    assert out["elapsed"] < 8.0


def test_non_stream_response_parses_exactly_as_before():
    """The read path changed; the parsed ChatResponse must not."""
    def script(h):
        payload = json.dumps({
            "choices": [{"message": {
                "content": "text out",
                "tool_calls": [{"id": "c1", "function": {
                    "name": "read_file", "arguments": '{"path": "a.py"}'}}],
            }, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22},
        }).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        h.wfile.write(payload)

    url, stop = _serve(script)
    try:
        b = Backend(base_url=url, model="probe", api_key="k", max_attempts=1)
        resp = b.chat([{"role": "user", "content": "hi"}])
    finally:
        stop()
    assert resp.text == "text out"
    assert resp.finish_reason == "tool_calls"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_file"
    assert resp.tool_calls[0].arguments == {"path": "a.py"}
    assert resp.timing.prompt_tokens == 11
    assert resp.timing.completion_tokens == 22


def test_http_error_body_still_surfaces():
    """4xx must still fail fast with the server's body in the message."""
    def script(h):
        body = b'{"error": "bad request detail"}'
        h.send_response(400)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    url, stop = _serve(script)
    try:
        b = Backend(base_url=url, model="probe", api_key="k", max_attempts=1)
        with pytest.raises(BackendError, match="bad request detail"):
            b.chat([{"role": "user", "content": "hi"}])
    finally:
        stop()
