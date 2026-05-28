"""Run metadata block — embedded at the top of every summary.json.

Without this block, longitudinal benchmark comparisons can't tell
"model regressed" apart from "tokenizer / chat-template / quant changed."
"""
from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVAL_SUITE_VERSION = "0.1.0"


@dataclass
class RunMeta:
    eval_suite_version: str
    benchmark_protocol_version: str
    model_id: str
    sampling: dict[str, Any]
    backend_kind: str
    context_window: int
    timestamp_utc: str
    luxe_commit: str | None
    device: str
    tokenizer_id: str | None = None
    backend_base_url: str | None = None
    benchmark_dataset_sha256: str | None = None
    model_file_sha256: str | None = None
    scoring: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_run_meta(
    *,
    benchmark_protocol_version: str,
    model_id: str,
    sampling: dict[str, Any],
    backend_kind: str,
    context_window: int,
    tokenizer_id: str | None = None,
    backend_base_url: str | None = None,
    benchmark_dataset_sha256: str | None = None,
    model_file_sha256: str | None = None,
    scoring: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> RunMeta:
    if backend_kind not in ("http", "mlx_direct"):
        raise ValueError(f"backend_kind must be 'http' or 'mlx_direct', got {backend_kind!r}")
    return RunMeta(
        eval_suite_version=EVAL_SUITE_VERSION,
        benchmark_protocol_version=benchmark_protocol_version,
        model_id=model_id,
        sampling=sampling,
        backend_kind=backend_kind,
        context_window=context_window,
        timestamp_utc=_now_utc_iso(),
        luxe_commit=_git_commit(),
        device=_device_string(),
        tokenizer_id=tokenizer_id,
        backend_base_url=backend_base_url,
        benchmark_dataset_sha256=benchmark_dataset_sha256,
        model_file_sha256=model_file_sha256,
        scoring=scoring or {},
        extra=extra or {},
    )


def _now_utc_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _git_commit() -> str | None:
    repo_root = Path(__file__).resolve().parents[2]
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def _device_string() -> str:
    return (
        f"{platform.system()} {platform.machine()} "
        f"(Python {sys.version_info.major}.{sys.version_info.minor})"
    )
