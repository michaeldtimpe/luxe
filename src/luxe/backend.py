"""oMLX backend — OpenAI-compatible chat completions client.

Resilience features (rev 2):
- Startup health-check with model resolution (`assert_models_available`).
- Mid-flight retry that distinguishes transient (model loading/swapping)
  from terminal (server crashed / OOM) failures by inspecting the response
  body. Burning 21s of retry on a crashed server is wasteful; the body
  inspection lets us fail fast.
- Model-swap thermal guard — between stages where the model changes, sleep
  briefly and re-confirm /v1/models reports the target loaded before the
  first chat call.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx


logger = logging.getLogger(__name__)


@dataclass
class GenerationTiming:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_s: float = 0.0
    time_to_first_token_s: float = 0.0

    @property
    def decode_tok_per_s(self) -> float:
        if self.total_s <= 0 or self.completion_tokens <= 0:
            return 0.0
        return self.completion_tokens / self.total_s


@dataclass
class ToolCallResponse:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatResponse:
    text: str = ""
    tool_calls: list[ToolCallResponse] = field(default_factory=list)
    finish_reason: str = ""
    timing: GenerationTiming = field(default_factory=GenerationTiming)
    retries: int = 0


# --- Text-channel tool-call recovery -----------------------------------------
#
# Some models write a tool call into `content` instead of the template's
# native wrapper. Qwen2.5-Coder (1.5B/3B/7B, temp 0) does this consistently:
# it emits a fenced ```json {"name": ..., "arguments": {...}} ``` block, the
# server's template parser never sees a `<tool_call>` tag, and the response
# arrives with `tool_calls: null` and the call sitting in prose. The agent
# then counts 0 tool calls and the turn dies. (Root-caused 2026-08-03 during
# the neo bake-off; micro-mind's equivalent recovery path measured
# native=0/recovered=1 on every Qwen2.5-Coder turn.)
#
# Recovery is deliberately narrow, mirroring micro-mind's belt-and-braces
# parser: only when the request OFFERED tools, only when the native channel
# is empty, and only when the extracted JSON names an offered tool with dict
# arguments. Prose that merely contains braces never matches; a model that
# names an un-offered tool is not "recovered" into calling it.


def _first_balanced_json(text: str, start: int) -> str | None:
    """The shortest balanced {...} substring starting at `start`, or None."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def recover_tool_calls_from_text(
    text: str, tools: list[dict[str, Any]] | None
) -> list[ToolCallResponse]:
    """Salvage a tool call the model wrote into `content`.

    Returns at most one call (single-action bias — a content-channel model
    that "chains" calls in prose is speculating, not acting). Empty list when
    nothing recoverable is present.
    """
    if not text or not tools:
        return []
    offered = {
        (t.get("function") or {}).get("name")
        for t in tools
        if isinstance(t, dict)
    }
    offered.discard(None)
    if not offered:
        return []
    pos = text.find("{")
    while pos != -1:
        candidate = _first_balanced_json(text, pos)
        if candidate:
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict) and obj.get("name") in offered:
                args = obj.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                if isinstance(args, dict):
                    return [ToolCallResponse(
                        id="recovered_call_0",
                        name=obj["name"],
                        arguments=args,
                    )]
        pos = text.find("{", pos + 1)
    return []


# --- Retry classification ---------------------------------------------------

_TRANSIENT_BODY_MARKERS = ("loading", "swapping", "warming", "starting", "not yet ready")
_TERMINAL_BODY_MARKERS = ("unavailable", "crashed", "out of memory", "oom", "shut down")
_WARMUP_WINDOW_S = 5.0
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BACKOFF_S = (1.0, 4.0, 16.0)


@dataclass
class RetryDecision:
    retry: bool
    reason: str
    delay_s: float = 0.0


