"""Vendor extensions go at the top level of the request body.

`extra_body` is an OpenAI **SDK** convention: the SDK pops that dict and merges
it into the request before sending. `Backend.chat` posts raw JSON via
`httpx` (`json=body`), so nesting produced a literal

    {"extra_body": {"num_ctx": 32768, "repeat_penalty": 1.05}}

field on the wire. No server has ever read it. Two consequences, both live for
the life of the file until 2026-08-11:

  - `num_ctx` never reached any backend (moot against oMLX, which has no
    per-request context knob at any spelling, but not against Ollama-style
    servers, and it made the wire misleading to read);
  - `repeat_penalty` never reached any backend either — which is why the C10
    experiment (2026-06-11, `acceptance/c10_repeat_penalty/`) came back a
    "measured no-op". The two arms were byte-identical by construction.

Flattening is safe rather than a 400 because servers ignore unknown top-level
fields: oMLX's `ChatCompletionRequest` sets no `extra` policy, so pydantic v2
defaults to `extra="ignore"`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from luxe.backend import Backend


def _capture(**chat_kwargs) -> dict:
    """Run one `chat` against a transport that records the body."""
    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        })

    backend = Backend(base_url="http://test", model="m", api_key="k")
    backend._client = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(_handler))
    backend.chat([{"role": "user", "content": "hi"}], **chat_kwargs)
    return seen


class TestNumCtx:
    def test_it_is_a_top_level_field(self):
        body = _capture(num_ctx=32768)
        assert body["num_ctx"] == 32768

    def test_there_is_no_extra_body_wrapper(self):
        """The regression itself. A nested key is silently dropped by every
        server; nothing would fail loudly if this came back."""
        body = _capture(num_ctx=32768)
        assert "extra_body" not in body

    def test_it_is_omitted_when_unset(self):
        assert "num_ctx" not in _capture()


class TestRepeatPenalty:
    def test_both_spellings_are_sent(self):
        """The fleet runs two servers with two names for one knob: llama.cpp
        wants `repeat_penalty` (neo/micro-mind), oMLX wants
        `repetition_penalty` (a real field on its request model). Each ignores
        the other's, so sending both is what makes the knob work on both."""
        body = _capture(repeat_penalty=1.05)
        assert body["repeat_penalty"] == 1.05
        assert body["repetition_penalty"] == 1.05

    def test_it_is_omitted_when_unset(self):
        body = _capture()
        assert "repeat_penalty" not in body
        assert "repetition_penalty" not in body

    def test_the_omlx_spelling_is_the_one_its_schema_declares(self):
        """Guards the direction of the mapping. `repetition_penalty` is the
        field name on oMLX's ChatCompletionRequest; getting this backwards
        reinstates the silent no-op with extra steps."""
        body = _capture(repeat_penalty=1.1)
        assert "repetition_penalty" in body


class TestUnrelatedFieldsAreUntouched:
    @pytest.mark.parametrize("field,expected", [
        ("model", "m"), ("temperature", 0.2), ("stream", False),
    ])
    def test_the_rest_of_the_body_is_unchanged(self, field, expected):
        body = _capture(num_ctx=1024, repeat_penalty=1.05)
        assert body[field] == expected


def _capture_with_extras(extras, **chat_kwargs) -> dict:
    """Same as `_capture`, but for a Backend constructed with `body_extras`."""
    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        })

    backend = Backend(base_url="http://test", model="m", api_key="k",
                      body_extras=extras)
    backend._client = httpx.Client(
        base_url="http://test", transport=httpx.MockTransport(_handler))
    backend.chat([{"role": "user", "content": "hi"}], **chat_kwargs)
    return seen


class TestBodyExtras:
    """Per-endpoint declared body fields (2026-08-17, the openrouter carve-out).

    Constructor-injected, NOT a `chat()` kwarg: `agents/loop.py`'s call site is
    frozen (chat.sdd Must-not), and a Backend built by the benchmark path never
    receives extras — which is what keeps tests/test_golden_request.py green by
    construction rather than by care.
    """

    def test_they_are_top_level_fields(self):
        body = _capture_with_extras({"usage": {"include": True}})
        assert body["usage"] == {"include": True}
        assert "extra_body" not in body

    def test_they_are_absent_when_the_endpoint_declares_none(self):
        """The byte-identity guarantee for every local endpoint."""
        assert _capture_with_extras(None) == _capture_with_extras({})
        assert "usage" not in _capture_with_extras(None)

    def test_the_rest_of_the_body_is_unchanged(self):
        body = _capture_with_extras({"usage": {"include": True}})
        assert body["model"] == "m"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert body["temperature"] == 0.2
        assert body["stream"] is False

    def test_config_cannot_rewrite_what_luxe_assembled(self):
        """A per-endpoint dict is CONFIGURATION. Letting it set `model` or
        `messages` would make a YAML file able to silently redirect a turn."""
        body = _capture_with_extras({
            "model": "hijacked", "messages": [], "tools": [],
            "temperature": 9.9, "stream": True, "usage": {"include": True},
        })
        assert body["model"] == "m"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert body["temperature"] == 0.2
        assert body["stream"] is False
        assert body["usage"] == {"include": True}   # the legitimate one survives

    def test_the_backend_holds_its_own_copy(self):
        """Mutating the dict a caller passed must not rewrite the endpoint."""
        extras = {"usage": {"include": True}}
        b = Backend(base_url="http://test", model="m", api_key="k",
                    body_extras=extras)
        extras["usage"] = "clobbered"
        assert b.body_extras == {"usage": {"include": True}}
