"""End-to-end automated A/B benchmark with preflight + tee'd logging.

Single command: `uv run python scripts/run_ab_full.py`

Phases:
  0. Preflight  — verify Ollama daemon, llama-server binary, candidate
                  weights present in both Ollama and HF cache.
  1. Pull       — `ollama pull` any missing tags; `huggingface-cli`-style
                  prefetch any missing GGUFs (so the first llama-server
                  boot isn't a 5–20 GB download timing out).
  2. Sweep      — invoke scripts/run_ab_benchmark.py end-to-end with
                  every line tee'd to a timestamped log file under
                  results/ab_ollama_vs_llamacpp/runs/.
  3. Report     — print the regenerated REPORT.md and exit cleanly.

Exits non-zero on any preflight failure or sweep crash. Safe to leave
running for hours: SIGINT propagates to the child process, partial
results are preserved on disk (every benchmark is resumable).

Flags:
  --skip-pulls          don't try to pull missing models, fail preflight
  --skip-prefetch       don't prefetch GGUFs (let first llama-server
                        boot do it inline)
  --candidate <ids>     comma-separated candidate id list (forwarded)
  --backends <kinds>    comma-separated backend list (forwarded)
  --bench <names>       comma-separated benchmark list (forwarded)
  --limit <n>           cap tasks per benchmark (forwarded)
  --yes                 don't prompt for confirmations
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.registry import Candidate, load_registry  # noqa: E402

PHASE_DIR = ROOT / "results" / "ab_ollama_vs_llamacpp"
LOG_DIR = PHASE_DIR / "runs"
DEFAULT_OLLAMA = "http://127.0.0.1:11434"

console = Console()


def _ts() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def _say(msg: str, *, style: str = "") -> None:
    """One-line console + log output, prefixed with HH:MM:SS."""
    console.print(f"[dim]{_ts()}[/dim]  {msg}", style=style)


def _section(title: str) -> None:
    console.print()
    console.print(Panel(title, border_style="cyan", expand=False))


# ── Phase 0: preflight ────────────────────────────────────────────────


def _preflight(
    ollama_url: str, candidates: list[Candidate], backends: list[str]
) -> tuple[list[str], list[Candidate]]:
    """Check daemons + binaries. Returns (missing_ollama_tags,
    candidates_needing_gguf_prefetch). Hard-fails on any unrecoverable
    issue."""
    _section("Phase 0 · preflight")

    issues: list[str] = []

    # Ollama daemon
    if "ollama" in backends:
        try:
            r = httpx.get(f"{ollama_url}/v1/models", timeout=5.0)
            r.raise_for_status()
            _say(f"[green]✓[/green] Ollama daemon reachable at {ollama_url}")
        except Exception as e:  # noqa: BLE001
            issues.append(
                f"Ollama daemon unreachable at {ollama_url} ({e}). "
                f"Start it: `brew services start ollama` or `ollama serve`."
            )

    # llama-server binary
    if "llamacpp" in backends:
        if shutil.which("llama-server") is None:
            issues.append(
                "llama-server not on $PATH. Install: `brew install llama.cpp`."
            )
        else:
            _say(f"[green]✓[/green] llama-server binary present "
                 f"({shutil.which('llama-server')})")

    if issues:
        for i in issues:
            _say(f"[red]✗[/red] {i}", style="red")
        raise typer.Exit(2)

    # Per-candidate model presence
    missing_ollama: list[str] = []
    needs_prefetch: list[Candidate] = []

    if "ollama" in backends:
        try:
            installed = _list_ollama_tags(ollama_url)
        except Exception as e:  # noqa: BLE001
            _say(f"[yellow]![/yellow] Could not list ollama tags: {e}")
            installed = set()
        for c in candidates:
            if not c.ollama_tag:
                _say(f"[yellow]![/yellow] {c.id} has no ollama_tag — "
                     f"ollama backend will be skipped for it")
                continue
            if c.ollama_tag in installed:
                _say(f"[green]✓[/green] ollama tag installed: {c.ollama_tag}")
            else:
                missing_ollama.append(c.ollama_tag)
                _say(f"[yellow]·[/yellow] ollama tag missing: {c.ollama_tag}")

    if "llamacpp" in backends:
        for c in candidates:
            if not (c.gguf_repo and c.gguf_file):
                _say(f"[yellow]![/yellow] {c.id} has no gguf_repo/file — "
                     f"llamacpp backend will fail for it")
                continue
            cached = _gguf_cached(c)
            if cached:
                _say(f"[green]✓[/green] GGUF cached: {c.gguf_repo}/{c.gguf_file}")
            else:
                needs_prefetch.append(c)
                _say(f"[yellow]·[/yellow] GGUF not cached: "
                     f"{c.gguf_repo}/{c.gguf_file}")

    return missing_ollama, needs_prefetch


def _list_ollama_tags(base_url: str) -> set[str]:
    r = httpx.get(f"{base_url}/api/tags", timeout=10.0)
    r.raise_for_status()
    return {m["name"] for m in r.json().get("models", [])}


def _gguf_cached(c: Candidate) -> bool:
    """Cheap check: does the HF hub cache already contain this GGUF?"""
    from huggingface_hub import try_to_load_from_cache

    try:
        path = try_to_load_from_cache(repo_id=c.gguf_repo, filename=c.gguf_file)
    except Exception:  # noqa: BLE001
        return False
    return bool(path) and Path(str(path)).exists()


# ── Phase 1: pull / prefetch ──────────────────────────────────────────


def _pull_phase(
    missing_ollama: list[str],
    needs_prefetch: list[Candidate],
    *,
    ollama_url: str,
    skip_pulls: bool,
    skip_prefetch: bool,
    yes: bool,
) -> None:
    if not (missing_ollama or needs_prefetch):
        return

    _section("Phase 1 · weight prep")

    # Ollama pulls
    if missing_ollama:
        if skip_pulls:
            for tag in missing_ollama:
                _say(f"[red]✗[/red] missing ollama tag {tag} — "
                     f"--skip-pulls, aborting")
            raise typer.Exit(2)
        if not yes and not typer.confirm(
            f"Pull {len(missing_ollama)} ollama tag(s): "
            f"{', '.join(missing_ollama)}?",
            default=True,
        ):
            raise typer.Exit(2)
        for tag in missing_ollama:
            _say(f"→ ollama pull {tag} (this can take a while)")
            t0 = time.monotonic()
            r = subprocess.run(  # noqa: S603, S607
                ["ollama", "pull", tag],
                check=False,
            )
            if r.returncode != 0:
                _say(f"[red]✗[/red] ollama pull {tag} exited {r.returncode}")
                raise typer.Exit(2)
            _say(f"[green]✓[/green] {tag} pulled in "
                 f"{time.monotonic() - t0:.0f}s")

    # GGUF prefetch
    if needs_prefetch and not skip_prefetch:
        from huggingface_hub import hf_hub_download

        for c in needs_prefetch:
            _say(f"→ prefetching {c.gguf_repo}/{c.gguf_file} "
                 f"(this is the big one — multi-GB download)")
            t0 = time.monotonic()
            try:
                hf_hub_download(
                    repo_id=c.gguf_repo,
                    filename=c.gguf_file,
                    resume_download=True,
                )
            except Exception as e:  # noqa: BLE001
                _say(f"[red]✗[/red] hf prefetch failed for {c.id}: {e}")
                raise typer.Exit(2) from e
            _say(f"[green]✓[/green] {c.id} GGUF cached in "
                 f"{time.monotonic() - t0:.0f}s")
    elif needs_prefetch and skip_prefetch:
        _say(f"[yellow]![/yellow] --skip-prefetch — first llama-server boot "
             f"will inline-download {len(needs_prefetch)} GGUF(s) "
             f"(may stall the bench)")


# ── Phase 2: sweep ────────────────────────────────────────────────────


def _sweep(
    candidate_ids: str, backends: str, bench: str, limit: int | None,
    ollama_url: str, log_file: Path,
) -> int:
    _section("Phase 2 · sweep")

    cmd = [
        sys.executable, "-u",  # unbuffered, so progress streams promptly
        str(ROOT / "scripts" / "run_ab_benchmark.py"),
        "--candidate", candidate_ids,
        "--backends", backends,
        "--bench", bench,
        "--ollama-url", ollama_url,
    ]
    if limit:
        cmd += ["--limit", str(limit)]

    _say(f"command: {' '.join(cmd)}")
    _say(f"log:     {log_file}")
    started = time.monotonic()

    # Tee child stdout/stderr to both console and log file. SIGINT
    # propagates to the child via shared process group so a Ctrl-C
    # cleanly stops the sweep.
    with log_file.open("w", buffering=1) as logf:
        logf.write(f"# A/B sweep started {dt.datetime.now().isoformat()}\n")
        logf.write(f"# command: {' '.join(cmd)}\n\n")
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                console.print(line, highlight=False)
                logf.write(line + "\n")
        except KeyboardInterrupt:
            _say("[yellow]interrupted — signalling child[/yellow]")
            with suppress(ProcessLookupError):
                proc.send_signal(signal.SIGINT)
        finally:
            rc = proc.wait()

    elapsed = time.monotonic() - started
    _say(f"sweep exited {rc} in {elapsed:.0f}s "
         f"({elapsed / 60:.1f} min)")
    return rc


# ── Phase 3: report ───────────────────────────────────────────────────


def _report() -> None:
    _section("Phase 3 · report")
    report_md = PHASE_DIR / "REPORT.md"
    if not report_md.exists():
        _say(f"[yellow]![/yellow] no REPORT.md — sweep produced no data")
        return
    _say(f"[green]✓[/green] {report_md}")
    console.print()
    console.print(report_md.read_text())


# ── Main ──────────────────────────────────────────────────────────────


def main(
    candidate: str = typer.Option(
        "qwen2.5-7b-instruct,qwen2.5-coder-14b,qwen2.5-32b-instruct",
        "--candidate",
    ),
    backends: str = typer.Option("ollama,llamacpp", "--backends"),
    bench: str = typer.Option(
        "decode_throughput,bfcl_v3,humaneval_plus,luxe_replay", "--bench"
    ),
    limit: int | None = typer.Option(None, "--limit"),
    ollama_url: str = typer.Option(DEFAULT_OLLAMA, "--ollama-url"),
    skip_pulls: bool = typer.Option(False, "--skip-pulls"),
    skip_prefetch: bool = typer.Option(False, "--skip-prefetch"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"sweep-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}.log"

    reg = load_registry()
    candidate_ids = [c.strip() for c in candidate.split(",") if c.strip()]
    backend_kinds = [b.strip() for b in backends.split(",") if b.strip()]
    bench_names = [b.strip() for b in bench.split(",") if b.strip()]

    candidates: list[Candidate] = []
    for cid in candidate_ids:
        try:
            candidates.append(reg.get(cid))
        except KeyError:
            _say(f"[red]✗[/red] unknown candidate: {cid}")
            raise typer.Exit(2) from None

    # Banner
    console.print(Panel.fit(
        "[bold]luxe A/B benchmark[/bold]\n"
        f"candidates: [cyan]{', '.join(candidate_ids)}[/cyan]\n"
        f"backends:   [cyan]{', '.join(backend_kinds)}[/cyan]\n"
        f"benches:    [cyan]{', '.join(bench_names)}[/cyan]\n"
        f"limit:      [cyan]{limit if limit else 'all'}[/cyan]\n"
        f"log:        [dim]{log_file}[/dim]",
        border_style="green",
    ))

    # Estimate total runs (purely informational)
    n_runs = len(candidates) * len(backend_kinds) * len(bench_names)
    _say(f"plan: {n_runs} (candidate × backend × bench) cells "
         f"= roughly {n_runs * (limit or 30) * 2:.0f} model calls")

    overall_t0 = time.monotonic()

    # Phase 0
    missing_ollama, needs_prefetch = _preflight(
        ollama_url, candidates, backend_kinds
    )

    # Phase 1
    _pull_phase(
        missing_ollama, needs_prefetch,
        ollama_url=ollama_url,
        skip_pulls=skip_pulls,
        skip_prefetch=skip_prefetch,
        yes=yes,
    )

    # Phase 2
    rc = _sweep(
        ",".join(candidate_ids), ",".join(backend_kinds), ",".join(bench_names),
        limit, ollama_url, log_file,
    )

    # Phase 3
    _report()

    total = time.monotonic() - overall_t0
    _section("done")
    _say(f"total wall: {total / 60:.1f} min "
         f"({total / 3600:.2f} h)")
    _say(f"full log:   {log_file}")
    if rc != 0:
        _say(f"[yellow]sweep exited {rc} — partial results may still be in "
             f"results/runs/{PHASE_DIR.name}/[/yellow]")
        raise typer.Exit(1)


if __name__ == "__main__":
    typer.run(main)