def classify_failure(
    *,
    exc: Exception | None = None,
    status_code: int | None = None,
    body: str = "",
    elapsed_since_start_s: float = 0.0,
    attempt: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> RetryDecision:
    """Decide whether to retry based on the failure shape and elapsed time.

    Retry on:
      - connection / read timeouts (httpx.RequestError, httpx.TimeoutException)
      - 5xx with body containing transient markers (loading / swapping / warming)
      - 5xx with empty body during the warmup window (first 5s of a run)

    Fail fast on:
      - 4xx (our request bug, retrying won't help)
      - 5xx with terminal markers (unavailable / crashed / OOM)
      - 5xx with empty body AFTER warmup window (assume terminal)
      - any failure on the last attempt
    """
    if attempt + 1 >= max_attempts:
        return RetryDecision(retry=False, reason="exhausted-attempts")

    delay = _DEFAULT_BACKOFF_S[min(attempt, len(_DEFAULT_BACKOFF_S) - 1)]

    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                        httpx.NetworkError, httpx.RemoteProtocolError)):
        return RetryDecision(retry=True, reason=f"transient-{type(exc).__name__}", delay_s=delay)

    if status_code is None:
        # Other RequestError subclasses we don't specifically recognise — treat as terminal.
        if exc is not None:
            return RetryDecision(retry=False, reason=f"unknown-error-{type(exc).__name__}")
        return RetryDecision(retry=False, reason="no-status-no-exception")

    if 400 <= status_code < 500:
        return RetryDecision(retry=False, reason=f"4xx-{status_code}")

    if 500 <= status_code < 600:
        body_lc = (body or "").lower()
        for marker in _TERMINAL_BODY_MARKERS:
            if marker in body_lc:
                return RetryDecision(retry=False, reason=f"5xx-terminal-{marker}")
        for marker in _TRANSIENT_BODY_MARKERS:
            if marker in body_lc:
                return RetryDecision(retry=True, reason=f"5xx-transient-{marker}", delay_s=delay)
        if not body_lc.strip() and elapsed_since_start_s < _WARMUP_WINDOW_S:
            return RetryDecision(retry=True, reason="5xx-empty-warmup", delay_s=delay)
        return RetryDecision(retry=False, reason="5xx-empty-post-warmup")

    return RetryDecision(retry=False, reason=f"unexpected-{status_code}")


# --- Backend ---------------------------------------------------------------


class BackendError(Exception):
    """Raised when the backend gives up after retries."""


