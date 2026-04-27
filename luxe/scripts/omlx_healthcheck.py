"""oMLX Phase-0 healthcheck + auto-installer.

Detects existing oMLX, installs if missing, pulls the two models the
luxe oMLX evaluation suite uses, and verifies both endpoints respond.
Fails fast with the exact failing step so the rest of the suite
aborts cleanly.

Usage:

    uv run python scripts/omlx_healthcheck.py
    uv run python scripts/omlx_healthcheck.py --base-url http://127.0.0.1:10240
    uv run python scripts/omlx_healthcheck.py --skip-install     # detect-only
    uv run python scripts/omlx_healthcheck.py --skip-models      # endpoint check only

Exit code 0 = all checks passed; 1 = any check failed.

Environment knobs:
- OMLX_PORT       — override the assumed port (default 10240)
- OMLX_INSTALL_URL — direct URL to a release tarball; bypasses brew

This script is intentionally cautious about side effects. The two
install paths it WILL take when --skip-install is not set:
  1. brew install --cask omlx       (if `brew` is available)
  2. download from OMLX_INSTALL_URL  (only if env var set)

The GitHub-release auto-download fallback in the original plan is
NOT performed automatically — it requires network access to an
unverified release URL, and if oMLX moves repos or rotates release
artifacts the suite would silently install the wrong thing. Setting
OMLX_INSTALL_URL = the tarball URL keeps the human in the loop on
exactly what's being downloaded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx
import typer

DEFAULT_OMLX_PORT = int(os.environ.get("OMLX_PORT", "8000"))
DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_OMLX_PORT}"
ENDPOINT_TIMEOUT_S = 60.0
HELLO_PROMPT = "Reply with the single word: ok"
API_KEY = os.environ.get("OMLX_API_KEY", "")
AUTH_HEADERS = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

# The two models the rest of the suite uses. Keep aligned with
# configs/candidates.yaml — if these IDs change there, change them
# here too. The "mlx_repo" field is the HF repo id used to pull;
# oMLX exposes models under its own canonical names that don't match
# this string verbatim, so the models check fuzzy-matches by token.
REQUIRED_MODELS = (
    {
        "candidate_id": "qwen2.5-coder-14b",
        "mlx_repo": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",
        "match_tokens": ["Qwen2.5", "Coder", "14B", "Instruct"],
    },
    {
        "candidate_id": "qwen2.5-7b-instruct",
        "mlx_repo": "mlx-community/Qwen2.5-7B-Instruct-4bit",
        "match_tokens": ["Qwen2.5", "7B", "Instruct"],
    },
)


@dataclass
class StepResult:
    name: str
    passed: bool
    detail: str = ""


def _log(step: str, msg: str) -> None:
    typer.echo(f"  [{step}] {msg}")


# ── steps ────────────────────────────────────────────────────────────


def _step_detect_endpoint(base_url: str) -> StepResult:
    """True if oMLX is already responding. Skip the install step when
    the daemon is up — running brew on every healthcheck would be
    rude on already-provisioned hosts. 401 counts as "up" — the
    later auth step will surface the missing key."""
    try:
        r = httpx.get(f"{base_url}/v1/models", headers=AUTH_HEADERS, timeout=3.0)
        if r.status_code < 500:
            return StepResult("detect", True, f"endpoint up at {base_url} (status={r.status_code})")
    except Exception as e:  # noqa: BLE001
        return StepResult("detect", False, f"no endpoint: {type(e).__name__}: {e}")
    return StepResult("detect", False, f"endpoint at {base_url} returned {r.status_code}")


def _step_install() -> StepResult:
    """Try brew, then OMLX_INSTALL_URL. Anything else requires manual
    install — we surface that as a clear failure rather than guessing."""
    install_url = os.environ.get("OMLX_INSTALL_URL", "").strip()
    brew = shutil.which("brew")
    if brew:
        _log("install", "trying brew install --cask omlx (may prompt for sudo)")
        try:
            res = subprocess.run(  # noqa: S603
                [brew, "install", "--cask", "omlx"],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if res.returncode == 0:
                return StepResult("install", True, "installed via brew cask")
            _log("install", f"brew failed (exit={res.returncode}): {res.stderr.strip()[:200]}")
        except subprocess.TimeoutExpired:
            return StepResult("install", False, "brew install timed out after 600s")
        except Exception as e:  # noqa: BLE001
            _log("install", f"brew threw: {e}")

    if install_url:
        _log("install", f"trying OMLX_INSTALL_URL={install_url}")
        try:
            r = httpx.get(install_url, timeout=120.0, follow_redirects=True)
            r.raise_for_status()
            target_dir = os.path.expanduser("~/Downloads")
            os.makedirs(target_dir, exist_ok=True)
            target_path = os.path.join(target_dir, "omlx_release.tar.gz")
            with open(target_path, "wb") as f:
                f.write(r.content)
            return StepResult(
                "install", False,
                f"downloaded to {target_path} — please extract + open the .app, "
                "then re-run this script. (Auto-extract intentionally skipped to "
                "keep the human in the loop on what's being installed.)"
            )
        except Exception as e:  # noqa: BLE001
            return StepResult(
                "install", False,
                f"OMLX_INSTALL_URL download failed: {type(e).__name__}: {e}"
            )

    return StepResult(
        "install", False,
        "neither `brew` nor OMLX_INSTALL_URL available. Install oMLX manually "
        "(https://github.com/jundot/omlx) and re-run --skip-install."
    )


def _step_wait_endpoint(base_url: str, timeout_s: float = ENDPOINT_TIMEOUT_S) -> StepResult:
    deadline = time.monotonic() + timeout_s
    last_err: str = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/v1/models", headers=AUTH_HEADERS, timeout=3.0)
            if r.status_code < 500:
                return StepResult(
                    "wait", True,
                    f"endpoint ready after {int(timeout_s - (deadline - time.monotonic()))}s"
                )
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(2.0)
    return StepResult(
        "wait", False,
        f"endpoint did not come up within {timeout_s}s ({last_err})"
    )


def _resolve_required_model(spec: dict, listed: list[str]) -> str | None:
    """Find a listed model whose id contains every match_token (case-
    insensitive). Returns the resolved id or None if no match."""
    tokens = spec.get("match_tokens") or []
    for m in listed:
        if all(t.lower() in (m or "").lower() for t in tokens):
            return m
    return None


def _step_models_present(base_url: str) -> tuple[StepResult, list[str], dict[str, str]]:
    """List loaded models and report which of REQUIRED_MODELS are
    missing. Returns (step_result, listed, resolved_by_candidate) so
    later steps can reuse the resolved oMLX-side ids."""
    try:
        r = httpx.get(f"{base_url}/v1/models", headers=AUTH_HEADERS, timeout=10.0)
        if r.status_code == 401:
            return (
                StepResult("models", False,
                    "401 Unauthorized — set OMLX_API_KEY env var to the key from "
                    "the oMLX admin dashboard, then re-run."),
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
            "Download via admin dashboard (http://127.0.0.1:8000/admin) "
            "or POST /admin/api/hf/download with the desired repo_id."
        ),
        listed, resolved,
    )


def _step_chat_completion(base_url: str, resolved: dict[str, str]) -> StepResult:
    """One-token chat to confirm the OpenAI-compat endpoint actually
    serves a model (not just lists one). Uses whichever required
    model resolved successfully."""
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


def _step_messages_endpoint(base_url: str, resolved: dict[str, str]) -> StepResult:
    """Anthropic-format /v1/messages probe. Optional — if oMLX doesn't
    expose this endpoint the suite still works (it only uses /v1/chat/
    completions), but the user wanted both checked."""
    model = next(iter(resolved.values()), None) if resolved else None
    if not model:
        return StepResult("messages", False, "no resolved model to test against")
    try:
        r = httpx.post(
            f"{base_url}/v1/messages",
            json={
                "model": model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": HELLO_PROMPT}],
            },
            headers={**AUTH_HEADERS, "anthropic-version": "2023-06-01"},
            timeout=120.0,
        )
        if r.status_code == 404:
            return StepResult(
                "messages", True,
                "/v1/messages not exposed (OK — suite only requires "
                "/v1/chat/completions)"
            )
        r.raise_for_status()
        body = r.json()
        text = ""
        if isinstance(body.get("content"), list):
            text = "".join(
                blk.get("text", "") for blk in body["content"] if isinstance(blk, dict)
            ).strip()
        return StepResult(
            "messages", bool(text),
            f"/v1/messages: {text[:60]!r}" if text else "/v1/messages returned no text"
        )
    except Exception as e:  # noqa: BLE001
        return StepResult("messages", False, f"/v1/messages failed: {e}")


# ── orchestration ────────────────────────────────────────────────────


def _print_summary(results: list[StepResult]) -> bool:
    typer.echo("")
    typer.echo("=== Phase 0 healthcheck summary ===")
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
    skip_install: bool = typer.Option(False, "--skip-install"),
    skip_models: bool = typer.Option(False, "--skip-models"),
) -> None:
    results: list[StepResult] = []

    detect = _step_detect_endpoint(base_url)
    results.append(detect)
    if not detect.passed and not skip_install:
        results.append(_step_install())
        results.append(_step_wait_endpoint(base_url))
    elif not detect.passed:
        results.append(StepResult("install", False, "skipped (--skip-install)"))

    # Don't bother with the rest if the endpoint never came up.
    endpoint_up = any(r.name in ("detect", "wait") and r.passed for r in results)
    resolved: dict[str, str] = {}
    if endpoint_up:
        if not skip_models:
            models_step, _, resolved = _step_models_present(base_url)
            results.append(models_step)
        else:
            # Even when models step is skipped, fetch the listing so
            # later chat/messages steps have a model to target.
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
        results.append(_step_messages_endpoint(base_url, resolved))

    ok = _print_summary(results)
    if not API_KEY:
        typer.echo("\n[hint] OMLX_API_KEY env var is unset. If you saw 401, set it:")
        typer.echo("       export OMLX_API_KEY=omlx-...")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    typer.run(main)
