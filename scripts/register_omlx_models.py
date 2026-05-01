#!/usr/bin/env python
"""Register HF-cached MLX models with the local oMLX server.

oMLX (the brew launchd service) only registers models present as directories
under ~/.omlx/models/. HuggingFace-cached weights are NOT auto-discovered.
This script symlinks each candidate's HF snapshot directory into
~/.omlx/models/<oMLX-name>/ so they appear in /v1/models after a server
re-scan.

Usage:
  python scripts/register_omlx_models.py --models-file roster.json --dry-run
  python scripts/register_omlx_models.py --models-file roster.json
  python scripts/register_omlx_models.py --models-file roster.json --restart-omlx
  python scripts/register_omlx_models.py --models-file roster.json --remove

The roster JSON is a list of {label, model, hf_repo, params_b} entries.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rich.console import Console  # noqa: E402

console = Console()

# bench_small_models.py was retired in the v1.0 mono-only simplification
# (2026-04-30). Pass --models-file <path> with a JSON list of
# {label, model, hf_repo, params_b} to register a custom roster.
DEFAULT_CANDIDATES: list[dict] = []

OMLX_MODELS_DIR = Path.home() / ".omlx" / "models"
HF_HUB_DIR = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub"


def find_snapshot(hf_repo: str) -> Path | None:
    """Resolve the HF cache snapshot directory for a given mlx-community repo.

    Layout:
      ~/.cache/huggingface/hub/models--<org>--<name>/snapshots/<sha>/
    """
    cache_subdir = "models--" + hf_repo.replace("/", "--")
    snap_root = HF_HUB_DIR / cache_subdir / "snapshots"
    if not snap_root.is_dir():
        return None
    snaps = sorted(snap_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    snaps = [s for s in snaps if s.is_dir()]
    return snaps[0] if snaps else None


def link_one(candidate: dict, dry_run: bool) -> tuple[str, Path | None, str]:
    """Symlink one candidate's HF snapshot to ~/.omlx/models/<model-id>/.

    Returns (status, target_path, msg). Status ∈ {created, exists, missing, error}.
    """
    repo = candidate.get("hf_repo")
    if not repo:
        return "error", None, "no hf_repo in candidate dict"

    snap = find_snapshot(repo)
    if snap is None:
        return "missing", None, f"no HF cache snapshot for {repo} — run `hf download {repo}` first"

    link_path = OMLX_MODELS_DIR / candidate["model"]
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink():
            target = link_path.resolve()
            if target == snap.resolve():
                return "exists", link_path, f"already linked → {target}"
            return "exists", link_path, f"link points elsewhere → {target}"
        return "exists", link_path, "real directory exists (not a symlink) — leaving untouched"

    if dry_run:
        return "created", link_path, f"would link → {snap}"

    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(snap)
    return "created", link_path, f"linked → {snap}"


def remove_one(candidate: dict, dry_run: bool) -> tuple[str, str]:
    link_path = OMLX_MODELS_DIR / candidate["model"]
    if not (link_path.exists() or link_path.is_symlink()):
        return "skip", "not present"
    if not link_path.is_symlink():
        return "skip", "exists but is a real directory (not removing)"
    if dry_run:
        return "removed", f"would unlink {link_path}"
    link_path.unlink()
    return "removed", f"unlinked {link_path}"


def restart_omlx() -> bool:
    """Restart the brew-managed oMLX service."""
    console.print("\n[bold]Restarting oMLX brew service…[/]")
    r = subprocess.run(["brew", "services", "restart", "omlx"], capture_output=True, text=True)
    if r.returncode != 0:
        console.print(f"[red]restart failed[/]: {r.stderr.strip() or r.stdout.strip()}")
        return False
    console.print(f"[green]✓[/] {r.stdout.strip()}")
    # Give oMLX a moment to come back up.
    console.print("Waiting up to 30s for oMLX to re-scan model dir…")
    from luxe.backend import Backend
    b = Backend()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if b.health():
            return True
        time.sleep(1.0)
    console.print("[yellow]oMLX did not respond within 30s — check logs at /opt/homebrew/var/log/omlx.log[/]")
    return False


def verify_loaded(candidates: list[dict]) -> None:
    """Print which candidates are now registered."""
    from luxe.backend import Backend
    b = Backend()
    if not b.health():
        console.print("[yellow]oMLX unreachable — skipping verification.[/]")
        return
    available = set(b.list_models())
    console.print(f"\n[bold]Verification[/] — oMLX now lists {len(available)} model(s).")
    for c in candidates:
        marker = "[green]✓[/]" if c["model"] in available else "[red]✗[/]"
        console.print(f"  {marker} {c['label']:22s} ({c['model']})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models-file", type=Path, default=None,
                    help="JSON file with [{label, model, hf_repo, params_b}, ...]")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change, don't write")
    ap.add_argument("--remove", action="store_true",
                    help="Unlink the registered symlinks (reverses --register)")
    ap.add_argument("--restart-omlx", action="store_true",
                    help="After symlinking, restart the brew oMLX service so it re-scans")
    args = ap.parse_args()

    if args.models_file:
        candidates = json.loads(args.models_file.read_text())
    else:
        candidates = DEFAULT_CANDIDATES

    console.print(f"[bold]oMLX model dir:[/] {OMLX_MODELS_DIR}")
    console.print(f"[bold]HF cache dir:  [/] {HF_HUB_DIR}")
    console.print(f"[bold]Candidates:    [/] {len(candidates)}")
    console.print(f"[bold]Mode:          [/] {'remove' if args.remove else 'register'}"
                  f"{' (DRY RUN)' if args.dry_run else ''}")
    console.print()

    if args.remove:
        for c in candidates:
            status, msg = remove_one(c, args.dry_run)
            color = {"removed": "yellow", "skip": "dim"}.get(status, "white")
            console.print(f"  [{color}]{status:8s}[/] {c['label']:22s} — {msg}")
    else:
        for c in candidates:
            status, _, msg = link_one(c, args.dry_run)
            color = {"created": "green", "exists": "cyan", "missing": "red", "error": "red"}.get(status, "white")
            console.print(f"  [{color}]{status:8s}[/] {c['label']:22s} — {msg}")

    if args.dry_run:
        console.print("\n[dim]--dry-run: no changes made.[/]")
        return 0

    if args.restart_omlx:
        ok = restart_omlx()
        if ok:
            verify_loaded(candidates)
    else:
        console.print("\n[dim]Symlinks created. Run with --restart-omlx, "
                      "or `brew services restart omlx`, to make oMLX rescan.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
