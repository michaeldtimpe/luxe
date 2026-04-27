"""Day-long autonomous test runner — sweep ollama × omlx
× (llamacpp where it matters), capture multi-turn /review wall on
three real repos, configure-and-test DFlash for long-output agents,
run all verdict scripts, walk away. Designed to run unattended for
6–12 hours.

LM Studio was previously a third comparison backend. Dropped 2026-04-27
because Qwen 32B reproducibly loops on identical tool calls inside
long-context multi-subtask agent loops; bug is downstream-fixable only.
Diagnostic probes preserved under scripts/archive/ for future revisits.

Self-recovery: per-phase try/except + signal-based timeout. A failed
phase logs the failure and the runner continues to the next phase.
Persistent state.json is written after every phase transition so a
partial run is still useful.

Resume: pass --resume to skip phases that previously completed
successfully; failed/timeout phases re-run.

Dry run: --dry-run exits cleanly after Phase 0 (preflight).

Usage:

    OMLX_API_KEY=… \\
        uv run python scripts/run_overnight.py

Outputs:
- results/overnight_<ts>/state.json — per-phase timeline
- results/overnight_<ts>/preflight.json — starting-state snapshot
- results/overnight_<ts>/<phase>.log — phase stdout/stderr
- results/runs/overnight_<ts>/<candidate>/<config>/<bench>.jsonl
- results/orchestrator_bench/history.jsonl (real /review records appended)
- results/overnight_<ts>/{OMLX,SPEC_DECODING,COMPOSITE}_VERDICT.{md,csv}
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import typer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPOS = (
    ("elara", "https://github.com/michaeldtimpe/elara"),
    ("never-say-yes", "https://github.com/michaeldtimpe/never-say-yes"),
    ("neon-rain", "https://github.com/michaeldtimpe/neon-rain"),
)

# Model name the review/refactor agents must use, per backend. The
# agents' agents.yaml-declared model ("Qwen2.5-32B-Instruct-4bit") is
# the oMLX-internal tag; Ollama serves the same weights under
# "qwen2.5:32b-instruct". Set as LUXE_MODEL_OVERRIDE before launching
# a /review subprocess so the cross-backend comparison actually
# compares the SAME weights, not whatever happens to live at that tag
# on each backend. None = no override (use the agents.yaml default —
# appropriate for oMLX).
_REVIEW_MODEL_BY_BACKEND: dict[str, str | None] = {
    "omlx": None,
    "ollama": "qwen2.5:32b-instruct",
}

# Per-task wall budget for Phase 3 /review runs. Caps any single run
# so a stuck task can't blow the overnight budget. 90 min covers the
# elara baseline (46 min on Ollama, 70 min on oMLX) with headroom.
REVIEW_TASK_WALL_S = 5400.0

OMLX_BASE_URL = os.environ.get("OMLX_BASE_URL", "http://127.0.0.1:8000")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

# Per-phase CLI filters (set in main()). These narrow what
# multi_turn_reviews runs so a single (repo, backend) chunk can be
# initiated and supervised on its own.
_FILTER_REPO: str | None = None
_FILTER_BACKEND: str | None = None


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _log_line(log_path: Path, msg: str) -> None:
    """Append a single timestamped line to log_path. Used for harness-
    level events (backend probe results, retries) — distinct from
    _shell which logs subprocess invocations."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as f:
        f.write(f"=== {_now()} {msg}\n".encode())


# ── backend liveness ────────────────────────────────────────────────


def _probe_backend(name: str) -> bool:
    """Quick liveness probe. True iff the backend's /v1/models (or
    equivalent) responds with status<500. Used between phases (and
    between (repo, backend) runs) to detect a backend that died after
    preflight passed."""
    import httpx

    try:
        if name == "omlx":
            api_key = os.environ.get("OMLX_API_KEY", "")
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            r = httpx.get(f"{OMLX_BASE_URL}/v1/models", headers=headers, timeout=3.0)
            return r.status_code < 500
        if name == "ollama":
            r = httpx.get(f"{OLLAMA_BASE_URL}/api/version", timeout=3.0)
            return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False
    return False


