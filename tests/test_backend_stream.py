"""Tests for the gated streaming path in src/luxe/backend.py.

Two guarantees matter most (chat.sdd / the determinism gate):
  1. stream=False (the default) builds a request body byte-identical to the
     legacy request, and never invokes on_token.
  2. stream=True reconstructs the same ChatResponse shape from SSE chunks and
     fires on_token per text fragment.
"""

from __future__ import annotations

import json

import httpx
import pytest

from luxe.backend import Backend


def _backend(transport) -> Backend:
    backend = Backend(model="test")
    backend._client = httpx.Client(base_url=backend.base_url, transport=transport)
    return backend


# --- (1) default path byte-identity + on_token inert ----------------------


def test_default_body_is_byte_identical_and_on_token_inert():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {"content": "hi", "role": "assistant"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    backend = _backend(httpx.MockTransport(handler))
    token_calls: list[str] = []
    backend.chat(
        [{"role": "user", "content": "hi"}],
        max_tokens=2048,
        temperature=0.0,
        on_token=lambda t: token_calls.append(t),  # must be ignored
    )
    # The exact legacy body: model/messages/max_tokens/temperature/stream:false.
    assert captured["body"] == {
        "model": "test",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 2048,
        "temperature": 0.0,
        "stream": False,
    }
    assert token_calls == []  # on_token never fired on the non-stream path


def test_default_body_has_no_stream_options():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {},
        })

    backend = _backend(httpx.MockTransport(handler))
    backend.chat([{"role": "user", "content": "hi"}])
    assert "stream_options" not in captured["body"]
    assert captured["body"]["stream"] is False


# --- (2) streaming reconstruction -----------------------------------------


def _sse(*chunks: dict) -> str:
    lines = []
    for c in chunks:
        lines.append(f"data: {json.dumps(c)}")
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def test_stream_reconstructs_text_and_fires_on_token():
    body = _sse(
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo "}}]},
        {"choices": [{"delta": {"content": "world"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=body)

    backend = _backend(httpx.MockTransport(handler))
    tokens: list[str] = []
    resp = backend.chat(
        [{"role": "user", "content": "hi"}],
        stream=True,
        on_token=lambda t: tokens.append(t),
    )
    assert resp.text == "Hello world"
    assert tokens == ["Hel", "lo ", "world"]
    assert resp.finish_reason == "stop"
    assert resp.timing.completion_tokens == 4
    assert resp.timing.prompt_tokens == 3


def test_stream_reconstructs_tool_calls_from_fragments():
    body = _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1",
             "function": {"name": "read_file", "arguments": "{\"pa"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "th\": \"a.py\"}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    backend = _backend(httpx.MockTransport(handler))
    resp = backend.chat([{"role": "user", "content": "hi"}], stream=True)
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    assert tc.arguments == {"path": "a.py"}
    assert resp.finish_reason == "tool_calls"


# --- timeout_s plumbing (multi-backend regression) --------------------------
# The 2400s value briefly lived as a hardcoded Backend default (the m5-tunnel
# hack); it now belongs to BackendEntry.timeout_s. Backend must keep its 600s
# default AND honor an explicit per-endpoint override end-to-end.


def test_backend_default_timeout_is_600():
    b = Backend(model="test")
    assert b.timeout_s == 600.0
    assert b._client.timeout.read == 600.0
    assert b._client.timeout.connect == 30.0


def test_backend_timeout_override_plumbs_to_http_client():
    b = Backend(model="test", timeout_s=2400.0)
    assert b.timeout_s == 2400.0
    assert b._client.timeout.read == 2400.0
    # connect stays snappy even with a long read timeout
    assert b._client.timeout.connect == 30.0


def test_stream_request_includes_usage_options():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text=_sse(
            {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]},
        ))

    backend = _backend(httpx.MockTransport(handler))
    backend.chat([{"role": "user", "content": "hi"}], stream=True)
    assert captured["body"]["stream"] is True
    assert captured["body"]["stream_options"] == {"include_usage": True}


# --- (3) stream-path retry decisions reach the logger (2026-08-04) ---------


def test_stream_failure_decision_is_logged(caplog):
    """The stream path used to hand retry decisions to `on_retry` and log
    nothing — during an outage the retry history never reached debug.log.
    It must now log the same `decision=` line as the non-stream path."""
    import logging

    from luxe.backend import BackendError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    backend = _backend(httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING, logger="luxe.backend"):
        with pytest.raises(BackendError):
            backend.chat([{"role": "user", "content": "hi"}], stream=True)
    assert any("decision=" in r.getMessage() for r in caplog.records)


# --- per-request cost (2026-08-17, the openrouter carve-out) ---------------
#
# A metered endpoint nests the USD it charged inside `usage` once the body
# declares `usage: {"include": true}` (BackendEntry.body_extras). It has to be
# parsed on BOTH paths: chat streams, and the benchmark/maintain path does not,
# so reading it in only one place would make the spend total depend on which
# front-end happened to run the turn.


def test_non_stream_path_parses_cost_from_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi", "role": "assistant"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                      "cost": 0.00123},
        })

    resp = _backend(httpx.MockTransport(handler)).chat(
        [{"role": "user", "content": "hi"}])
    assert resp.timing.cost_usd == pytest.approx(0.00123)


def test_stream_path_parses_cost_from_the_terminal_chunk():
    body = _sse(
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 4, "cost": 0.0042}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    resp = _backend(httpx.MockTransport(handler)).chat(
        [{"role": "user", "content": "hi"}], stream=True)
    assert resp.timing.cost_usd == pytest.approx(0.0042)


def test_a_server_that_reports_no_cost_yields_none_not_zero():
    """"Free" and "never said" are different facts, and the cost surfaces must
    not render a confident $0.00 for an endpoint that simply didn't report."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    resp = _backend(httpx.MockTransport(handler)).chat(
        [{"role": "user", "content": "hi"}])
    assert resp.timing.cost_usd is None


def test_a_string_cost_is_accepted_and_junk_is_not():
    from luxe.backend import _usage_cost
    assert _usage_cost({"cost": "0.5"}) == 0.5
    assert _usage_cost({"cost": None}) is None
    assert _usage_cost({"cost": "free"}) is None
    assert _usage_cost({"cost": True}) is None      # bool is not a price
    assert _usage_cost({}) is None
    assert _usage_cost(None) is None


def test_the_loop_sums_cost_across_a_multi_step_turn():
    """One turn is many `backend.chat` calls, each separately billed. A cap
    reading only the last step's cost would undercount by the turn's length."""
    from luxe.agents.loop import AgentResult

    r = AgentResult()
    for c in (0.001, 0.002, 0.004):
        r.cost_usd += c
    assert r.cost_usd == pytest.approx(0.007)
    assert AgentResult().cost_usd == 0.0
