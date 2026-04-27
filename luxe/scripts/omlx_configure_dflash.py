"""Configure DFlash (oMLX scheduler-level speculative decoding) on a
target model via PUT /admin/api/models/{id}/settings.

Usage:

    # Show current setting for the 14B coder
    uv run python scripts/omlx_configure_dflash.py show \\
        --target Qwen2.5-Coder-14B-Instruct-MLX-4bit

    # Enable DFlash with the 0.5B draft (4-bit)
    uv run python scripts/omlx_configure_dflash.py enable \\
        --target Qwen2.5-Coder-14B-Instruct-MLX-4bit \\
        --draft Qwen2.5-Coder-0.5B-Instruct-MLX-4bit \\
        --quant-bits 4

    # Disable
    uv run python scripts/omlx_configure_dflash.py disable \\
        --target Qwen2.5-Coder-14B-Instruct-MLX-4bit

    # Pull a draft model first if missing
    uv run python scripts/omlx_configure_dflash.py pull-draft \\
        --repo mlx-community/Qwen2.5-Coder-0.5B-Instruct-4bit

OMLX_API_KEY env var must be set for all commands.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time

import httpx
import typer

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
app = typer.Typer(help=__doc__)


def _api_key() -> str:
    key = os.environ.get("OMLX_API_KEY", "").strip()
    if not key:
        typer.echo("ERROR: OMLX_API_KEY env var not set.", err=True)
        sys.exit(1)
    return key


@contextlib.contextmanager
def _admin_client(base_url: str):
    """Cookie-authenticated httpx client for /admin/api/*. The
    bearer token works on /v1/* but admin endpoints require a
    session cookie obtained from POST /admin/api/login."""
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post("/admin/api/login", json={"api_key": _api_key()})
        r.raise_for_status()
        if not (r.json() or {}).get("success"):
            typer.echo(f"ERROR: admin login failed: {r.text}", err=True)
            sys.exit(1)
        yield client


def _list_models(client: httpx.Client) -> list[dict]:
    r = client.get("/admin/api/models")
    r.raise_for_status()
    body = r.json() or {}
    if isinstance(body, dict):
        return body.get("models") or body.get("data") or []
    return body if isinstance(body, list) else []


def _fetch_settings(client: httpx.Client, model_id: str) -> dict:
    """Pull the current model entry for `model_id`. The model dict
    includes flattened settings (dflash_draft_model etc.) when
    populated."""
    models = _list_models(client)
    for m in models:
        if (m.get("id") or m.get("model_id") or m.get("name")) == model_id:
            return m
    listed = [m.get("id") or m.get("model_id") or m.get("name") for m in models]
    typer.echo(
        f"ERROR: model {model_id!r} not found. Loaded ids: {listed}",
        err=True,
    )
    sys.exit(2)


def _put_settings(client: httpx.Client, model_id: str, payload: dict) -> dict:
    r = client.put(f"/admin/api/models/{model_id}/settings", json=payload)
    r.raise_for_status()
    return r.json() if r.content else {}


_RELEVANT_FIELDS = (
    "dflash_enabled", "dflash_draft_model", "dflash_draft_quant_bits",
    "specprefill_enabled", "specprefill_draft_model",
    "specprefill_keep_pct", "specprefill_threshold",
)


def _print_relevant(info: dict) -> None:
    # The actual settings live under info["settings"] in the model
    # listing response, but the PUT /settings endpoint accepts the
    # flat shape. Read the nested dict for display.
    src = info.get("settings") or info
    relevant = {k: src.get(k) for k in _RELEVANT_FIELDS}
    typer.echo(json.dumps(relevant, indent=2))


@app.command()
def show(
    target: str = typer.Option(..., "--target"),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
) -> None:
    """Print the DFlash + specprefill subset of `target`'s settings."""
    with _admin_client(base_url) as c:
        info = _fetch_settings(c, target)
        _print_relevant(info)


@app.command(name="list-models")
def list_models_cmd(
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
) -> None:
    """Show every model id oMLX has loaded — handy for picking the
    --target / --draft strings."""
    with _admin_client(base_url) as c:
        for m in _list_models(c):
            mid = m.get("id") or m.get("model_id") or m.get("name")
            size = m.get("estimated_size_formatted") or "?"
            loaded = "loaded" if m.get("loaded") else "unloaded"
            typer.echo(f"  {mid}  ({size}, {loaded})")


@app.command()
def enable(
    target: str = typer.Option(..., "--target"),
    draft: str = typer.Option(..., "--draft"),
    quant_bits: int = typer.Option(4, "--quant-bits"),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
) -> None:
    """Set dflash_enabled + dflash_draft_model + dflash_draft_quant_bits
    on `target`. All three are required — without dflash_enabled=true
    oMLX silently keeps the configured draft but doesn't actually use
    it during decode."""
    payload = {
        "dflash_enabled": True,
        "dflash_draft_model": draft,
        "dflash_draft_quant_bits": quant_bits,
    }
    typer.echo(f"PUT settings on {target}: {payload}")
    with _admin_client(base_url) as c:
        _put_settings(c, target, payload)
        _print_relevant(_fetch_settings(c, target))


@app.command()
def disable(
    target: str = typer.Option(..., "--target"),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
) -> None:
    """Clear DFlash on `target` (sets dflash_enabled to false)."""
    typer.echo(f"clearing DFlash on {target}")
    with _admin_client(base_url) as c:
        _put_settings(
            c, target, {
                "dflash_enabled": False,
                "dflash_draft_model": None,
                "dflash_draft_quant_bits": None,
            }
        )
        _print_relevant(_fetch_settings(c, target))


@app.command("pull-draft")
def pull_draft(
    repo: str = typer.Option(..., "--repo"),
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url"),
    wait: bool = typer.Option(True, "--wait/--no-wait",
        help="Poll /admin/api/hf/tasks until the download finishes."),
    poll_s: float = typer.Option(5.0, "--poll-s"),
) -> None:
    """POST /admin/api/hf/download with the given repo_id, optionally
    polling until done."""
    typer.echo(f"requesting download of {repo}")
    with _admin_client(base_url) as c:
        r = c.post("/admin/api/hf/download", json={"repo_id": repo})
        r.raise_for_status()
        body = r.json() if r.content else {}
        # oMLX returns {"success": true, "task": {"task_id": "...", ...}}
        # Older builds may inline task_id at top level — check both.
        task_block = body.get("task") if isinstance(body.get("task"), dict) else {}
        task_id = (
            task_block.get("task_id") or task_block.get("id")
            or body.get("task_id") or body.get("id")
        )
        typer.echo(f"  download started: task_id={task_id}, response={body}")
        if not wait or not task_id:
            return
        while True:
            time.sleep(poll_s)
            try:
                tr = c.get("/admin/api/hf/tasks")
                tr.raise_for_status()
                tasks = tr.json() or []
                if isinstance(tasks, dict):
                    tasks = tasks.get("data", []) or list(tasks.values())
                mine = next(
                    (t for t in tasks if (t.get("id") or t.get("task_id")) == task_id),
                    None,
                )
            except Exception as e:  # noqa: BLE001
                typer.echo(f"  poll failed: {e} (retrying)")
                continue
            if not mine:
                typer.echo(f"  task {task_id} no longer in /tasks — assuming done")
                return
            status = mine.get("status") or mine.get("state") or "?"
            progress = mine.get("progress") or mine.get("percent") or "?"
            typer.echo(f"  status={status} progress={progress}")
            if str(status).lower() in ("done", "completed", "success", "finished"):
                typer.echo("  download complete.")
                return
            if str(status).lower() in ("failed", "error", "cancelled"):
                typer.echo(f"  download failed: {mine}")
                sys.exit(3)


if __name__ == "__main__":
    app()
