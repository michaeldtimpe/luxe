"""Persistence for gitkit reports — `~/.luxe/reports/<repo_hash>/`.

Mirrors the `~/.luxe/sessions` / `~/.luxe/compares` convention so reports
survive temp-clone cleanup and accrete into a per-repo audit trail.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from luxe.memory.project import repo_hash


def reports_dir(repo_path: str | Path) -> Path:
    """Return the report directory for a repo (`~/.luxe/reports/<repo_hash>/`)."""
    return Path.home() / ".luxe" / "reports" / repo_hash(repo_path)


def save_report(repo_path: str | Path, kind: str, text: str,
                meta: dict | None = None) -> Path:
    """Write a gitkit report to disk and return its path.

    Args:
        repo_path: the analyzed repo (used to derive the report directory).
        kind: report kind — gitsummary | gitreview | gitrefactor.
        text: the model's markdown report body.
        meta: optional header fields (model, head, repo display path, etc.).

    Returns:
        Path to `~/.luxe/reports/<repo_hash>/<kind>-<timestamp>-<rand>.md`. The
        short random suffix avoids clashes between same-second / concurrent runs.

    Side effects: creates the report directory (parents, exist_ok) and writes
    one markdown file with a YAML frontmatter header.
    """
    meta = dict(meta or {})
    ts = int(time.time())
    out_dir = reports_dir(repo_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{kind}-{ts}-{uuid.uuid4().hex[:6]}.md"

    header = {
        "kind": kind,
        "repo": str(meta.get("repo", repo_path)),
        "timestamp": ts,
        "model": meta.get("model", ""),
        "head": meta.get("head", ""),
    }
    # Optional pass-through keys (deep-mode marker + timing telemetry). Allowlisted
    # rather than splatting `meta` so the frontmatter schema stays explicit and
    # callers can't accidentally pollute it. `mode`/`chunks` were passed by deep
    # mode but silently dropped before this; they now land in the header too.
    for k in ("mode", "chunks", "total_wall_s", "n_passes", "avg_pass_s"):
        if k in meta:
            header[k] = meta[k]
    front = "\n".join(f"{k}: {v}" for k, v in header.items())
    path.write_text(f"---\n{front}\n---\n\n{text.rstrip()}\n")
    return path
