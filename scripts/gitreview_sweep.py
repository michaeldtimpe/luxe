#!/usr/bin/env python3
"""Batch `luxe gitreview` over all of the user's GitHub repos — telemetry sweep.

Drives the existing `luxe gitreview` CLI once per repo to (a) lay down first-pass
baselines + per-HEAD `map/` caches and (b) roll the new per-run `timing.json`
telemetry (gitkit deep mode) up into CSVs that calibrate the wall estimate across
a wide size range. Pure orchestration — no gitkit-contract changes.

Design (see ~/.claude/plans/deep-dancing-fox.md, Part C):
  - Repo list = `gh repo list <owner> --json name,sshUrl,diskUsage` (the 46 repos;
    `gh` as source naturally excludes third-party clones like llama.cpp).
  - Resolve each to a local working copy BY REMOTE IDENTITY (git remote origin →
    owner/name), NOT basename — so a same-named fork/mirror is never analyzed by
    mistake. Missing repos are cloned into ~/.luxe/sweep-clones/<name>.
  - Run `luxe gitreview <path> --keep-loaded` as a piped (non-TTY) subprocess:
    no TTY → the large-repo confirm gate auto-skips → full depth, unbounded
    (footprint auto-selects single-pass vs deep). --keep-loaded keeps the champion
    warm between repos.
  - Resumable: skip a repo whose current HEAD already has a saved gitreview report
    (resume key = repo_hash + HEAD, the strongest identifier).
  - Order smallest-first so broad telemetry accrues before the multi-hour giants
    (neon-rain ~601 MB, aurora ~138 MB); a kill mid-run still yields most points.
  - Roll up: scripts/out/sweep_telemetry.csv (one row/repo) +
    scripts/out/sweep_passes.csv (one row/pass).

Usage:
  uv run python scripts/gitreview_sweep.py [--owner michaeldtimpe]
      [--only a,b,c] [--skip neon-rain] [--kind gitreview] [--dry-run]

Knobs: --only / --skip select repos by name; --dry-run prints the resolved plan
(reuse vs clone, order, skip-because-done) without running anything.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Make the in-tree luxe package importable when run via `uv run python scripts/…`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from luxe.memory.project import repo_hash  # noqa: E402
from luxe.repo_index import build_repo_summary  # noqa: E402

HOME = Path.home()
REPORTS_ROOT = HOME / ".luxe" / "reports"
CLONE_ROOT = HOME / ".luxe" / "sweep-clones"
OUT_DIR = Path(__file__).resolve().parent / "out"
LOG_DIR = OUT_DIR / "logs"
TELEMETRY_CSV = OUT_DIR / "sweep_telemetry.csv"
PASSES_CSV = OUT_DIR / "sweep_passes.csv"

# Local roots to scan for already-cloned working copies (exclude bench/cache/tooling).
LOCAL_SCAN_ROOTS = [HOME / "Downloads", HOME / "code", HOME]
LOCAL_SCAN_EXCLUDE = ("/swebench-workspace", "/bench-workspace", "/fixture-cache",
                      "/.oh-my-zsh", "/.tmux/", "/Library/", "/node_modules/")

TELEMETRY_COLS = ["repo", "path", "head", "model", "window", "mode",
                  "footprint_loc", "footprint_tokens", "n_files", "chunks",
                  "n_passes", "total_wall_s", "avg_pass_s", "est_minutes",
                  "actual_minutes"]
PASS_COLS = ["repo", "label", "window", "wall_s", "est_tokens", "loc", "n_files",
             "started_at"]


def sh(args: list[str], cwd: Path | None = None, timeout: int | None = None) -> tuple[int, str, str]:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


# --- repo discovery ---------------------------------------------------------

def gh_repos(owner: str) -> list[dict]:
    rc, out, err = sh(["gh", "repo", "list", owner, "--limit", "200",
                       "--json", "name,sshUrl,diskUsage"])
    if rc != 0:
        sys.exit(f"gh repo list failed: {err.strip() or out.strip()}")
    return json.loads(out)


def origin_identity(path: Path) -> str | None:
    """`git remote get-url origin` → 'owner/name' (lowercased), or None."""
    rc, out, _ = sh(["git", "remote", "get-url", "origin"], cwd=path)
    if rc != 0 or not out.strip():
        return None
    url = re.sub(r"\.git$", "", out.strip())
    m = re.search(r"[/:]([^/:]+)/([^/:]+)$", url)
    return f"{m.group(1)}/{m.group(2)}".lower() if m else None


def discover_local_clones() -> dict[str, Path]:
    """Map 'owner/name' → local path for every real (non-bench) clone on disk."""
    seen: dict[str, Path] = {}
    found: set[Path] = set()
    for root in LOCAL_SCAN_ROOTS:
        if not root.is_dir():
            continue
        rc, out, _ = sh(["find", str(root), "-maxdepth", "6", "-type", "d",
                         "-name", ".git"])
        if rc != 0:
            continue
        for line in out.splitlines():
            repo = Path(line).parent
            if any(x in str(repo) + "/" for x in LOCAL_SCAN_EXCLUDE):
                continue
            found.add(repo)
    for repo in found:
        ident = origin_identity(repo)
        if ident and ident not in seen:
            seen[ident] = repo
    return seen


def resolve_local(repo: dict, owner: str, local_by_ident: dict[str, Path]) -> tuple[Path, str]:
    """Return (path, how) for a GitHub repo — reuse a clone matched by remote
    identity, else clone into CLONE_ROOT. `how` ∈ {reuse, cloned, clone-failed}."""
    ident = f"{owner}/{repo['name']}".lower()
    if ident in local_by_ident:
        return local_by_ident[ident], "reuse"
    dest = CLONE_ROOT / repo["name"]
    if (dest / ".git").is_dir() and origin_identity(dest) == ident:
        return dest, "reuse"
    CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    rc, _, err = sh(["git", "clone", repo["sshUrl"], str(dest)], timeout=1800)
    if rc != 0:
        print(f"  clone failed for {repo['name']}: {err.strip()[:200]}")
        return dest, "clone-failed"
    return dest, "cloned"


# --- resume + telemetry -----------------------------------------------------

def current_head(path: Path) -> str:
    rc, out, _ = sh(["git", "rev-parse", "--short", "HEAD"], cwd=path)
    return out.strip() if rc == 0 else ""


def _frontmatter(md: Path) -> dict:
    txt = md.read_text(errors="ignore")
    if not txt.startswith("---\n"):
        return {}
    end = txt.find("\n---", 4)
    body = txt[4:end] if end != -1 else ""
    fm: dict = {}
    for line in body.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def existing_reports(path: Path, kind: str) -> list[Path]:
    rdir = REPORTS_ROOT / repo_hash(path)
    return sorted(rdir.glob(f"{kind}-*.md"), key=lambda p: p.stat().st_mtime)


def already_done(path: Path, kind: str, head: str) -> bool:
    return any(_frontmatter(p).get("head") == head for p in existing_reports(path, kind))


def newest_report(path: Path, kind: str) -> Path | None:
    reports = existing_reports(path, kind)
    return reports[-1] if reports else None


def newest_timing(path: Path, kind: str) -> dict | None:
    rdir = REPORTS_ROOT / repo_hash(path)
    works = sorted(rdir.glob(f"{kind}-*.work"), key=lambda p: p.stat().st_mtime)
    for w in reversed(works):
        tj = w / "timing.json"
        if tj.is_file():
            try:
                return json.loads(tj.read_text())
            except (ValueError, OSError):
                continue
    return None


def parse_est_minutes(log_path: Path) -> str:
    """Pull the tool's own pre-run estimate ('plan: … ~X min') out of the log."""
    if not log_path.is_file():
        return ""
    m = re.search(r"plan:.*?~\s*(\d+)\s*min", log_path.read_text(errors="ignore"))
    return m.group(1) if m else ""


