"""Characterization of one streaming attempt (`Backend._stream_once`).

Written 2026-10-22 against the pre-decomposition code, before
`_stream_once` was split into an accumulator + helpers, so that every branch
the refactor moves is pinned by something that fails if it changes. Covers
what the other stream suites leave open:

  - SSE line tolerance: bare JSON, comments, non-JSON, non-dict JSON,
    `data:` without its space, a mid-stream `[DONE]`, junk choices, `delta:
    null`, a falsy `error` field;
  - tool-call fragment assembly (index order, missing index, late id/name,
    empty and unparseable arguments) and the text-channel salvage;
  - usage / timings / finish_reason accumulation;
  - callback order across the reasoning, answer and tool channels;
  - the B6 progress clock, exactly: which chunk kinds reset it, which bound
    applies, the stall message, and that it is terminal — on a FAKE clock
    advanced by the stream itself, so it is deterministic and instant
    (test_backend_stall.py covers the same clock against a real server);
  - which chunk kinds count as output for the no-replay rule.

MockTransport throughout; never the real oMLX on :8000.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

import luxe.backend as backend_mod
from luxe.backend import Backend, BackendError

MSG = [{"role": "user", "content": "hi"}]
READ_FILE_TOOL = [{"type": "function", "function": {
    "name": "read_file", "parameters": {"type": "object"}}}]


def _data(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _content(t: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": t}}]}


def _reasoning(t: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"reasoning": t}}]}


_KEEPALIVE = {"model": "keepalive", "choices": [{"delta": {"content": ""}}]}
_STOP = {"choices": [{"delta": {}, "finish_reason": "stop"}]}


class _Seq(httpx.MockTransport):
    """One response per request, in order; counts dispatches."""

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
    kw.setdefault("max_attempts", 3)
    b = Backend(model="test", api_key="k", **kw)
    b._client = httpx.Client(base_url=b.base_url, transport=transport)
    return b


def _ok_stream(*parts: bytes) -> _Seq:
    return _Seq([httpx.Response(200, content=b"".join(parts))])


# --- SSE line tolerance -----------------------------------------------------


def test_a_messy_stream_keeps_exactly_the_parseable_content():
    body = b"".join([
        b": an SSE comment line\n\n",
        b"data: not json at all\n\n",
        b"data: [1, 2, 3]\n\n",                    # JSON, but not an object
        b'data: "a string"\n\n',
        b"data:    \n\n",                          # whitespace only
        b'data:{"choices":[{"delta":{"content":"LOST"}}]}\n\n',  # no space
        b'{"choices":[{"delta":{"content":"A"}}]}\n\n',          # bare JSON
        _data({"choices": ["junk", None, {"delta": {"content": "B"}}]}),
        _data({"choices": [{"delta": None}]}),
        _data({"choices": None}),
        _data({"error": None, "choices": [{"delta": {"content": "C"}}]}),
        _data({"error": "", "choices": [{"delta": {"content": "D"}}]}),
        b"data: [DONE]\n\n",
        # [DONE] is skipped, not a terminator: later chunks still count.
        _data(_content("E")),
        _data(_STOP),
        b"data: [DONE]\n\n",
    ])
    tokens: list[str] = []
    resp = _backend(_ok_stream(body)).chat(
        MSG, stream=True, on_token=tokens.append)
    assert tokens == ["A", "B", "C", "D", "E"]
    assert resp.text == "ABCDE"
    assert resp.finish_reason == "stop"
    assert resp.tool_calls == []
    assert resp.reasoning_chars == 0 and resp.reasoning_text == ""


def test_an_empty_stream_is_an_empty_response():
    resp = _backend(_ok_stream(b"data: [DONE]\n\n")).chat(
        MSG, stream=True, on_token=lambda s: None)
    assert resp.text == "" and resp.tool_calls == []
    assert resp.finish_reason == ""
    assert resp.timing.prompt_tokens == 0
    assert resp.timing.completion_tokens == 0
    assert resp.timing.cost_usd is None
    assert resp.retries == 0


# --- tool-call fragment assembly --------------------------------------------


def test_tool_call_fragments_assemble_by_index():
    body = b"".join([
        _data({"choices": [{"delta": {"content": "Let me look."}}]}),
        # Index 1 opens first; the result is still ordered by index.
        _data({"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "c1",
             "function": {"name": "grep", "arguments": "{\"pat"}}]}}]}),
        # No index → slot 0. Its id and name arrive in a LATER fragment.
        _data({"choices": [{"delta": {"tool_calls": [
            {"function": {"arguments": ""}}]}}]}),
        _data({"choices": [{"delta": {"tool_calls": [
            {"index": 1, "function": {"arguments": "tern\": \"x\"}"}},
            {"index": 0, "id": "c0", "function": {"name": "list_dir"}},
        ]}}]}),
        # A later fragment with an empty id/name does not erase them.
        _data({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "", "function": {"name": ""}}]}}]}),
        # Index 2: arguments that never become JSON.
        _data({"choices": [{"delta": {"tool_calls": [
            {"index": 2, "id": "c2", "function": {"name": "bash",
                                                  "arguments": "{not json"}}]}}]}),
        # Index 3: a fragment with no `function` key at all.
        _data({"choices": [{"delta": {"tool_calls": [{"index": 3, "id": "c3"}]}}]}),
        _data({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
    ])
    resp = _backend(_ok_stream(body)).chat(
        MSG, tools=READ_FILE_TOOL, stream=True, on_token=lambda s: None)
    got = [(tc.id, tc.name, tc.arguments) for tc in resp.tool_calls]
    assert got == [
        ("c0", "list_dir", {}),                   # empty arguments → {}
        ("c1", "grep", {"pattern": "x"}),
        ("c2", "bash", {"_raw": "{not json"}),
        ("c3", "", {}),
    ]
    # Native calls present → the prose is kept and nothing is salvaged.
    assert resp.text == "Let me look."
    assert resp.finish_reason == "tool_calls"


def test_a_tool_call_written_into_content_is_salvaged_on_the_stream_path():
    call = '{"name": "read_file", "arguments": {"path": "a.py"}}'
    body = _data(_content(call[:20])) + _data(_content(call[20:])) + _data(_STOP)
    tokens: list[str] = []
    resp = _backend(_ok_stream(body)).chat(
        MSG, tools=READ_FILE_TOOL, stream=True, on_token=tokens.append)
    assert tokens == [call[:20], call[20:]]     # the UI still saw the raw JSON
    assert resp.text == ""
    assert [(tc.id, tc.name, tc.arguments) for tc in resp.tool_calls] == [
        ("recovered_call_0", "read_file", {"path": "a.py"})]


def test_no_salvage_when_the_request_offered_no_tools():
    call = '{"name": "read_file", "arguments": {"path": "a.py"}}'
    resp = _backend(_ok_stream(_data(_content(call)), _data(_STOP))).chat(
        MSG, stream=True, on_token=lambda s: None)
    assert resp.text == call and resp.tool_calls == []


# --- usage / timings / finish_reason ----------------------------------------


def test_usage_timings_and_finish_reason_accumulate():
    body = b"".join([
        _data({"choices": [{"delta": {"content": "x"}, "finish_reason": "length"}],
               "usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
        # A later usage block replaces the earlier one wholesale; a non-dict
        # usage is ignored rather than clobbering it.
        _data({"choices": [], "usage": {
            "prompt_tokens": 50, "completion_tokens": 7, "cost": 0.25,
            "prompt_tokens_details": {"cached_tokens": 40}}}),
        _data({"choices": [{"delta": {}, "finish_reason": None}], "usage": None}),
        # An empty finish_reason does not overwrite the last real one.
        _data({"choices": [{"delta": {}, "finish_reason": ""}]}),
        # llama-server's timings block (fills what usage did not report).
        _data({"choices": [], "timings": {"cache_n": 999, "prompt_ms": 1500,
                                          "predicted_ms": 250}}),
        _data({"choices": [], "timings": "not a dict"}),
    ])
    b = _backend(_ok_stream(body))
    resp = b.chat(MSG, stream=True, on_token=lambda s: None)
    assert resp.finish_reason == "length"
    t = resp.timing
    assert (t.prompt_tokens, t.completion_tokens) == (50, 7)
    assert t.cost_usd == 0.25 and b.cost_total_usd == 0.25
    assert t.cached_tokens == 40                # usage wins over timings
    assert t.prefill_s == 1.5 and t.decode_s == 0.25


def test_the_last_finish_reason_wins():
    body = _data({"choices": [{"delta": {"content": "a"}, "finish_reason": "length"}]}) \
        + _data(_STOP)
    resp = _backend(_ok_stream(body)).chat(MSG, stream=True,
                                           on_token=lambda s: None)
    assert resp.finish_reason == "stop"


# --- callback order across channels -----------------------------------------


def test_callbacks_fire_in_stream_order_reasoning_before_content_in_a_chunk():
    events: list[tuple[str, str]] = []
    body = b"".join([
        _data(_reasoning("plan")),
        _data({"choices": [{"delta": {"reasoning": "more", "content": "Hi"}}]}),
        _data({"choices": [{"delta": {"content": " there", "tool_calls": [
            {"index": 0, "id": "t", "function": {"name": "read_file",
                                                 "arguments": "{}"}}]}}]}),
        # Two choices in one chunk are handled in order.
        _data({"choices": [{"delta": {"content": "!"}},
                           {"delta": {"reasoning_content": "late"}}]}),
        _data(_STOP),
    ])
    b = _backend(_ok_stream(body))
    b.on_reasoning = lambda s: events.append(("reasoning", s))
    resp = b.chat(MSG, stream=True, on_token=lambda s: events.append(("token", s)))
    assert events == [
        ("reasoning", "plan"), ("reasoning", "more"), ("token", "Hi"),
        ("token", " there"), ("token", "!"), ("reasoning", "late"),
    ]
    assert resp.text == "Hi there!"
    assert resp.reasoning_text == "planmorelate"
    assert resp.reasoning_chars == len("planmorelate")
    assert [tc.name for tc in resp.tool_calls] == ["read_file"]


def test_streaming_without_any_callback_still_assembles_the_answer():
    body = _data(_reasoning("hm")) + _data(_content("ok")) + _data(_STOP)
    resp = _backend(_ok_stream(body)).chat(MSG, stream=True)
    assert resp.text == "ok" and resp.reasoning_text == "hm"


# --- the progress clock, on a fake clock ------------------------------------


class _Clock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class _TimedStream(httpx.SyncByteStream):
    """Sets the fake clock to `t` immediately before delivering each piece."""

    def __init__(self, clock: _Clock, events: list[tuple[float, bytes]]):
        self.clock = clock
        self.events = events

    def __iter__(self):
        for t, data in self.events:
            self.clock.now = t
            yield data


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    c = _Clock()
    # Patch the module's `time` binding only — the real `time` module (and
    # httpx/pytest's use of it) is untouched.
    monkeypatch.setattr(backend_mod, "time",
                        SimpleNamespace(monotonic=c, sleep=lambda s: None))
    return c


def _timed(clock, events, **kw):
    t = _Seq([httpx.Response(200, stream=_TimedStream(clock, events))
              for _ in range(kw.pop("responses", 1))])
    return t, _backend(t, **kw)


def test_ttft_is_pinned_to_the_first_content_token_and_wall_to_the_end(clock):
    _, b = _timed(clock, [
        (103.0, _data(_reasoning("hm"))),
        (107.0, _data(_content("a"))),
        (109.0, _data(_content("b"))),
        (111.5, _data(_STOP)),
    ])
    resp = b.chat(MSG, stream=True, on_token=lambda s: None)
    assert resp.timing.time_to_first_token_s == 7.0
    assert resp.timing.total_s == 11.5


def test_a_tool_only_turn_has_no_ttft(clock):
    _, b = _timed(clock, [
        (104.0, _data({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c", "function": {"name": "read_file",
                                                 "arguments": "{}"}}]}}]})),
        (105.0, _data({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})),
    ])
    resp = b.chat(MSG, tools=READ_FILE_TOOL, stream=True, on_token=lambda s: None)
    assert resp.timing.time_to_first_token_s == 0.0
    assert resp.timing.total_s == 5.0


def _stall_message(exc: pytest.ExceptionInfo) -> str:
    return str(exc.value)


def test_keepalives_alone_stall_at_the_prefill_bound_and_are_not_retried(clock):
    t, b = _timed(clock, [
        (105.0, _data(_KEEPALIVE)),
        (110.0, b": comment-style keepalive\n\n"),
        (112.0, _data(_KEEPALIVE)),
        (200.0, _data(_content("never reached"))),
    ], stall_timeout_s=10, decode_stall_timeout_s=3)
    with pytest.raises(BackendError) as e:
        b.chat(MSG, stream=True, on_token=lambda s: None)
    assert _stall_message(e) == (
        "oMLX stream failed: ProgressStall: progress stall: 12s of keepalive "
        "chunks with no first token (bound=10s) (progress-stall)")
    assert t.calls == 1                         # terminal: one budget, not 3×


def test_a_usage_chunk_counts_as_progress(clock):
    _, b = _timed(clock, [
        (106.0, _data({"choices": [], "usage": {"prompt_tokens": 1}})),
        (115.0, _data(_KEEPALIVE)),             # 9s after usage; 15s after start
        (116.0, _data(_content("ok"))),
        (117.0, _data(_STOP)),
    ], stall_timeout_s=10)
    assert b.chat(MSG, stream=True, on_token=lambda s: None).text == "ok"


def test_a_finish_reason_counts_as_progress(clock):
    _, b = _timed(clock, [
        (106.0, _data({"choices": [{"delta": {}, "finish_reason": "length"}]})),
        (115.0, _data(_KEEPALIVE)),
        (116.0, _data(_content("ok"))),
    ], stall_timeout_s=10)
    assert b.chat(MSG, stream=True, on_token=lambda s: None).text == "ok"


def test_a_timings_chunk_does_not_count_as_progress(clock):
    _, b = _timed(clock, [
        (106.0, _data({"choices": [], "timings": {"prompt_ms": 5}})),
        (112.0, _data(_KEEPALIVE)),
    ], stall_timeout_s=10)
    with pytest.raises(BackendError, match=r"12s of keepalive chunks with no "
                                           r"first token \(bound=10s\)"):
        b.chat(MSG, stream=True, on_token=lambda s: None)


def test_reasoning_resets_the_clock_but_keeps_the_prefill_bound(clock):
    _, b = _timed(clock, [
        (105.0, _data(_reasoning("hm"))),
        (112.0, _data(_KEEPALIVE)),             # 7s > decode bound, < prefill
        (113.0, _data(_reasoning("still"))),
        (122.0, _data(_KEEPALIVE)),             # 9s; 22s since start
        (123.0, _data(_content("ok"))),
    ], stall_timeout_s=10, decode_stall_timeout_s=3)
    assert b.chat(MSG, stream=True, on_token=lambda s: None).text == "ok"


def test_a_content_token_switches_to_the_decode_bound(clock):
    _, b = _timed(clock, [
        (101.0, _data(_content("a"))),
        (105.0, _data(_KEEPALIVE)),
    ], stall_timeout_s=100, decode_stall_timeout_s=3)
    with pytest.raises(BackendError, match=r"progress stall: 4s of keepalive "
                                           r"chunks with no tokens \(bound=3s\)"):
        b.chat(MSG, stream=True, on_token=lambda s: None)


def test_a_tool_call_fragment_switches_to_the_decode_bound(clock):
    _, b = _timed(clock, [
        (101.0, _data({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c", "function": {"name": "read_file"}}]}}]})),
        (105.0, _data(_KEEPALIVE)),
    ], stall_timeout_s=100, decode_stall_timeout_s=3)
    with pytest.raises(BackendError, match=r"no tokens \(bound=3s\)"):
        b.chat(MSG, tools=READ_FILE_TOOL, stream=True, on_token=lambda s: None)


def test_the_gap_is_exclusive_at_the_bound(clock):
    """`>` not `>=`: a gap of exactly the bound is still alive."""
    _, b = _timed(clock, [
        (110.0, _data(_KEEPALIVE)),
        (110.0, _data(_content("ok"))),
    ], stall_timeout_s=10)
    assert b.chat(MSG, stream=True, on_token=lambda s: None).text == "ok"


# --- what counts as output for the no-replay rule ---------------------------


class _BreakingStream(httpx.SyncByteStream):
    def __init__(self, head: bytes):
        self.head = head

    def __iter__(self):
        yield self.head
        raise httpx.RemoteProtocolError("peer closed connection mid-body")


def test_a_tool_fragment_counts_as_output_so_a_break_is_not_retried(monkeypatch):
    monkeypatch.setattr(backend_mod.time, "sleep", lambda s: None)
    t = _Seq([
        httpx.Response(200, stream=_BreakingStream(_data({"choices": [{"delta": {
            "tool_calls": [{"index": 0, "function": {"name": "read_file"}}]}}]}))),
        httpx.Response(200, content=_data(_content("x"))),
    ])
    with pytest.raises(BackendError, match="after-output"):
        _backend(t).chat(MSG, stream=True, on_token=lambda s: None)
    assert t.calls == 1


def test_usage_finish_and_keepalives_are_not_output_so_a_break_retries(monkeypatch):
    monkeypatch.setattr(backend_mod.time, "sleep", lambda s: None)
    head = b"".join([
        _data(_KEEPALIVE),
        _data({"choices": [], "usage": {"prompt_tokens": 3}}),
        _data({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        _data({"choices": [], "timings": {"prompt_ms": 5}}),
    ])
    t = _Seq([
        httpx.Response(200, stream=_BreakingStream(head)),
        httpx.Response(200, content=_data(_content("fine")) + _data(_STOP)),
    ])
    resp = _backend(t).chat(MSG, stream=True, on_token=lambda s: None)
    assert t.calls == 2 and resp.retries == 1
    assert resp.text == "fine" and resp.finish_reason == "stop"
    # Nothing from the failed attempt leaks into the retried response.
    assert resp.timing.prompt_tokens == 0


# --- in-band error chunk: exact surface -------------------------------------


def test_an_error_chunk_reports_the_stripped_line_and_message(caplog):
    err = {"error": {"code": 502, "message": "Provider returned error"},
           "choices": [{"delta": {"content": "zz"}, "finish_reason": "error"}]}
    # Compact separators + trailing blanks: the logged body is the line as
    # received, minus its `data: ` prefix and surrounding whitespace.
    line = json.dumps(err, separators=(",", ":"))
    tokens: list[str] = []
    t = _Seq([httpx.Response(200, content=_data(_content("par"))
                             + b"data: " + line.encode() + b"   \n\n"
                             + _data(_content("after")))])
    with caplog.at_level(logging.WARNING, logger="luxe.backend"):
        with pytest.raises(BackendError) as e:
            _backend(t).chat(MSG, stream=True, on_token=tokens.append)
    assert str(e.value) == ("oMLX stream returned an error: Provider returned "
                            "error [code=502] (stream-error-chunk)")
    # The error chunk's own delta is never delivered, nor anything after it.
    assert tokens == ["par"]
    assert any(
        r.getMessage() == (
            f"backend test status=200 body={line[:200]!r} decision="
            "RetryDecision(retry=False, reason='stream-error-chunk', "
            "delay_s=0.0)")
        for r in caplog.records)


def test_an_error_status_reads_the_body_and_retries_before_output(monkeypatch):
    monkeypatch.setattr(backend_mod.time, "sleep", lambda s: None)
    t = _Seq([
        httpx.Response(503, text="model loading"),
        httpx.Response(200, content=_data(_content("up")) + _data(_STOP)),
    ])
    resp = _backend(t).chat(MSG, stream=True, on_token=lambda s: None)
    assert resp.text == "up" and t.calls == 2 and resp.retries == 1