class Backend:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        model: str = "",
        # 600s suits the local box. Slow remote endpoints need more — dense
        # 16384-tok turns over the m5 Tailscale link can run ~25 min, and a 600s
        # read-timeout there left dead-request hangs. That case is now expressed
        # per-endpoint via BackendEntry.timeout_s (configs/chat.yaml `backends:`
        # m5 → 2400), NOT by raising this default.
        timeout_s: float = 600.0,
        api_key: str = "",
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        # --- progress deadlines (B6) ---------------------------------------
        # `timeout_s` above is httpx's PER-READ deadline: it fires only after
        # that long with NO byte at all. oMLX deliberately breaks the silence —
        # it emits keepalives so a long prefill doesn't trip the read timeout
        # (SSE chunks with `"model": "keepalive"` when streaming; a bare space
        # byte under chunked encoding when not). Those bytes reset the read
        # clock, so a request whose generation has STOPPED — model unloaded
        # mid-flight, worker crash, OOM recovery — used to hang forever with no
        # error and no log line. Observed at 23 minutes against a 600s timeout.
        #
        # So we keep a second clock, on PROGRESS rather than on bytes.
        # Keepalives are liveness, not progress, and never reset it.
        #
        # Two bounds, because a stalled request and a slow one look identical
        # until the first token: before any content arrives the wait is a
        # legitimate prefill (which is exactly what keepalives exist to cover),
        # so it gets a generous budget; once tokens are flowing a gap is
        # unambiguous and gets a tight one. Raising `timeout_s` cannot
        # substitute for either — no finite per-read value survives a trickle.
        stall_timeout_s: float = 1800.0,
        decode_stall_timeout_s: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.stall_timeout_s = stall_timeout_s
        self.decode_stall_timeout_s = decode_stall_timeout_s
        self.max_attempts = max_attempts
        # Pick api_key from arg first, then env → ~/.luxe/secrets.env →
        # keychain (luxe.secrets). Many oMLX deployments require auth;
        # without a key, every chat call 401s — and shells that source
        # secrets.env without exporting used to produce exactly that.
        if not api_key:
            from luxe.secrets import resolve_api_key
            api_key = resolve_api_key()
        self.api_key = api_key
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=30.0),
            headers=headers,
        )
        self._created_at = time.monotonic()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        num_ctx: int | None = None,
        repeat_penalty: float | None = None,
        response_format: dict[str, Any] | None = None,
        on_retry: Callable[[RetryDecision, int], None] | None = None,
        stream: bool = False,
        on_token: Callable[[str], None] | None = None,
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
        if response_format is not None:
            # Constrained decoding (e.g. {"type": "json_object"}). Suppresses the
            # reasoning preamble for thinking models — load-bearing for the reflect
            # verifier, which must emit a parseable verdict, not a CoT trace.
            body["response_format"] = response_format
        if num_ctx is not None:
            body.setdefault("extra_body", {})["num_ctx"] = num_ctx
        if repeat_penalty is not None:
            body.setdefault("extra_body", {})["repeat_penalty"] = repeat_penalty

        # Opt-in streaming path for the interactive chat front-end. The default
        # (stream=False) leaves the body byte-identical to the legacy request and
        # never touches `on_token` — the benchmark/maintain path is unchanged.
        if stream:
            return self._chat_stream(body, on_token=on_token, on_retry=on_retry)

        attempt = 0
        last_decision: RetryDecision | None = None
        request_t0 = time.monotonic()

        while attempt < self.max_attempts:
            t0 = time.monotonic()
            try:
                # Read via stream() rather than post() purely for visibility:
                # post() hands back a fully-buffered body, which gives us no
                # way to notice that the only thing arriving is keepalive
                # whitespace. The REQUEST is unchanged (same method, URL and
                # json=body), so the benchmark payload stays byte-identical —
                # tests/test_golden_request.py pins that.
                with self._client.stream(
                    "POST", "/v1/chat/completions", json=body
                ) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                    else:
                        raw_body = self._read_body_with_progress(resp)
                    wall = time.monotonic() - t0
                if resp.status_code >= 400:
                    decision = classify_failure(
                        status_code=resp.status_code,
                        body=resp.text,
                        elapsed_since_start_s=time.monotonic() - request_t0,
                        attempt=attempt,
                        max_attempts=self.max_attempts,
                    )
                    last_decision = decision
                    logger.warning(
                        "backend %s status=%d body=%r decision=%s",
                        self.model, resp.status_code, resp.text[:200], decision,
                    )
                    if not decision.retry:
                        raise BackendError(
                            f"oMLX returned {resp.status_code}: {resp.text[:200]} "
                            f"({decision.reason})"
                        )
                    if on_retry:
                        on_retry(decision, attempt)
                    time.sleep(decision.delay_s)
                    attempt += 1
                    continue
                # Success path
                data = json.loads(raw_body)
                choice = data["choices"][0]
                msg = choice["message"]
                usage = data.get("usage", {})

                timing = GenerationTiming(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_s=wall,
                )

                tc_list: list[ToolCallResponse] = []
                for tc in msg.get("tool_calls") or []:
                    fn = tc["function"]
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    tc_list.append(ToolCallResponse(
                        id=tc.get("id", ""),
                        name=fn["name"],
                        arguments=args,
                    ))

                text = msg.get("content") or ""
                if not tc_list:
                    # Native channel empty — check the text channel (see
                    # recover_tool_calls_from_text above for why).
                    tc_list = recover_tool_calls_from_text(text, tools)
                    if tc_list:
                        text = ""

                return ChatResponse(
                    text=text,
                    tool_calls=tc_list,
                    finish_reason=choice.get("finish_reason", ""),
                    timing=timing,
                    retries=attempt,
                )
            except (httpx.HTTPError, OSError) as exc:
                decision = classify_failure(
                    exc=exc,
                    elapsed_since_start_s=time.monotonic() - request_t0,
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                )
                last_decision = decision
                logger.warning(
                    "backend %s exception=%s decision=%s",
                    self.model, type(exc).__name__, decision,
                )
                if not decision.retry:
                    raise BackendError(
                        f"oMLX call failed: {type(exc).__name__}: {exc} ({decision.reason})"
                    ) from exc
                if on_retry:
                    on_retry(decision, attempt)
                time.sleep(decision.delay_s)
                attempt += 1

        # Loop exhausted without success
        reason = last_decision.reason if last_decision else "unknown"
        raise BackendError(f"oMLX retries exhausted after {self.max_attempts} attempts ({reason})")

    def _read_body_with_progress(self, resp) -> bytes:
        """Accumulate a non-streamed response body, aborting if it stalls.

        oMLX pads a pending non-stream response with a bare space byte every
        ~10s under `Transfer-Encoding: chunked` (legal leading JSON
        whitespace, so the eventual parse still works). Those bytes keep
        httpx's read timeout from ever firing, so we time the gap between
        bytes that carry actual content instead.

        Raises `httpx.ReadTimeout` on stall so `classify_failure` treats it
        exactly like any other read timeout — transient, therefore retried,
        which is the right response when a model was swapped out underneath
        us and the retry will simply reload it.
        """
        if resp.is_stream_consumed:
            # Already materialised (e.g. httpx.MockTransport in tests, or any
            # transport that buffers eagerly). Nothing can trickle, so there is
            # nothing to time — and iter_raw() would raise StreamConsumed.
            return resp.content
        buf = bytearray()
        last_progress = time.monotonic()
        for raw in resp.iter_raw():
            now = time.monotonic()
            if raw.strip():           # anything that isn't padding is progress
                last_progress = now
            elif now - last_progress > self.stall_timeout_s:
                raise httpx.ReadTimeout(
                    f"progress stall: {now - last_progress:.0f}s of keepalive "
                    f"padding with no response content "
                    f"(stall_timeout_s={self.stall_timeout_s:.0f})"
                )
            buf += raw
        return bytes(buf)

    def _chat_stream(
        self,
        body: dict[str, Any],
        *,
        on_token: Callable[[str], None] | None = None,
        on_retry: Callable[[RetryDecision, int], None] | None = None,
    ) -> ChatResponse:
        """SSE streaming path (chat front-end only).

        Reconstructs the same `ChatResponse` shape the non-stream path returns,
        firing `on_token(delta)` for each text fragment so the REPL can render
        live. Retries only cover connection establishment (via classify_failure);
        once tokens flow, a mid-stream error aborts the turn — acceptable for an
        interactive front-end (the user just retypes). Never used by benchmarks.
        """
        # Ask oMLX to include a usage block in the terminal chunk.
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}

        attempt = 0
        last_decision: RetryDecision | None = None
        request_t0 = time.monotonic()

        while attempt < self.max_attempts:
            t0 = time.monotonic()
            text_parts: list[str] = []
            tool_frags: dict[int, dict[str, Any]] = {}
            finish_reason = ""
            usage: dict[str, Any] = {}
            ttft = 0.0
            started = False
            # Distinct from `started` (which pins ttft to the first *content*
            # token): tool-call fragments are progress too, and a tool-only
            # turn must still get the tighter decode bound.
            decoding = False
            try:
                with self._client.stream("POST", "/v1/chat/completions", json=body) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        decision = classify_failure(
                            status_code=resp.status_code,
                            body=resp.text,
                            elapsed_since_start_s=time.monotonic() - request_t0,
                            attempt=attempt,
                            max_attempts=self.max_attempts,
                        )
                        last_decision = decision
                        logger.warning(
                            "backend %s status=%d body=%r decision=%s",
                            self.model, resp.status_code, resp.text[:200],
                            decision,
                        )
                        if not decision.retry:
                            raise BackendError(
                                f"oMLX returned {resp.status_code}: {resp.text[:200]} "
                                f"({decision.reason})"
                            )
                        if on_retry:
                            on_retry(decision, attempt)
                        time.sleep(decision.delay_s)
                        attempt += 1
                        continue

                    last_progress = time.monotonic()
                    for line in resp.iter_lines():
                        # Checked BEFORE the blank-line skip, and before the
                        # keepalive chunks are discarded below: those are
                        # precisely the events that must not count as progress.
                        now = time.monotonic()
                        bound = (self.decode_stall_timeout_s if decoding
                                 else self.stall_timeout_s)
                        if now - last_progress > bound:
                            raise httpx.ReadTimeout(
                                f"progress stall: {now - last_progress:.0f}s of "
                                f"keepalive chunks with no "
                                f"{'tokens' if decoding else 'first token'} "
                                f"(bound={bound:.0f}s)"
                            )
                        if not line:
                            continue
                        if line.startswith("data: "):
                            line = line[len("data: "):]
                        line = line.strip()
                        if not line or line == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # Usage-only terminal chunk has empty choices.
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                            last_progress = time.monotonic()
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {}) or {}
                            piece = delta.get("content") or ""
                            if piece:
                                if not started:
                                    ttft = time.monotonic() - t0
                                    started = True
                                text_parts.append(piece)
                                last_progress = time.monotonic()
                                decoding = True
                                if on_token:
                                    on_token(piece)
                            for tc in delta.get("tool_calls") or []:
                                last_progress = time.monotonic()
                                decoding = True
                                idx = tc.get("index", 0)
                                slot = tool_frags.setdefault(
                                    idx, {"id": "", "name": "", "arguments": ""}
                                )
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] = fn["name"]
                                if fn.get("arguments"):
                                    slot["arguments"] += fn["arguments"]
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                                last_progress = time.monotonic()

                wall = time.monotonic() - t0
                tc_list: list[ToolCallResponse] = []
                for idx in sorted(tool_frags):
                    frag = tool_frags[idx]
                    raw = frag["arguments"] or "{}"
                    try:
                        args = json.loads(raw)
                    except json.JSONDecodeError:
                        args = {"_raw": raw}
                    tc_list.append(ToolCallResponse(
                        id=frag["id"], name=frag["name"], arguments=args,
                    ))

                text = "".join(text_parts)
                if not tc_list:
                    # Same text-channel salvage as the non-streaming path.
                    # The raw JSON already streamed to `on_token` (the UI
                    # showed it); what matters here is that the agent loop
                    # sees a call, not prose.
                    tc_list = recover_tool_calls_from_text(
                        text, body.get("tools"))
                    if tc_list:
                        text = ""

                return ChatResponse(
                    text=text,
                    tool_calls=tc_list,
                    finish_reason=finish_reason,
                    timing=GenerationTiming(
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                        total_s=wall,
                        time_to_first_token_s=ttft,
                    ),
                    retries=attempt,
                )
            except (httpx.HTTPError, OSError) as exc:
                decision = classify_failure(
                    exc=exc,
                    elapsed_since_start_s=time.monotonic() - request_t0,
                    attempt=attempt,
                    max_attempts=self.max_attempts,
                )
                last_decision = decision
                logger.warning(
                    "backend %s exception=%s decision=%s",
                    self.model, type(exc).__name__, decision,
                )
                if not decision.retry:
                    raise BackendError(
                        f"oMLX stream failed: {type(exc).__name__}: {exc} ({decision.reason})"
                    ) from exc
                if on_retry:
                    on_retry(decision, attempt)
                time.sleep(decision.delay_s)
                attempt += 1

        reason = last_decision.reason if last_decision else "unknown"
        raise BackendError(
            f"oMLX stream retries exhausted after {self.max_attempts} attempts ({reason})"
        )

    def health(self) -> bool:
        # OSError as well as httpx.HTTPError: a socket-level ETIMEDOUT (macOS
        # errno 60, e.g. a dropped Tailscale route) is an OSError and must read
        # as "unhealthy", not crash the caller.
        try:
            r = self._client.get("/v1/models")
            return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    def list_models(self) -> list[str]:
        r = self._client.get("/v1/models")
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]

    def assert_models_available(self, required: list[str]) -> list[str]:
        """Confirm all required model IDs resolve via /v1/models. Returns missing list."""
        available = set(self.list_models())
        return [m for m in required if m not in available]

    def loaded_models(self) -> list[str]:
        """Return the list of model IDs currently loaded in memory.

        Distinct from list_models() — that returns *available* models;
        this returns only those actually in RAM. Uses /v1/models/status,
        which oMLX exposes alongside the OpenAI-compatible /v1/models.
        """
        return [m.get("id", "") for m in self._models_status() if m.get("loaded")]

    def model_paths(self) -> dict[str, str]:
        """Map model id → the path oMLX loads its weights from.

        Powers the chat front-end's local-vs-network provenance flag
        (`chat/origin.py`): the path can be a symlink into the HF cache, a
        network mount, or a cloud-sync placeholder tree. Best-effort — an old
        or unreachable server yields `{}`.
        """
        return {m.get("id", ""): m.get("model_path", "")
                for m in self._models_status() if m.get("id")}

    def _models_status(self) -> list[dict[str, Any]]:
        """`/v1/models/status` model records, or [] when unavailable."""
        try:
            r = self._client.get("/v1/models/status")
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, OSError, ValueError):
            return []
        models = data.get("models", [])
        return models if isinstance(models, list) else []

    def unload_model(self, model_id: str) -> bool:
        """Free memory for one model. Returns True on success.

        oMLX endpoint: POST /v1/models/{model_id}/unload. Idempotent —
        unloading an already-unloaded model returns 200. Errors are
        swallowed (best-effort) since unload is a cleanup-time concern.
        """
        try:
            r = self._client.post(f"/v1/models/{model_id}/unload")
            return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    def unload_all_loaded(self, *, except_for: list[str] | None = None) -> dict[str, bool]:
        """Best-effort unload of every currently-loaded model. Returns a map
        of model_id → success. Models in `except_for` stay resident — useful
        for keeping a small "always-warm" set across runs.
        """
        keep = set(except_for or [])
        results: dict[str, bool] = {}
        for mid in self.loaded_models():
            if mid in keep:
                continue
            results[mid] = self.unload_model(mid)
        return results

    def thermal_guard(self, target_model: str, settle_s: float = 2.0,
                      max_wait_s: float = 30.0) -> bool:
        """Sleep briefly after a model swap and confirm the target is loaded.

        Returns True if the target is reported loaded within max_wait_s,
        False if the wait timed out (caller may proceed; the subsequent chat
        will retry on transient failures regardless).
        """
        time.sleep(settle_s)
        deadline = time.monotonic() + max_wait_s
        while time.monotonic() < deadline:
            try:
                if target_model in set(self.list_models()):
                    return True
            except (httpx.HTTPError, OSError):
                pass
            time.sleep(1.0)
        return False
