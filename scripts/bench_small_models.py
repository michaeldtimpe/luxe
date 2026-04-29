#!/usr/bin/env python
"""Small-model bake-off — A/B test 7 small coder models as worker tier
in both swarm and microloop modes against the qwen_32gb baseline.

What it does
------------
For each candidate model:
  1. Generates an overlay YAML by deep-copying configs/qwen_32gb.yaml and
     re-pointing the worker tier (swarm: worker_read/worker_code/worker_analyze;
     microloop: drafter/coder) to the candidate.
  2. Runs the bench harness against a small task list under both modes.
  3. Aggregates per-(candidate, mode) wall-time, decode tok/s, score,
     abort rate, and microstep-reject rate into one comparison table.

Pre-flight
----------
Pings oMLX /v1/models and SKIPs candidates whose model ID isn't loaded —
prints a clear hint up front so you know what to load before re-running.

Usage
-----
  python scripts/bench_small_models.py
  python scripts/bench_small_models.py --tasks summarize-python
  python scripts/bench_small_models.py --tasks bugfix-sqli --modes microloop
  python scripts/bench_small_models.py --models-file my_models.json --full
  python scripts/bench_small_models.py --dry-run

Expected runtime: ~2-5 min/run x 7 models x 2 modes x 1 task ≈ 30-60 min.
The first run for a freshly-loaded model includes the oMLX cold-load —
expect 30-60s extra latency on the first stage.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

# Ensure src/ is importable when run from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from luxe.backend import Backend  # noqa: E402
from luxe.benchmark.runner import BenchmarkSuite, run_benchmark  # noqa: E402

console = Console()

# ---- Default candidate roster --------------------------------------------
# `model` is the oMLX model ID (the part after `mlx-community/` for HF repos
# downloaded via scripts/download_models.sh). `hf_repo` is the HuggingFace
# path used for --download-missing. Override via --models-file <path>.
DEFAULT_CANDIDATES: list[dict] = [
    {"label": "qwen-coder-0.5b", "model": "Qwen2.5-Coder-0.5B-Instruct-4bit",
     "hf_repo": "mlx-community/Qwen2.5-Coder-0.5B-Instruct-4bit", "params_b": 0.5},
    {"label": "qwen-coder-1.5b", "model": "Qwen2.5-Coder-1.5B-Instruct-4bit",
     "hf_repo": "mlx-community/Qwen2.5-Coder-1.5B-Instruct-4bit", "params_b": 1.5},
    # Bake-off iteration 1 (2026-04-28): tested 7 candidates, kept the qwen
    # series. Dropped: granite-h-tiny (29-min microloop wall, 14.7 schema
    # rejects/run); deepseek-coder-1.3b and stable-code-3b (zero tool calls
    # — passthrough scoring); starcoder2-3b and codegemma-2b (base completion
    # models, no chat_template).
]

DEFAULT_TASKS = ["summarize-python"]   # cheapest probe; 3-stage pipeline, read-only
FULL_TASKS = ["summarize-python", "review-python-quality", "bugfix-sqli"]
BASE_CONFIG = ROOT / "configs" / "qwen_32gb.yaml"

# Worker-tier role keys overridden per candidate. Keeping architect/validator/
# synthesizer/verifier on the 3B baseline isolates the candidate as the
# coding workhorse — the comparison stays apples-to-apples across candidates.
SWARM_ROLE_KEYS = ["worker_read", "worker_code", "worker_analyze"]
MICROLOOP_ROLE_KEYS = ["drafter", "coder"]


def write_overlay(candidate: dict, modes: list[str], outdir: Path) -> Path:
    """Generate an overlay YAML pointing the worker tier at a candidate model."""
    base = yaml.safe_load(BASE_CONFIG.read_text())
    overlay = deepcopy(base)

    # Replace worker-tier model aliases with the candidate's model.
    target_keys: set[str] = set()
    if "swarm" in modes:
        target_keys.update(SWARM_ROLE_KEYS)
    if "microloop" in modes:
        target_keys.update(MICROLOOP_ROLE_KEYS)

    for key in target_keys:
        overlay["models"][key] = candidate["model"]

    # Cap context to keep small models inside their high-attention zone.
    # Most of these candidates are <=4B with 4-16k native context but
    # poor effective attention beyond ~4k.
    for role_name, role in overlay["roles"].items():
        if role.get("model_key") in target_keys:
            role["num_ctx"] = min(int(role.get("num_ctx", 4096)), 4096)

    # File-stem becomes the bench label, so keep it terse and stable.
    out_path = outdir / f"{candidate['label']}.yaml"
    out_path.write_text(yaml.safe_dump(overlay, sort_keys=False))
    return out_path


def warmup_one(base_url: str, model_id: str, timeout_s: float) -> tuple[bool, float, str]:
    """Probe whether a model is loadable by posting a 1-token chat completion.

    Returns (ok, wall_s, error_msg). Wall time is the cold-load cost — use it
    as a heuristic for how slow this candidate's first stage will be.

    A bare httpx call (not Backend.chat) so the rich BackendError retry
    classification doesn't interfere — we want raw load latency.
    """
    import httpx
    import os
    headers = {}
    if (k := os.environ.get("OMLX_API_KEY")):
        headers["Authorization"] = f"Bearer {k}"
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "temperature": 0.0,
        "stream": False,
    }
    t0 = time.monotonic()
    try:
        with httpx.Client(base_url=base_url, timeout=httpx.Timeout(timeout_s, connect=10.0),
                          headers=headers) as cli:
            r = cli.post("/v1/chat/completions", json=body)
        wall = time.monotonic() - t0
        if r.status_code == 200:
            return True, wall, ""
        body_excerpt = (r.text or "")[:200].replace("\n", " ")
        return False, wall, f"HTTP {r.status_code}: {body_excerpt}"
    except httpx.HTTPError as e:
        return False, time.monotonic() - t0, f"{type(e).__name__}: {e}"


def hf_download(repo: str) -> tuple[bool, str]:
    """Run `hf download <repo>` to fetch missing MLX weights from HuggingFace."""
    import shutil, subprocess
    cmd = "hf" if shutil.which("hf") else ("huggingface-cli" if shutil.which("huggingface-cli") else None)
    if cmd is None:
        return False, "neither 'hf' nor 'huggingface-cli' on PATH"
    try:
        r = subprocess.run([cmd, "download", repo], capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            return False, f"{cmd} exit {r.returncode}: {(r.stderr or '')[-300:]}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "download timed out (15 min)"


def prepare_candidates(
    candidates: list[dict], base_url: str,
    *, warmup_timeout_s: float, allow_download: bool,
) -> list[dict]:
    """Verify each candidate is reachable; optionally download missing weights.

    Returns the list of loadable candidates with `cold_load_s` populated.
    Models that fail warmup are dropped with a clear message.
    """
    backend = Backend(base_url=base_url, model="(probe)")
    if not backend.health():
        console.print(f"[red]oMLX server unreachable at {base_url}[/]. "
                      "Start it (or check OMLX_API_KEY) and re-run.")
        sys.exit(2)

    pre_loaded = set(backend.list_models())
    console.print(f"[dim]oMLX currently lists {len(pre_loaded)} model(s) as loaded.[/]")

    out: list[dict] = []
    for c in candidates:
        label, mid = c["label"], c["model"]
        in_list = mid in pre_loaded
        marker = "[green]listed[/]" if in_list else "[yellow]not-listed[/]"
        console.print(f"\n  → [bold]{label}[/] ({mid}) — {marker}, probing chat…")

        ok, wall, err = warmup_one(base_url, mid, timeout_s=warmup_timeout_s)
        if ok:
            c2 = {**c, "cold_load_s": wall}
            out.append(c2)
            console.print(f"    [green]✓ loaded[/] in {wall:.1f}s")
            continue

        # If the failure looks like "model not found" and we have an HF repo,
        # offer to download then retry once.
        looks_missing = "not found" in err.lower() or "404" in err or "no such" in err.lower()
        repo = c.get("hf_repo")
        if looks_missing and allow_download and repo:
            console.print(f"    [yellow]not found in oMLX[/] — attempting hf download {repo}")
            dl_ok, dl_err = hf_download(repo)
            if not dl_ok:
                console.print(f"    [red]✗ download failed[/]: {dl_err}")
                console.print(f"    [dim]skipping {label}[/]")
                continue
            console.print(f"    [green]downloaded[/] — retrying warmup")
            ok2, wall2, err2 = warmup_one(base_url, mid, timeout_s=warmup_timeout_s)
            if ok2:
                out.append({**c, "cold_load_s": wall2})
                console.print(f"    [green]✓ loaded[/] in {wall2:.1f}s")
                continue
            err = err2

        console.print(f"    [red]✗ skipped[/]: {err[:160]}")

    return out


def render_summary(suite: BenchmarkSuite, candidates: list[dict],
                   modes: list[str]) -> None:
    """Pivot suite.results by (candidate-label, mode) and print a comparison table."""
    by_key: dict[tuple[str, str], list] = {}
    label_set = {c["label"] for c in candidates}
    default_mode = modes[0] if modes else "swarm"

    for r in suite.results:
        # config_name format: "<label> [mode]" when multiple modes run,
        # or just "<label>" when a single mode runs (use default_mode then).
        cn = r.config_name
        if " [" in cn and cn.endswith("]"):
            label, mode = cn.split(" [", 1)
            mode = mode.rstrip("]")
        else:
            label, mode = cn, default_mode
        if label not in label_set:
            continue
        by_key.setdefault((label, mode), []).append(r)

    # Map candidate label → params for sort key.
    params_b = {c["label"]: c["params_b"] for c in candidates}

    table = Table(title="Small-Model Bake-Off — averaged across tasks per (model, mode)")
    table.add_column("Model", style="cyan")
    table.add_column("Params", justify="right")
    table.add_column("Mode", style="magenta")
    table.add_column("OK/Run", justify="right")
    table.add_column("Wall s", justify="right")
    table.add_column("Tok/s", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Tools", justify="right")
    table.add_column("Schema rj", justify="right")
    table.add_column("μ-rej", justify="right")
    table.add_column("Aborts", justify="right")

    sorted_keys = sorted(by_key.keys(), key=lambda k: (params_b.get(k[0], 0), k[0], k[1]))
    for (label, mode) in sorted_keys:
        runs = by_key[(label, mode)]
        ok = [r for r in runs if r.error is None]
        n_ok, n_total = len(ok), len(runs)

        if not ok:
            table.add_row(label, f"{params_b.get(label, 0):.1f}B", mode,
                          f"{n_ok}/{n_total}", "—", "—", "—", "—", "—", "—",
                          str(n_total))
            continue

        avg_wall = sum(r.metrics.total_wall_s for r in ok) / n_ok
        # decode tok/s: completion_tokens / wall_s aggregated across stages
        comp = sum(r.metrics.total_completion_tokens for r in ok)
        wall = sum(r.metrics.total_wall_s for r in ok)
        tok_per_s = comp / wall if wall > 0 else 0.0
        avg_score = sum(r.score.detection_rate for r in ok) / n_ok
        avg_tools = sum(r.metrics.total_tool_calls for r in ok) / n_ok
        avg_schema = sum(r.metrics.total_schema_rejects for r in ok) / n_ok
        # microstep_rejects lives on per-subtask metrics — collector may not
        # surface it. Try the attribute, default 0.
        mu_rej = sum(getattr(r.metrics, "total_microstep_rejects", 0) for r in ok) / n_ok
        aborts = sum(1 for r in runs if r.error is not None)

        table.add_row(
            label, f"{params_b.get(label, 0):.1f}B", mode,
            f"{n_ok}/{n_total}",
            f"{avg_wall:.1f}",
            f"{tok_per_s:.1f}",
            f"{avg_score:.0%}",
            f"{avg_tools:.1f}",
            f"{avg_schema:.1f}",
            f"{mu_rej:.1f}",
            str(aborts),
        )

    console.print()
    console.print(table)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models-file", type=Path, default=None,
                    help="JSON file with [{label, model, params_b}, ...]; overrides default roster")
    ap.add_argument("--tasks", nargs="*", default=None,
                    help=f"Bench task IDs (default: {DEFAULT_TASKS})")
    ap.add_argument("--modes", nargs="*", choices=["swarm", "microloop"],
                    default=["swarm", "microloop"],
                    help="Modes to evaluate (default: both)")
    ap.add_argument("--full", action="store_true",
                    help=f"Use the longer task list ({FULL_TASKS}) instead of the default cheap probe")
    ap.add_argument("--output", type=Path, default=ROOT / "benchmarks",
                    help="Output dir for the bench suite JSON")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000",
                    help="oMLX base URL")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the matrix and overlay paths, don't execute bench")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="Skip the warmup probe; assume each candidate is loadable on demand")
    ap.add_argument("--warmup-timeout", type=float, default=180.0,
                    help="Per-model warmup timeout in seconds (default 180s — generous for cold load)")
    ap.add_argument("--download-missing", action="store_true",
                    help="If a candidate's weights aren't on disk, run `hf download` to fetch them")
    args = ap.parse_args()

    # Resolve candidate roster.
    if args.models_file:
        candidates = json.loads(args.models_file.read_text())
        if not isinstance(candidates, list) or not all("label" in c and "model" in c for c in candidates):
            console.print("[red]models-file must be a JSON array of {label, model, params_b}[/]")
            return 2
    else:
        candidates = DEFAULT_CANDIDATES

    # Resolve tasks.
    if args.tasks:
        tasks = list(args.tasks)
    elif args.full:
        tasks = list(FULL_TASKS)
    else:
        tasks = list(DEFAULT_TASKS)

    console.print(f"\n[bold]Small-Model Bake-Off[/]")
    console.print(f"  Candidates: {len(candidates)}")
    console.print(f"  Modes:      {args.modes}")
    console.print(f"  Tasks:      {tasks}")
    console.print(f"  Base cfg:   {BASE_CONFIG}")
    console.print(f"  Output:     {args.output}")

    # Pre-flight: probe each candidate via a 1-token chat to force-load it.
    # /v1/models on this oMLX lists only currently-loaded models, but the
    # server hot-loads on first request, so a chat probe is the real test.
    if not args.skip_preflight:
        console.print(f"\n[bold]Warm-up probe[/] (timeout={args.warmup_timeout:.0f}s/model"
                      f"{', will hf-download missing weights' if args.download_missing else ''})")
        loadable = prepare_candidates(
            candidates, args.base_url,
            warmup_timeout_s=args.warmup_timeout,
            allow_download=args.download_missing,
        )
        skipped = [c for c in candidates if c["label"] not in {x["label"] for x in loadable}]
        if skipped:
            console.print(f"\n[yellow]⚠ {len(skipped)} candidate(s) skipped:[/]")
            for c in skipped:
                console.print(f"    - {c['label']:25s}  ({c['model']})")
            if not args.download_missing:
                console.print("[dim]Hint: re-run with --download-missing to auto-fetch HF weights.[/]\n")
        if not loadable:
            console.print("[red]No loadable candidates — aborting.[/]")
            return 3
        candidates = loadable

    # Generate overlays.
    overlay_dir = Path(tempfile.mkdtemp(prefix="luxe_bakeoff_"))
    overlay_paths: list[Path] = []
    for c in candidates:
        p = write_overlay(c, args.modes, overlay_dir)
        overlay_paths.append(p)
        console.print(f"  overlay: {c['label']:25s} → {p}")

    if args.dry_run:
        console.print("\n[dim]--dry-run: stopping before bench launch.[/]")
        return 0

    args.output.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    suite = run_benchmark(
        config_paths=[str(p) for p in overlay_paths],
        task_ids=tasks,
        output_dir=str(args.output),
        execution_modes=list(args.modes),
    )
    wall_total = time.monotonic() - t0

    console.print(f"\n[bold green]✓ Bake-off complete[/] in {wall_total/60:.1f} min "
                  f"({len(suite.results)} runs)")
    render_summary(suite, candidates, args.modes)

    # Save a model-bake-off-flavored copy alongside the suite.
    bo_path = args.output / f"small_models_{int(time.time())}.json"
    bo_path.write_text(json.dumps({
        "candidates": candidates,
        "tasks": tasks,
        "modes": args.modes,
        "wall_total_s": wall_total,
        "suite": suite.to_dict(),
    }, indent=2, default=str))
    console.print(f"[dim]Bake-off report saved: {bo_path}[/]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