def _omlx_uptime_s() -> float | None:
    """Best-effort age of the omlx daemon in seconds. None if the
    process can't be located (no psutil, not running under brew, etc.).
    Used by _maybe_proactive_restart to decide whether the daemon has
    been alive long enough to risk hitting the latent Metal
    `gpu::check_error` crash mid-task."""
    try:
        import psutil
    except ImportError:
        return None
    for proc in psutil.process_iter(["name", "cmdline", "create_time"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if any("omlx" in c and "serve" in (proc.info.get("cmdline") or [""]) for c in cmd):
                return time.time() - float(proc.info["create_time"])
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return None


def _maybe_proactive_restart(log_path: Path, max_uptime_s: float = 4 * 3600.0) -> bool:
    """If oMLX has been up longer than max_uptime_s (default 4h),
    `brew services restart omlx` and re-probe. Cheap insurance against
    the latent MLX/Metal crash that has historically taken the daemon
    down mid-multi-turn slot. Returns True if oMLX is up afterward
    (whether or not we restarted), False if the restart left it down."""
    uptime = _omlx_uptime_s()
    if uptime is None:
        _log_line(log_path, "proactive_restart: skipped (uptime unknown)")
        return _probe_backend("omlx")
    if uptime < max_uptime_s:
        _log_line(log_path, f"proactive_restart: skipped (uptime {uptime / 3600:.1f}h < 4h)")
        return _probe_backend("omlx")
    _log_line(log_path, f"proactive_restart: uptime {uptime / 3600:.1f}h ≥ 4h — restarting")
    rc = _shell(["brew", "services", "restart", "omlx"], log_path)
    if rc != 0:
        _log_line(log_path, f"proactive_restart: brew exit={rc} — falling through to wait")
    return _wait_for_backend("omlx", max_wait_s=180, log_path=log_path)


def _wait_for_backend(name: str, max_wait_s: int = 300,
                      log_path: Path | None = None) -> bool:
    """Poll until backend is reachable or max_wait_s elapses. Returns
    True iff the backend is up by the deadline. Polls every 5s — long
    enough to give launchd's KeepAlive time to relaunch a crashed
    OMLX/Ollama and load its primary model (~60-180s typical)."""
    if _probe_backend(name):
        return True
    if log_path is not None:
        _log_line(log_path, f"backend {name} unreachable — waiting up to {max_wait_s}s")
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        time.sleep(5.0)
        if _probe_backend(name):
            elapsed = max_wait_s - int(deadline - time.monotonic())
            if log_path is not None:
                _log_line(log_path, f"backend {name} reachable after {elapsed}s")
            return True
    if log_path is not None:
        _log_line(log_path, f"backend {name} did NOT recover within {max_wait_s}s")
    return False


# ── per-phase timeout ────────────────────────────────────────────────


class PhaseTimeout(RuntimeError):
    pass


@contextmanager
def _timeout(seconds: int):
    """SIGALRM-based timeout for a phase. POSIX-only, fine on macOS.
    Phases that catch BaseException need to NOT swallow this."""

    def _handler(signum, frame):  # noqa: ARG001
        raise PhaseTimeout(f"phase exceeded {seconds}s budget")

    prev = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


# ── persistent state ────────────────────────────────────────────────


def _persist(out_dir: Path, state: dict) -> None:
    (out_dir / "state.json").write_text(json.dumps(state, indent=2, default=str))


def _shell(cmd: list[str], log_path: Path, env: dict | None = None,
           cwd: Path | None = None) -> int:
    """Run a subprocess with output piped to log_path. Returns exit
    code (does not raise on non-zero). Uses the parent's env unless
    `env` is supplied (full replacement, not merge)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as f:
        f.write(f"\n=== {_now()} $ {' '.join(cmd)}\n".encode())
        f.flush()
        proc = subprocess.run(  # noqa: S603
            cmd,
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env or os.environ.copy(),
            cwd=str(cwd) if cwd else None,
        )
        return proc.returncode


# ── Phase 0: preflight ──────────────────────────────────────────────


def run_phase_preflight(out_dir: Path, dry_run: bool = False) -> dict:
    """Verify backends + models + repos, snapshot starting state."""
    log_path = out_dir / "preflight.log"
    snapshot: dict = {"ts": _now(), "checks": {}}

    # 1. Cleanup: disable DFlash on 14B Coder (might have been left
    # enabled from a prior session). Run via subprocess so a failure
    # here doesn't tank the rest of the phase.
    rc = _shell(
        [sys.executable, "scripts/omlx_configure_dflash.py", "disable",
         "--target", "Qwen2.5-Coder-14B-Instruct-MLX-4bit"],
        log_path,
    )
    snapshot["checks"]["dflash_disabled"] = (rc == 0)

    # 2. Per-backend healthcheck. Each script exits 0 on PASS, 1 on FAIL.
    # We capture but don't abort — phases that need a missing backend
    # will skip themselves with a clear note.
    for name, cmd in (
        ("omlx", [sys.executable, "scripts/omlx_healthcheck.py", "--skip-install"]),
    ):
        rc = _shell(cmd, log_path)
        snapshot["checks"][f"{name}_healthcheck"] = (rc == 0)

    # 3. Ollama daemon ping (no script — quick HTTP probe).
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:11434/api/version", timeout=3.0)
        snapshot["checks"]["ollama_alive"] = (r.status_code == 200)
    except Exception as e:  # noqa: BLE001
        snapshot["checks"]["ollama_alive"] = False
        snapshot["checks"]["ollama_error"] = str(e)

    # 4. llama-server binary present.
    import shutil
    snapshot["checks"]["llamacpp_binary"] = bool(shutil.which("llama-server"))

    # 5. Repo presence + clone if missing.
    snapshot["repos"] = {}
    for name, url in REPOS:
        target = ROOT / name
        if (target / ".git").exists():
            snapshot["repos"][name] = {"path": str(target), "status": "present"}
            continue
        try:
            with log_path.open("ab") as f:
                f.write(f"\n=== {_now()} cloning {url}\n".encode())
                f.flush()
                subprocess.run(  # noqa: S603
                    ["git", "clone", "--quiet", url, str(target)],
                    stdout=f, stderr=subprocess.STDOUT, timeout=300, check=True,
                )
            snapshot["repos"][name] = {"path": str(target), "status": "cloned"}
        except Exception as e:  # noqa: BLE001
            snapshot["repos"][name] = {"path": str(target), "status": "failed",
                                       "error": str(e)}

    # 6. Persist preflight snapshot for the report.
    (out_dir / "preflight.json").write_text(json.dumps(snapshot, indent=2, default=str))

    # Hard prereq: at least Ollama OR oMLX must be reachable, OR every
    # phase will skip and the run is wasted.
    backends_ok = sum(
        1 for k in ("ollama_alive", "omlx_healthcheck")
        if snapshot["checks"].get(k)
    )
    if backends_ok == 0:
        raise RuntimeError(
            "no backends reachable — abort. Check that Ollama and/or "
            "oMLX are running with the right env vars set."
        )

    if dry_run:
        return {**snapshot, "dry_run_exit": True}
    return snapshot


# ── Phase 1: synthetic baseline sweep ───────────────────────────────


def run_phase_synthetic_baseline(out_dir: Path) -> dict:
    """run_ab_benchmark across all available backends × candidates ×
    benchmarks. Skip backends marked failed in preflight, then re-
    probe each one and drop any that are down at phase start."""
    pre = json.loads((out_dir / "preflight.json").read_text())
    candidates = []
    if pre["checks"].get("ollama_alive"):
        candidates.append("ollama")
    if pre["checks"].get("omlx_healthcheck"):
        candidates.append("omlx")
    if pre["checks"].get("llamacpp_binary"):
        candidates.append("llamacpp")

    log_path = out_dir / "synthetic_baseline.log"
    backends = []
    for b in candidates:
        if b == "llamacpp":
            backends.append(b)  # binary, not a daemon — no liveness probe
            continue
        if _wait_for_backend(b, max_wait_s=180, log_path=log_path):
            backends.append(b)
        else:
            _log_line(log_path, f"dropping {b} from phase — not reachable")

    if len(backends) < 2:
        return {"skipped": True,
                "reason": f"only {len(backends)} backend(s) live after probe"}

    phase_id = out_dir.name  # e.g. "overnight_2026-04-25T07:00:00"
    cmd = [
        sys.executable, "scripts/run_ab_benchmark.py",
        "--candidate", "qwen2.5-coder-14b,qwen2.5-32b-instruct",
        "--backends", ",".join(backends),
        "--bench", "decode_throughput,humaneval_plus,prefix_cache_decay",
        "--phase", phase_id,
        "--limit", "30",
    ]
    rc = _shell(cmd, log_path)
    return {"backends": backends, "exit_code": rc, "phase_id": phase_id}


# ── Phase 2: spec decoding sweep ────────────────────────────────────


def run_phase_spec_decoding(out_dir: Path) -> dict:
    """DFlash on oMLX + spec on llama-server, both for the 14B Coder."""
    pre = json.loads((out_dir / "preflight.json").read_text())
    phase_id = out_dir.name
    log_path = out_dir / "spec_decoding.log"
    results: dict = {"phase_id": phase_id, "variants": {}}

    # Re-probe oMLX before doing the (long) DFlash sweep. preflight may
    # have run hours ago; oMLX could have crashed since.
    omlx_live = (pre["checks"].get("omlx_healthcheck")
                 and _wait_for_backend("omlx", max_wait_s=180, log_path=log_path))

    # DFlash on oMLX (only if oMLX is reachable AND draft is loaded).
    if omlx_live:
        # Pull draft if missing — idempotent, returns quickly if cached.
        _shell(
            [sys.executable, "scripts/omlx_configure_dflash.py", "pull-draft",
             "--repo", "mlx-community/Qwen2.5-Coder-0.5B-Instruct-4bit", "--no-wait"],
            out_dir / "spec_decoding.log",
        )
        # Enable DFlash.
        _shell(
            [sys.executable, "scripts/omlx_configure_dflash.py", "enable",
             "--target", "Qwen2.5-Coder-14B-Instruct-MLX-4bit",
             "--draft", "Qwen2.5-Coder-0.5B-Instruct-4bit",
             "--quant-bits", "4"],
            out_dir / "spec_decoding.log",
        )
        # Sweep with --config-suffix _dflash.
        rc = _shell(
            [sys.executable, "scripts/run_ab_benchmark.py",
             "--candidate", "qwen2.5-coder-14b",
             "--backends", "omlx",
             "--bench", "decode_throughput,humaneval_plus,prefix_cache_decay",
             "--phase", phase_id,
             "--config-suffix", "_dflash",
             "--limit", "30"],
            out_dir / "spec_decoding.log",
        )
        results["variants"]["omlx_dflash"] = {"exit_code": rc}
        # Disable DFlash to leave the system clean.
        _shell(
            [sys.executable, "scripts/omlx_configure_dflash.py", "disable",
             "--target", "Qwen2.5-Coder-14B-Instruct-MLX-4bit"],
            out_dir / "spec_decoding.log",
        )

    # llama-server +spec via the existing dedicated runner.
    if pre["checks"].get("llamacpp_binary"):
        rc = _shell(
            [sys.executable, "scripts/llamacpp_spec_test.py",
             "--candidate", "qwen2.5-coder-14b",
             "--bench", "decode_throughput,humaneval_plus,prefix_cache_decay",
             "--phase", phase_id,
             "--limit", "30"],
            out_dir / "spec_decoding.log",
        )
        results["variants"]["llamacpp_spec"] = {"exit_code": rc}

    return results


# ── Phase 3: real /review on three repos ────────────────────────────


def run_phase_multi_turn_reviews(out_dir: Path) -> dict:
    """Headless /review on each repo through each healthy backend.
    Caps each run at REVIEW_TASK_WALL_S so a single stuck task
    doesn't blow the overnight budget.

    Honors module-level filters _FILTER_REPO and _FILTER_BACKEND so
    a single (repo, backend) chunk can be initiated and supervised
    on its own — see `--repo` and `--backend` CLI flags."""
    pre = json.loads((out_dir / "preflight.json").read_text())

    # Backends to test for /review. Skip llamacpp — its baseline is
    # too slow per Phase 1 data, not worth the time on multi-turn.
    backends = []
    if pre["checks"].get("ollama_alive"):
        backends.append("ollama")
    if pre["checks"].get("omlx_healthcheck"):
        backends.append("omlx")

    if _FILTER_BACKEND:
        backends = [b for b in backends if b == _FILTER_BACKEND]

    if not backends:
        return {"skipped": True, "reason": "no backends for /review"}

    # start_review_task expects a URL (or a local clone whose `origin`
    # matches a URL passed in). Look the URL up by name from REPOS;
    # the local path is then re-resolved by resolve_repo via
    # find_local_clone scanning cwd. Passing the local path directly
    # makes resolve_repo treat the path as the URL, see that the
    # filesystem entry exists, and bail with "origin does not match".
    url_by_name = {n: u for n, u in REPOS}
    repos = [(n, url_by_name[n]) for n, info in pre.get("repos", {}).items()
             if info.get("status") in ("present", "cloned") and n in url_by_name]
    if _FILTER_REPO:
        repos = [(n, u) for n, u in repos if n == _FILTER_REPO]
    if not repos:
        return {"skipped": True, "reason": "no repos available"}

    log_path = out_dir / "multi_turn_reviews.log"
    results: dict = {"runs": []}

    # Lazy import — only loads luxe code if this phase actually runs.
    from luxe_cli.registry import load_config
    from luxe_cli.review import start_review_task
    from luxe_cli.tasks.model import load as load_task

    cfg = load_config()

    for repo_name, repo_url in repos:
        for backend in backends:
            run_label = f"{repo_name}_{backend}"
            with log_path.open("ab") as f:
                f.write(f"\n=== {_now()} run {run_label} ===\n".encode())

            # Proactive oMLX restart: if the daemon has been up >4h,
            # restart it before this slot. Defends against the latent
            # MLX/Metal `gpu::check_error` crash that has historically
            # taken oMLX down mid-multi-turn — cheaper to spend ~3min
            # on a clean restart than to lose a 90-min /review wall.
            if backend == "omlx":
                _maybe_proactive_restart(log_path)

            # Re-probe THIS backend before each run. Cheap insurance
            # against the overnight 09:15:29 incident where every (repo,
            # backend) launched at the same instant against a backend
            # that had crashed minutes earlier and not yet recovered.
            if not _wait_for_backend(backend, max_wait_s=300, log_path=log_path):
                results["runs"].append({
                    "repo": repo_name, "backend": backend,
                    "status": "backend_unreachable_at_phase_start",
                })
                continue

            # Set LUXE_BACKEND_OVERRIDE (URL) and LUXE_MODEL_OVERRIDE
            # (model tag) so the spawned /review process points at the
            # right backend AND asks for a tag that backend recognises.
            # Without the model override, Ollama returns 404 for the
            # oMLX-internal name in agents.yaml. The child inherits via
            # os.environ.
            prior = os.environ.get("LUXE_BACKEND_OVERRIDE")
            prior_model = os.environ.get("LUXE_MODEL_OVERRIDE")
            try:
                os.environ["LUXE_BACKEND_OVERRIDE"] = backend
                model_override = _REVIEW_MODEL_BY_BACKEND.get(backend)
                if model_override:
                    os.environ["LUXE_MODEL_OVERRIDE"] = model_override
                else:
                    os.environ.pop("LUXE_MODEL_OVERRIDE", None)
                _log_line(log_path, f"{run_label} env: BACKEND={backend} "
                          f"MODEL={model_override or '(default)'}")
                task_id_str = start_review_task(repo_url, "review", cfg)
            except Exception as e:  # noqa: BLE001
                results["runs"].append({
                    "repo": repo_name, "backend": backend,
                    "status": "launch_failed", "error": str(e),
                })
                continue
            finally:
                if prior is None:
                    os.environ.pop("LUXE_BACKEND_OVERRIDE", None)
                else:
                    os.environ["LUXE_BACKEND_OVERRIDE"] = prior
                if prior_model is None:
                    os.environ.pop("LUXE_MODEL_OVERRIDE", None)
                else:
                    os.environ["LUXE_MODEL_OVERRIDE"] = prior_model

            # Patch the task's wall budget to our cap (overrides the
            # repo-survey heuristic which can be too stingy for big
            # repos under a slow backend).
            try:
                t = load_task(task_id_str)
                if t:
                    t.max_wall_s = REVIEW_TASK_WALL_S
                    from luxe_cli.tasks.model import persist as persist_task
                    persist_task(t)
            except Exception:  # noqa: BLE001
                pass

            # Poll state.json until terminal.
            from luxe_cli.tasks.model import TASKS_ROOT
            state_path = TASKS_ROOT / task_id_str / "state.json"
            deadline = time.monotonic() + REVIEW_TASK_WALL_S + 300  # +5min slack
            terminal_status = None
            while time.monotonic() < deadline:
                try:
                    s = json.loads(state_path.read_text())
                    terminal_status = s.get("status")
                    if terminal_status in ("done", "blocked", "aborted"):
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(15)

            # Capture summary into our results.
            try:
                final = json.loads(state_path.read_text())
                wall_s = sum(s.get("wall_s", 0) for s in final.get("subtasks", []))
                done_subs = sum(1 for s in final.get("subtasks", []) if s.get("status") == "done")

                # Sanity guard: terminal status with ~zero wall_s and
                # zero subtasks done means the spawned task gave up
                # immediately. Almost always: the backend died between
                # _wait_for_backend and start_review_task. Re-probe so
                # the result records WHY (backend_died vs task_aborted)
                # — the operator needs that distinction to decide
                # whether to re-run this slot.
                suspect = (wall_s < 60 and done_subs == 0)
                effective_status = final.get("status")
                if suspect:
                    backend_alive_after = _probe_backend(backend)
                    _log_line(log_path, f"{run_label} suspicious "
                              f"(wall_s={wall_s:.0f} done={done_subs}); "
                              f"backend post-run alive={backend_alive_after}")
                    if not backend_alive_after:
                        effective_status = "backend_died_mid_run"

                results["runs"].append({
                    "repo": repo_name, "backend": backend,
                    "task_id": task_id_str,
                    "status": effective_status,
                    "wall_s": round(wall_s, 1),
                    "subtasks_done": done_subs,
                    "subtasks_total": len(final.get("subtasks", [])),
                    "suspect": suspect,
                })
            except Exception as e:  # noqa: BLE001
                results["runs"].append({
                    "repo": repo_name, "backend": backend,
                    "task_id": task_id_str,
                    "status": "state_unreadable", "error": str(e),
                })

    return results


# ── Phase 4: DFlash for writing/calc ────────────────────────────────


def run_phase_dflash_long_output(out_dir: Path) -> dict:
    """DFlash for writing (Gemma 3 27B) and calc (Qwen2.5-32B). Tests
    whether the long-output regime delivers the predicted spec-
    decoding win for these agents."""
    pre = json.loads((out_dir / "preflight.json").read_text())
    log_path = out_dir / "dflash_long_output.log"
    if not pre["checks"].get("omlx_healthcheck"):
        return {"skipped": True, "reason": "oMLX not reachable"}
    if not _wait_for_backend("omlx", max_wait_s=180, log_path=log_path):
        return {"skipped": True, "reason": "oMLX failed phase-start probe"}

    phase_id = out_dir.name
    results: dict = {"variants": {}}

    # Calc agent: 32B-Instruct + 1.5B-Instruct draft. Same family,
    # safe acceptance pattern.
    _shell(
        [sys.executable, "scripts/omlx_configure_dflash.py", "pull-draft",
         "--repo", "mlx-community/Qwen2.5-1.5B-Instruct-4bit", "--no-wait"],
        log_path,
    )
    _shell(
        [sys.executable, "scripts/omlx_configure_dflash.py", "enable",
         "--target", "Qwen2.5-32B-Instruct-4bit",
         "--draft", "Qwen2.5-1.5B-Instruct-4bit",
         "--quant-bits", "4"],
        log_path,
    )
    rc = _shell(
        [sys.executable, "scripts/run_ab_benchmark.py",
         "--candidate", "qwen2.5-32b-instruct",
         "--backends", "omlx",
         "--bench", "decode_throughput",  # long-output bench is the right shape
         "--phase", phase_id,
         "--config-suffix", "_dflash_calc",
         "--limit", "10"],
        log_path,
    )
    results["variants"]["calc_dflash"] = {"exit_code": rc}
    _shell(
        [sys.executable, "scripts/omlx_configure_dflash.py", "disable",
         "--target", "Qwen2.5-32B-Instruct-4bit"],
        log_path,
    )

    # Writing agent: Gemma 3 27B + Gemma 3 1B draft. Best-effort —
    # if Gemma 3 doesn't load cleanly under oMLX we skip with a note.
    _shell(
        [sys.executable, "scripts/omlx_configure_dflash.py", "pull-draft",
         "--repo", "mlx-community/gemma-3-1b-it-4bit", "--no-wait"],
        log_path,
    )
    _shell(
        [sys.executable, "scripts/omlx_configure_dflash.py", "pull-draft",
         "--repo", "mlx-community/gemma-3-27b-it-4bit", "--no-wait"],
        log_path,
    )
    # We don't know if oMLX will serve Gemma 3 with the right chat
    # template. Try; capture exit code; mark variant as inconclusive
    # if it errors. Gemma 3 isn't in the candidates registry as an
    # MLX-served model, so skip the AB run and just probe via curl
    # through the configured endpoint.
    results["variants"]["writing_dflash"] = {
        "status": "deferred",
        "reason": "Gemma 3 27B not in candidates.yaml as MLX-served — "
                  "verify chat-template + tool-call format on oMLX "
                  "manually before adding the candidate + re-running.",
    }
    return results


# ── Phase 5: verdicts + composite report ────────────────────────────


def run_phase_verdicts(out_dir: Path) -> dict:
    """Run all the verdict scripts against the freshly-collected data
    and emit a composite report.

    Pre-step: stitch ~/.luxe/tasks/T-… records into multi_turn_runs.jsonl
    so composite_verdict has the cross-(repo, backend) view it needs.
    state.json's `result.runs` only retains the LAST sub-chunk's record —
    the jsonl is the durable cross-cut."""
    phase_id = out_dir.name
    log_path = out_dir / "verdicts.log"
    results: dict = {"verdicts": {}}

    rc = _shell(
        [sys.executable, "scripts/aggregate_multi_turn.py", "--phase", phase_id],
        log_path,
    )
    results["aggregate_multi_turn"] = {"exit_code": rc}

    for cand in ("qwen2.5-coder-14b", "qwen2.5-32b-instruct"):
        rc = _shell(
            [sys.executable, "scripts/omlx_verdict.py",
             "--phase", phase_id, "--candidate", cand,
             "--out-dir", str(out_dir)],
            log_path,
        )
        results["verdicts"][f"omlx_{cand}"] = {"exit_code": rc}

    rc = _shell(
        [sys.executable, "scripts/spec_decoding_verdict.py",
         "--phase", phase_id, "--candidate", "qwen2.5-coder-14b",
         "--out-dir", str(out_dir)],
        log_path,
    )
    results["verdicts"]["spec_decoding"] = {"exit_code": rc}

    rc = _shell(
        [sys.executable, "scripts/composite_verdict.py",
         "--phase", phase_id, "--out-dir", str(out_dir)],
        log_path,
    )
    results["verdicts"]["composite"] = {"exit_code": rc}

    return results


# ── orchestrator ────────────────────────────────────────────────────


PhaseFn = Callable[[Path], dict]
PhaseEntry = tuple[str, PhaseFn, int]  # (name, fn, max_minutes)

PHASES: list[PhaseEntry] = [
    ("preflight",          lambda d: run_phase_preflight(d, dry_run=False), 10),
    ("synthetic_baseline", run_phase_synthetic_baseline,                    120),
    ("spec_decoding",      run_phase_spec_decoding,                         60),
    ("multi_turn_reviews", run_phase_multi_turn_reviews,                    600),
    ("dflash_long_output", run_phase_dflash_long_output,                    90),
    ("verdicts",           run_phase_verdicts,                              10),
]


def main(
    dry_run: bool = typer.Option(False, "--dry-run",
        help="Run preflight only, exit cleanly without sweeps."),
    skip_phase: str = typer.Option("", "--skip-phase",
        help="Comma-separated phase names to skip."),
    only: str = typer.Option("", "--only",
        help="Comma-separated phase names to run (everything else "
             "skipped). Useful for supervised single-chunk runs. "
             "Requires --resume unless 'preflight' is the only entry."),
    repo: str = typer.Option("", "--repo",
        help="multi_turn_reviews only: restrict to this repo."),
    backend: str = typer.Option("", "--backend",
        help="multi_turn_reviews only: restrict to this backend."),
    resume: str = typer.Option("", "--resume",
        help="Resume from existing results/overnight_<ts>/ directory; "
             "phases marked status=done are skipped."),
) -> None:
    only_set = {s.strip() for s in only.split(",") if s.strip()}
    if only_set and not resume and only_set != {"preflight"}:
        typer.echo("--only requires --resume (preflight.json must already "
                   "exist) unless --only=preflight", err=True)
        sys.exit(2)

    if resume:
        out_dir = ROOT / "results" / resume
        if not out_dir.exists():
            typer.echo(f"--resume target missing: {out_dir}", err=True)
            sys.exit(2)
        state = json.loads((out_dir / "state.json").read_text())
    else:
        ts = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        out_dir = ROOT / "results" / f"overnight_{ts}"
        out_dir.mkdir(parents=True)
        state = {"started_at": _now(), "phases": {}}

    # Apply per-phase filters via module globals (phase fns read these
    # rather than threading an extra arg through every signature).
    global _FILTER_REPO, _FILTER_BACKEND
    _FILTER_REPO = repo or None
    _FILTER_BACKEND = backend or None

    skip_set = {s.strip() for s in skip_phase.split(",") if s.strip()}
    typer.echo(f"=== overnight run: {out_dir} ===")
    typer.echo(f"     dry_run={dry_run} skip={skip_set} only={only_set} "
               f"resume={bool(resume)} repo={_FILTER_REPO} "
               f"backend={_FILTER_BACKEND}")

    for name, fn, max_minutes in PHASES:
        prior = state.get("phases", {}).get(name)
        if only_set and name not in only_set:
            typer.echo(f"  [skip] {name} (--only)")
            continue
        if name in skip_set:
            state["phases"][name] = {"status": "skipped_by_user", "ts": _now()}
            _persist(out_dir, state)
            typer.echo(f"  [skip] {name} (--skip-phase)")
            continue
        # When --only is set the user explicitly wants this phase, so
        # do NOT skip on prior=done. Otherwise honor the resume rule.
        if not only_set and resume and prior and prior.get("status") == "done":
            typer.echo(f"  [skip] {name} (already done, --resume)")
            continue
        if dry_run and name != "preflight":
            state["phases"][name] = {"status": "skipped_dry_run", "ts": _now()}
            _persist(out_dir, state)
            continue

        typer.echo(f"  [run]  {name} (max {max_minutes}min)")
        state["phases"][name] = {"started_at": _now(), "status": "running"}
        _persist(out_dir, state)
        t0 = time.monotonic()

        try:
            with _timeout(max_minutes * 60):
                result = fn(out_dir)
            state["phases"][name].update(
                status="done", finished_at=_now(),
                wall_s=round(time.monotonic() - t0, 1),
                result=result,
            )
        except PhaseTimeout as e:
            state["phases"][name].update(
                status="timeout", finished_at=_now(),
                wall_s=round(time.monotonic() - t0, 1),
                error=str(e),
            )
            typer.echo(f"     ✗ TIMEOUT after {max_minutes}min")
        except Exception as e:  # noqa: BLE001
            state["phases"][name].update(
                status="failed", finished_at=_now(),
                wall_s=round(time.monotonic() - t0, 1),
                error=f"{type(e).__name__}: {e}",
            )
            typer.echo(f"     ✗ FAILED: {type(e).__name__}: {e}")
        else:
            typer.echo(f"     ✓ done in {time.monotonic() - t0:.0f}s")
        _persist(out_dir, state)

    state["finished_at"] = _now()
    _persist(out_dir, state)
    typer.echo(f"\n=== overnight complete — see {out_dir}/state.json ===")
    # Summary
    for name, _, _ in PHASES:
        st = state["phases"].get(name, {}).get("status", "skipped")
        typer.echo(f"  {name}: {st}")


if __name__ == "__main__":
    typer.run(main)