def footprint(path: Path) -> tuple[int, int, int]:
    """(total_loc, est_tokens, file_count) — cheap deterministic repo size."""
    try:
        s = build_repo_summary(path)
        return s.total_loc, s.total_loc * 10, s.file_count
    except Exception:
        return 0, 0, 0


def append_csv(csv_path: Path, cols: list[str], rows: list[dict]) -> None:
    new = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def collect(repo_name: str, path: Path, head: str, kind: str, log_path: Path) -> None:
    loc, tokens, n_files = footprint(path)
    timing = newest_timing(path, kind)
    report = newest_report(path, kind)
    fm = _frontmatter(report) if report else {}
    if timing:                       # deep run
        mode = "deep"
        n_passes = timing.get("n_passes", "")
        total_wall = timing.get("total_wall_s", "")
        avg = timing.get("avg_pass_s", "")
        passes = timing.get("passes", [])
        window = max((p.get("window", 0) for p in passes), default="")
        chunks = sum(1 for p in passes if str(p.get("label", "")).startswith("chunk-"))
        append_csv(PASSES_CSV, PASS_COLS, [{"repo": repo_name, **{
            k: p.get(k, "") for k in PASS_COLS if k != "repo"}} for p in passes])
    else:                            # single-pass run
        mode = "single"
        n_passes = fm.get("n_passes", "1")
        total_wall = fm.get("total_wall_s", "")
        avg = fm.get("avg_pass_s", "")
        window = ""
        chunks = fm.get("chunks", "1")
    actual_min = ""
    try:
        actual_min = round(float(total_wall) / 60, 2) if total_wall else ""
    except (TypeError, ValueError):
        pass
    append_csv(TELEMETRY_CSV, TELEMETRY_COLS, [{
        "repo": repo_name, "path": str(path), "head": head,
        "model": fm.get("model", ""), "window": window, "mode": mode,
        "footprint_loc": loc, "footprint_tokens": tokens, "n_files": n_files,
        "chunks": chunks, "n_passes": n_passes, "total_wall_s": total_wall,
        "avg_pass_s": avg, "est_minutes": parse_est_minutes(log_path),
        "actual_minutes": actual_min}])


