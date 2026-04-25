"""LM Studio Phase-0 healthcheck for the overnight test plan.

Detects the LM Studio server, probes auth requirements, lists loaded
models, and verifies chat completion + (best-effort) speculative-
decoding/draft-model API surface. Mirrors `omlx_healthcheck.py`'s
fail-fast structure so a missing prereq aborts the suite cleanly.

Usage:

    uv run python scripts/lmstudio_healthcheck.py
    uv run python scripts/lmstudio_healthcheck.py --base-url http://127.0.0.1:1234
    uv run python scripts/lmstudio_healthcheck.py --skip-models

Exit code 0 = all checks passed; 1 = any check failed.

Environment knobs:
- LMSTUDIO_PORT       — override the assumed port (default 1234)
- LMSTUDIO_API_KEY    — bearer token if your LM Studio requires auth
                        (newer builds may; older ones don't)

Unlike `omlx_healthcheck`, this script does NOT auto-install LM Studio.
It's a desktop app — install path varies (.dmg drag, brew cask, App
Store). If the server isn't reachable, the script tells you what to
install + start, and exits non-zero.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

import httpx
import typer

DEFAULT_PORT = int(os.environ.get("LMSTUDIO_PORT", "1234"))
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_PORT}"
ENDPOINT_TIMEOUT_S = 30.0
HELLO_PROMPT = "Reply with the single word: ok"
API_KEY = os.environ.get("LMSTUDIO_API_KEY", "")
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

# Models the overnight test plan expects to find loaded. Token-based
# fuzzy matching tolerates LM Studio's id-naming differences (publisher
# prefix, alias suffixes, etc.).
REQUIRED_MODELS = (
    {
        "candidate_id": "qwen2.5-coder-14b",
        "match_tokens": ["Qwen2.5", "Coder", "14B", "Instruct"],
    },
    {
        "candidate_id": "qwen2.5-32b-instruct",
        "match_tokens": ["Qwen2.5", "32B", "Instruct"],
    },
)


@dataclass
class StepResult:
    name: str
    passed: bool
    detail: str = ""


# ── steps ────────────────────────────────────────────────────────────


def _step_detect_endpoint(base_url: str) -> StepResult:
    """True if LM Studio's server is responding. 401 also counts as
    'up' — auth check is the next step."""
    try:
        r = httpx.get(f"{base_url}/v1/models", headers=AUTH_HEADERS, timeout=3.0)
        if r.status_code < 500:
            return StepResult("detect", True, f"endpoint up at {base_url} (status={r.status_code})")
    except Exception as e:  # noqa: BLE001
        return StepResult(
            "detect", False,
            f"no endpoint at {base_url}: {type(e).__name__}: {e}. "
            f"Install LM Studio (https://lmstudio.ai/), launch it, and "
            f"start the local server from the app's Server tab. Or run "
            f"`lms server start` if you have the CLI."
        )
    return StepResult("detect", False, f"endpoint at {base_url} returned {r.status_code}")


def _resolve_required_model(spec: dict, listed: list[str]) -> str | None:
    tokens = spec.get("match_tokens") or []
    for m in listed:
        if all(t.lower() in (m or "").lower() for t in tokens):
            return m
    return None


def _step_models_present(base_url: str) -> tuple[StepResult, list[str], dict[str, str]]:
    """List loaded models; report missing required ones."""
    try:
        r = httpx.get(f"{base_url}/v1/models", headers=AUTH_HEADERS, timeout=10.0)
        if r.status_code == 401:
            return (
                StepResult("models", False,
                    "401 Unauthorized — set LMSTUDIO_API_KEY to the key "
                    "from LM Studio's Server settings, then re-run."),
                [], {},
            )
        r.raise_for_status()
        listed = [m.get("id") or m.get("model") for m in (r.json().get("data") or [])]
        listed = [m for m in listed if m]
    except Exception as e:  # noqa: BLE001
        return StepResult("models", False, f"/v1/models failed: {e}"), [], {}

    missing = []
    resolved = {}
    for spec in REQUIRED_MODELS:
        match = _resolve_required_model(spec, listed)
        if match:
            resolved[spec["candidate_id"]] = match
        else:
            missing.append(spec["candidate_id"])

    if not missing:
        return (
            StepResult("models", True,
                f"all {len(REQUIRED_MODELS)} required models present "
                f"(resolved: {resolved})"),
            listed, resolved,
        )
    return (
        StepResult(
            "models", False,
            f"missing models: {missing}. Listed: {listed}. "
            "Pull via `lms get <repo>` (CLI) or LM Studio's model "
            "browser tab."
        ),
        listed, resolved,
    )


def _step_chat_completion(base_url: str, resolved: dict[str, str]) -> StepResult:
    """Round-trip chat to confirm the resolved model actually serves."""
    model = next(iter(resolved.values()), None) if resolved else None
    if not model:
        return StepResult("chat", False, "no resolved model to chat with")
    try:
        r = httpx.post(
            f"{base_url}/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={
                "model": model,
                "messages": [{"role": "user", "content": HELLO_PROMPT}],
                "max_tokens": 8,
                "temperature": 0,
                "stream": False,
            },
            timeout=120.0,
        )
        r.raise_for_status()
        body = r.json()
        text = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not text:
            return StepResult("chat", False, "/v1/chat/completions returned empty content")
        return StepResult("chat", True, f"/v1/chat/completions ({model}): {text[:60]!r}")
    except Exception as e:  # noqa: BLE001
        return StepResult("chat", False, f"/v1/chat/completions failed: {e}")


def _step_probe_speculative(base_url: str, resolved: dict[str, str]) -> StepResult:
    """Best-effort probe for speculative-decoding / draft-model API.

    LM Studio's docs are sparse; some builds expose `draft_model` as a
    chat-completion request field, others require server-side config.
    Send a request with `draft_model` and `speculative_n` set; if the
    server accepts (200 with sensible response), spec decoding is
    available via per-request fields. Otherwise log the failure mode
    so the test plan knows whether to skip the LM Studio +spec phase."""
    model = next(iter(resolved.values()), None) if resolved else None
    if not model:
        return StepResult("spec", True, "skipped (no model resolved)")
    try:
        r = httpx.post(
            f"{base_url}/v1/chat/completions",
            headers=AUTH_HEADERS,
            json={
                "model": model,
                "messages": [{"role": "user", "content": HELLO_PROMPT}],
                "max_tokens": 4,
                "temperature": 0,
                "stream": False,
                # Speculative-decoding hints — non-standard fields,
                # silently ignored by servers that don't support them.
                "draft_model": model,
                "speculative_n": 3,
            },
            timeout=60.0,
        )
        if r.status_code == 200:
            return StepResult(
                "spec", True,
                "/v1/chat/completions accepted draft_model field "
                "(spec decoding probably available per-request)"
            )
        return StepResult(
            "spec", True,
            f"/v1/chat/completions rejected draft_model (status {r.status_code}); "
            f"spec decoding NOT available via per-request field. "
            f"LM Studio +spec phase will be skipped."
        )
    except Exception as e:  # noqa: BLE001
        return StepResult("spec", True, f"spec probe failed: {e} (assumed unsupported)")


# ── orchestration ────────────────────────────────────────────────────


def _print_summary(results: list[StepResult]) -> bool:
    typer.echo("")
    typer.echo("=== LM Studio Phase-0 healthcheck summary ===")
    all_ok = True
    for r in results:
        mark = "✓" if r.passed else "✗"
        typer.echo(f"  {mark} {r.name:10s}  {r.detail}")
        if not r.passed:
            all_ok = False
    typer.echo("")
    typer.echo("RESULT: " + ("PASS" if all_ok else "FAIL"))
    return all_ok


def main(
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
    skip_models: bool = typer.Option(False, "--skip-models"),
    skip_spec: bool = typer.Option(False, "--skip-spec"),
) -> None:
    results: list[StepResult] = []
    detect = _step_detect_endpoint(base_url)
    results.append(detect)

    resolved: dict[str, str] = {}
    if detect.passed:
        if not skip_models:
            models_step, _, resolved = _step_models_present(base_url)
            results.append(models_step)
        else:
            try:
                r = httpx.get(f"{base_url}/v1/models", headers=AUTH_HEADERS, timeout=5.0)
                r.raise_for_status()
                listed = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
                for spec in REQUIRED_MODELS:
                    match = _resolve_required_model(spec, listed)
                    if match:
                        resolved[spec["candidate_id"]] = match
            except Exception:  # noqa: BLE001
                pass
        results.append(_step_chat_completion(base_url, resolved))
        if not skip_spec:
            results.append(_step_probe_speculative(base_url, resolved))

    ok = _print_summary(results)
    if not API_KEY:
        typer.echo("\n[hint] LMSTUDIO_API_KEY env var is unset. If you saw 401, set it.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    typer.run(main)
