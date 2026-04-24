"""Launch and manage a local model server (mlx-lm or llama.cpp).

The harness owns server lifecycle so every Phase-A/B/D run pins the exact
quant, KV, speculative-decoding, and prompt-cache settings we intend to
measure. Enter as a context manager; exit kills the process group.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import psutil
from rich.console import Console

from harness.backends import Backend, BackendKind
from harness.registry import Candidate, DraftModel, OptimizationConfig

_console = Console()


class ServerError(RuntimeError):
    pass


def _free_port(preferred: int = 8088) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _wait_ready(
    port: int,
    timeout_s: float = 300.0,
    proc: subprocess.Popen | None = None,
    log_path: Path | None = None,
) -> None:
    """Block until the OpenAI-compat server answers /v1/models. If the
    subprocess we're polling for died (e.g. llama-server bailed because
    the model path was wrong), bail immediately with the tail of its
    log so the caller doesn't sit through the full timeout."""
    url = f"http://127.0.0.1:{port}/v1/models"
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    elapsed_s = 0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=5.0)
            if r.status_code < 500:
                _console.log(f"[green]server ready[/] on :{port} after {elapsed_s}s")
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
        if proc is not None and proc.poll() is not None:
            tail = ""
            if log_path and log_path.exists():
                lines = log_path.read_text().splitlines()[-15:]
                tail = "\n  | " + "\n  | ".join(lines)
            raise ServerError(
                f"server on :{port} exited {proc.returncode} before ready"
                f"{tail}"
            )
        time.sleep(1.0)
        elapsed_s += 1
        if elapsed_s % 15 == 0:
            _console.log(f"waiting for server on :{port}… {elapsed_s}s elapsed")
    raise ServerError(f"server did not become ready on port {port}: {last_err}")


def _prefetch_weights(repo: str) -> None:
    """Download weights from HF ahead of server launch so progress is visible.

    Uses huggingface_hub's snapshot_download, which prints per-file progress
    bars to the terminal by default. Cached → returns instantly.
    """
    from huggingface_hub import snapshot_download

    _console.log(f"prefetching weights: [bold]{repo}[/] (cached downloads are no-ops)")
    snapshot_download(repo_id=repo, resume_download=True)


def _mlx_args(
    candidate: Candidate,
    config: OptimizationConfig,
    draft: DraftModel | None,
    port: int,
) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "mlx_lm.server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--model",
        candidate.mlx_repo or candidate.hf_repo,
    ]
    if config.kv_quant == "q8":
        args += ["--kv-bits", "8", "--kv-group-size", "64"]
    elif config.kv_quant == "q4":
        args += ["--kv-bits", "4", "--kv-group-size", "64"]
    if config.spec_decoding and draft:
        args += [
            "--draft-model",
            draft.mlx_repo or draft.hf_repo,
            "--num-draft-tokens",
            str(config.spec_draft_tokens),
        ]
    if config.prompt_cache:
        args += ["--use-default-chat-template"]
    return args


def _resolve_gguf_path(candidate: Candidate, models_dir: Path) -> Path:
    """Find the on-disk GGUF for `candidate`, checking the legacy
    models/<id>/<file> path first (for hand-placed weights), then the
    HuggingFace hub cache that hf_hub_download / snapshot_download
    write into."""
    if not candidate.gguf_file:
        raise ServerError(
            f"candidate {candidate.id} has no gguf_file; cannot run on llamacpp"
        )
    legacy = models_dir / candidate.id / candidate.gguf_file
    if legacy.exists():
        return legacy
    if candidate.gguf_repo:
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache(
            repo_id=candidate.gguf_repo, filename=candidate.gguf_file
        )
        if cached and Path(str(cached)).exists():
            return Path(str(cached))
    raise ServerError(
        f"GGUF for {candidate.id} not found. Tried:\n"
        f"  - {legacy}\n"
        f"  - HF cache for {candidate.gguf_repo}/{candidate.gguf_file}\n"
        f"Run `python scripts/run_ab_full.py` (no --skip-prefetch) to "
        f"download, or place the file at the legacy path manually."
    )


def _llamacpp_args(
    candidate: Candidate,
    config: OptimizationConfig,
    draft: DraftModel | None,
    port: int,
    models_dir: Path,
) -> list[str]:
    gguf_path = _resolve_gguf_path(candidate, models_dir)
    args = [
        "llama-server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--model",
        str(gguf_path),
        "--ctx-size",
        str(candidate.context_target),
        "--n-gpu-layers",
        "999",
    ]
    if config.kv_quant == "q8":
        args += ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
    elif config.kv_quant == "q4":
        args += ["--cache-type-k", "q4_0", "--cache-type-v", "q4_0"]
    if config.spec_decoding and draft:
        draft_file = models_dir / draft.id / "draft.gguf"
        args += [
            "--model-draft",
            str(draft_file),
            "--draft-max",
            str(config.spec_draft_tokens),
        ]
    if config.prompt_cache:
        args += ["--prompt-cache", "--prompt-cache-all"]
    return args


