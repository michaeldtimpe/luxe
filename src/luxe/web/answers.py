"""Grounded answers via Brave Answers — a separate product from web search.

Answers is Brave's OpenAI-compatible chat-completions endpoint: it runs its
own live searches server-side and returns ONE synthesized, grounded answer.
That is a different feature from `web_search` (ranked links you then read
yourself with `web_fetch`), on a different subscription with different
billing — so it is keyed SEPARATELY (`BRAVE_ANSWERS_API_KEY`) and the
`web_answer` tool is withheld unless that key resolves, exactly like
`web_search`'s gating. Both are `/web`-gated (web.sdd).

Key resolution follows the same rule as search.py: env →
~/.luxe/secrets.env → login Keychain, by env-var NAME only.
"""

from __future__ import annotations

ANSWERS_ENV = "BRAVE_ANSWERS_API_KEY"
ANSWERS_URL = "https://api.search.brave.com/res/v1/chat/completions"
SIGNUP = "https://brave.com/search/api/"
# Answers does live search + generation server-side; a deep query can run
# well past a search's 15s. Blocking (non-stream) responses only — the
# streaming/citations variant is a different response shape.
ANSWER_TIMEOUT_S = 120.0
MODELS = ("brave", "brave-pro")


def _key() -> str:
    from luxe.secrets import resolve_api_key
    try:
        return resolve_api_key(ANSWERS_ENV) or ""
    except Exception:
        return ""


def configured() -> bool:
    return bool(_key())


def missing_key_message() -> str:
    return (f"no Brave Answers API key found. luxe looks for {ANSWERS_ENV} "
            f"({SIGNUP} — the Answers plan, a separate subscription from web "
            "search) via env → ~/.luxe/secrets.env → Keychain. Add it to "
            "~/.luxe/secrets.env (the NAME goes in config, never the value).")


def answer(query: str, *, model: str = "") -> str:
    """One grounded answer for `query`. Raises WebError when unusable."""
    import httpx

    from luxe.web.fetch import WebError

    query = (query or "").strip()
    if not query:
        raise WebError("empty question")
    key = _key()
    if not key:
        raise WebError(missing_key_message())

    body: dict = {"stream": False,
                  "messages": [{"role": "user", "content": query}]}
    if model:
        if model not in MODELS:
            raise WebError(f"unknown answers model {model!r} "
                           f"(supported: {', '.join(MODELS)})")
        body["model"] = model
    try:
        r = httpx.post(
            ANSWERS_URL,
            json=body,
            headers={"Accept": "application/json",
                     "x-subscription-token": key},
            timeout=ANSWER_TIMEOUT_S,
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            raise WebError(
                f"brave answers rejected the API key in {ANSWERS_ENV} "
                f"(HTTP {code}) — check it is current and on the Answers "
                "plan (a separate subscription from web search)") from e
        if code == 402:
            raise WebError("brave answers: payment required (HTTP 402) — "
                           "the Answers subscription is out of quota") from e
        raise WebError(f"brave answers failed: HTTP {code}") from e
    except Exception as e:
        raise WebError(
            f"brave answers failed: {type(e).__name__}: {e}") from e

    try:
        content = (data.get("choices") or [{}])[0].get("message", {}) \
            .get("content", "")
    except AttributeError:
        content = ""
    if not (content or "").strip():
        raise WebError("brave answers returned an empty answer — retry, or "
                       "use web_search + web_fetch to research it yourself")
    used = str(data.get("model") or "brave")
    return f"{content.strip()}\n\n(answered by {used} — grounded in live " \
           "web search; use web_search + web_fetch to verify sources yourself)"