# --- run one repo -----------------------------------------------------------

def run_repo(path: Path, kind: str, log_path: Path) -> int:
    """Run `luxe <kind> <path> --keep-loaded`, piping all output to log_path."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        p = subprocess.run(
            ["luxe", kind, str(path), "--keep-loaded"],
            stdout=log, stderr=subprocess.STDOUT, text=True, env=os.environ)
    return p.returncode


# --- main -------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--owner", default="michaeldtimpe")
    ap.add_argument("--kind", default="gitreview",
                    choices=["gitreview", "gitsummary", "gitrefactor", "gitplan"])
    ap.add_argument("--only", default="", help="comma-separated repo names to include")
    ap.add_argument("--skip", default="", help="comma-separated repo names to exclude")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the resolved plan and exit (no clones, no runs)")
    ap.add_argument("--force", action="store_true",
                    help="re-run even if a report already exists for the HEAD "
                         "(use after an engine change, e.g. deep-gitplan)")
    args = ap.parse_args()

    only = {s for s in args.only.split(",") if s}
    skip = {s for s in args.skip.split(",") if s}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    repos = gh_repos(args.owner)
    if only:
        repos = [r for r in repos if r["name"] in only]
    repos = [r for r in repos if r["name"] not in skip]
    repos.sort(key=lambda r: r.get("diskUsage", 0))      # smallest-first
    print(f"· {len(repos)} repo(s) selected (smallest-first); owner={args.owner} "
          f"kind={args.kind}")

    local_by_ident = discover_local_clones()
    print(f"· {len(local_by_ident)} local clone(s) indexed by remote identity")

    ran = failed = skipped = 0
    for i, repo in enumerate(repos, 1):
        name = repo["name"]
        path, how = resolve_local(repo, args.owner, local_by_ident) if not args.dry_run \
            else (local_by_ident.get(f"{args.owner}/{name}".lower(),
                  CLONE_ROOT / name), "reuse" if f"{args.owner}/{name}".lower()
                  in local_by_ident else "clone")
        if how == "clone-failed":
            failed += 1
            continue
        head = current_head(path) if path.exists() else "?"
        tag = f"[{i}/{len(repos)}] {name} ({repo.get('diskUsage', 0)} KB) [{how}] HEAD {head}"

        if (not args.force and path.exists() and head
                and already_done(path, args.kind, head)):
            print(f"  SKIP {tag} — report exists for this HEAD")
            skipped += 1
            continue
        if args.dry_run:
            print(f"  PLAN {tag}")
            continue

        print(f"  RUN  {tag}")
        log_path = LOG_DIR / f"{name}.log"
        t0 = time.time()
        rc = run_repo(path, args.kind, log_path)
        dt = time.time() - t0
        if rc != 0:
            print(f"       ✗ exit {rc} after {dt/60:.1f} min — see {log_path}")
            failed += 1
            continue
        collect(name, path, head, args.kind, log_path)
        print(f"       ✓ {dt/60:.1f} min")
        ran += 1

    print(f"\n· done. ran={ran} skipped={skipped} failed={failed}")
    if not args.dry_run:
        print(f"· telemetry → {TELEMETRY_CSV}\n· per-pass  → {PASSES_CSV}")


if __name__ == "__main__":
    main()