@contextmanager
def launch_server(
    *,
    kind: BackendKind,
    candidate: Candidate,
    config: OptimizationConfig,
    draft: DraftModel | None = None,
    models_dir: Path = Path("models"),
    port: int | None = None,
    log_dir: Path = Path("results/server_logs"),
    ollama_base_url: str = "http://127.0.0.1:11434",
) -> Iterator[Backend]:
    if kind == "ollama":
        # Ollama runs as a long-lived daemon (launchd plist). The harness
        # doesn't own its lifecycle — just confirm it's up and yield a
        # Backend pointing at it. RSS sampling targets whichever process
        # is listening on Ollama's port.
        yield from _yield_ollama(candidate, ollama_base_url)
        return

    port = port or _free_port()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{candidate.id}__{config.id}.log"

    if kind == "mlx":
        _prefetch_weights(candidate.mlx_repo or candidate.hf_repo)
        if draft and config.spec_decoding:
            _prefetch_weights(draft.mlx_repo or draft.hf_repo)
        args = _mlx_args(candidate, config, draft, port)
    elif kind == "llamacpp":
        args = _llamacpp_args(candidate, config, draft, port, models_dir)
    else:
        raise ValueError(f"unknown backend kind: {kind}")

    _console.log(
        f"launching {kind} server for [bold]{candidate.id}[/] · {config.id} on :{port}"
    )
    _console.log(f"  server log → {log_path}")
    proc = subprocess.Popen(  # noqa: S603
        args,
        stdout=log_path.open("a"),
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    sampler = None
    try:
        _wait_ready(port, proc=proc, log_path=log_path)
        # mlx-lm's server uses the `model` field in each request to resolve
        # which weights to serve; it must match the repo passed on the CLI,
        # not our local registry id, or it 404s trying to download it.
        model_id = candidate.mlx_repo or candidate.hf_repo
        backend = Backend(
            kind=kind,
            base_url=f"http://127.0.0.1:{port}",
            model_id=model_id,
        )
        # Start a background RSS sampler. Attached to the backend so
        # run_benchmark can read peak bytes when writing each task's metrics.
        from harness.metrics import RssSampler

        sampler = RssSampler(pid=proc.pid, interval_s=2.0)
        sampler.start()
        backend._rss_sampler = sampler  # type: ignore[attr-defined]
        yield backend
    finally:
        if sampler is not None:
            sampler.stop()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=15)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def _yield_ollama(candidate: Candidate, base_url: str) -> Iterator[Backend]:
    """Treat an externally-managed Ollama daemon as the backend.

    Validates connectivity to /v1/models, then preloads the model with a
    one-token completion so the first benched task isn't measuring the
    cold-load. Sampler targets whatever process is listening on Ollama's
    port (usually the `ollama` parent that owns the loaded runner)."""
    if not candidate.ollama_tag:
        raise ServerError(
            f"candidate {candidate.id} has no ollama_tag; cannot run on Ollama"
        )
    try:
        r = httpx.get(f"{base_url}/v1/models", timeout=5.0)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        raise ServerError(
            f"Ollama daemon not reachable at {base_url}: {e}"
        ) from e

    _console.log(
        f"using ollama daemon at [bold]{base_url}[/] for "
        f"[bold]{candidate.id}[/] (tag: {candidate.ollama_tag})"
    )
    # Preload weights — Ollama lazy-loads on first request, which would
    # otherwise be charged as the first benchmark task's TTFT.
    try:
        httpx.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": candidate.ollama_tag,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "temperature": 0,
                "stream": False,
            },
            timeout=300.0,
        ).raise_for_status()
    except Exception as e:  # noqa: BLE001
        raise ServerError(
            f"Ollama failed to load {candidate.ollama_tag}: {e}"
        ) from e

    backend = Backend(
        kind="ollama",
        base_url=base_url,
        model_id=candidate.ollama_tag,
    )

    sampler: "RssSampler | None" = None
    pid = _ollama_pid(base_url)
    if pid:
        from harness.metrics import RssSampler

        sampler = RssSampler(pid=pid, interval_s=2.0)
        sampler.start()
        backend._rss_sampler = sampler  # type: ignore[attr-defined]
    try:
        yield backend
    finally:
        if sampler is not None:
            sampler.stop()


def _ollama_pid(base_url: str) -> int | None:
    """Find the pid of whatever process owns Ollama's listening socket."""
    from urllib.parse import urlparse

    port = urlparse(base_url).port
    if port is None:
        return None
    for proc in psutil.process_iter(["pid"]):
        try:
            for c in proc.net_connections(kind="inet"):
                if c.laddr and c.laddr.port == port and c.status == "LISTEN":
                    return proc.pid
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            continue
    return None


def sample_peak_rss(pid: int) -> int:
    """Peak RSS (bytes) of the process tree rooted at pid.

    Tolerant of zombies: if the server has died, we return 0 rather than
    letting the sampler thread crash (which would otherwise take down the
    whole sweep from a background thread).
    """
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0
    try:
        total = root.memory_info().rss
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
        return 0
    for child in root.children(recursive=True):
        try:
            total += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue
    return total


def is_server_alive(base_url: str, timeout_s: float = 3.0) -> bool:
    """Quick health check — True iff /v1/models responds."""
    try:
        r = httpx.get(f"{base_url}/v1/models", timeout=timeout_s)
        return r.status_code < 500
    except Exception:  # noqa: BLE001
        return False
