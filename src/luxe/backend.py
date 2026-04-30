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
        timeout_s: float = 600.0,
        api_key: str = "",
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        # Pick api_key from arg first, then OMLX_API_KEY env. Many oMLX
        # deployments require auth; without a key, every chat call 401s.
        import os as _os
        if not api_key:
            api_key = _os.environ.get("OMLX_API_KEY", "")
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
        on_retry: Callable[[RetryDecision, int], None] | None = None,
    ) -> ChatResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if num_ctx is not None:
            body.setdefault("extra_body", {})["num_ctx"] = num_ctx
        if repeat_penalty is not None:
            body.setdefault("extra_body", {})["repeat_penalty"] = repeat_penalty

        attempt = 0
        last_decision: RetryDecision | None = None
        request_t0 = time.monotonic()

        while attempt < self.max_attempts:
            t0 = time.monotonic()
            try:
                resp = self._client.post("/v1/chat/completions", json=body)
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
                data = resp.json()
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

                return ChatResponse(
                    text=msg.get("content") or "",
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

    def health(self) -> bool:
        try:
            r = self._client.get("/v1/models")
            return r.status_code == 200
        except httpx.HTTPError:
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
        try:
            r = self._client.get("/v1/models/status")
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return []
        return [m.get("id", "") for m in data.get("models", []) if m.get("loaded")]

    def unload_model(self, model_id: str) -> bool:
        """Free memory for one model. Returns True on success.

        oMLX endpoint: POST /v1/models/{model_id}/unload. Idempotent —
        unloading an already-unloaded model returns 200. Errors are
        swallowed (best-effort) since unload is a cleanup-time concern.
        """
        try:
            r = self._client.post(f"/v1/models/{model_id}/unload")
            return r.status_code == 200
        except httpx.HTTPError:
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
            except httpx.HTTPError:
                pass
            time.sleep(1.0)
        return False
