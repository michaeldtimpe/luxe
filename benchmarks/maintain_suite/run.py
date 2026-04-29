"""Acceptance suite runner — drives `luxe maintain` for each fixture, with
three-layer recovery so an interrupted suite resumes cleanly.

Recovery layers:
  1. Per-fixture state (`acceptance/<id>/state.json`):
     PENDING → RUNNING → DONE | ERROR | SKIPPED. On restart, DONE/SKIPPED
     fixtures are skipped; RUNNING/ERROR/PENDING fixtures execute or resume.
  2. Per-stage checkpoints inside luxe (~/.luxe/runs/<run-id>/stages/):
     when state.luxe_run_id is set and the stage cache has architect/worker_*/
     validator/synthesizer entries, we call `luxe resume` instead of
     `luxe maintain` so worker findings aren't recomputed.
  3. PR-cycle step ledger (~/.luxe/runs/<run-id>/pr_state.json):
     `luxe resume` already replays only the incomplete commit/test/push/
     create/watch_ci steps — no extra wiring needed.

Usage:
  python -m benchmarks.maintain_suite.run --all
  python -m benchmarks.maintain_suite.run --id fix-1 --id fix-2
  python -m benchmarks.maintain_suite.run --all --retry-errors
  python -m benchmarks.maintain_suite.run --force fix-1
  python -m benchmarks.maintain_suite.run --all --dry-run

Outputs under --output (default ./acceptance/):
  <id>/state.json     — current fixture status (resumable)
  <id>/result.json    — FixtureResult once status==DONE
  <id>/diagnostics.json — stage timings, tokens, validator status, etc.
  <id>/stdout.log     — captured luxe stdout
  <id>/stderr.log     — captured luxe stderr
  summary.json        — last-run aggregate
  history.jsonl       — append-only attempt log
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from benchmarks.maintain_suite.grade import (
    Fixture,
    FixtureResult,
    fixture_pass_threshold,
    grade_fixture,
    summarize,
)


# --- per-fixture status ledger --------------------------------------------

class FixtureStatus(str, Enum):
    PENDING = "pending"     # never attempted, or --force
    RUNNING = "running"     # mid-flight (crashed, killed); resume-eligible
    DONE = "done"           # completed (passed or failed grading)
    ERROR = "error"         # runtime error before grading
    SKIPPED = "skipped"     # required_env missing


@dataclass
class FixtureState:
    fixture_id: str
    status: FixtureStatus = FixtureStatus.PENDING
    luxe_run_id: str = ""
    last_attempt_ts: float = 0.0
    attempts: int = 0
    last_error: str = ""
    repo_path_used: str = ""
    base_sha_used: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FixtureState":
        return cls(
            fixture_id=str(d.get("fixture_id", "")),
            status=FixtureStatus(d.get("status", "pending")),
            luxe_run_id=str(d.get("luxe_run_id", "")),
            last_attempt_ts=float(d.get("last_attempt_ts", 0.0)),
            attempts=int(d.get("attempts", 0)),
            last_error=str(d.get("last_error", "")),
            repo_path_used=str(d.get("repo_path_used", "")),
            base_sha_used=str(d.get("base_sha_used", "")),
        )


def _fixture_dir(output: Path, fixture_id: str) -> Path:
    d = output / fixture_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(output: Path, fixture_id: str) -> FixtureState:
    p = _fixture_dir(output, fixture_id) / "state.json"
    if not p.is_file():
        return FixtureState(fixture_id=fixture_id)
    try:
        return FixtureState.from_dict(json.loads(p.read_text()))
    except (json.JSONDecodeError, OSError):
        return FixtureState(fixture_id=fixture_id)


def save_state(output: Path, state: FixtureState) -> None:
    p = _fixture_dir(output, state.fixture_id) / "state.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2))
    tmp.replace(p)


def append_history(output: Path, record: dict) -> None:
    p = output / "history.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(), **record}
    with p.open("a") as f:
        f.write(json.dumps(record) + "\n")


# --- luxe run-dir inspection ----------------------------------------------

def _luxe_run_dir(run_id: str) -> Path:
    return Path.home() / ".luxe" / "runs" / run_id


def _luxe_run_exists(run_id: str) -> bool:
    return run_id and (_luxe_run_dir(run_id) / "run.json").is_file()


def _luxe_completed_stages(run_id: str) -> list[str]:
    sd = _luxe_run_dir(run_id) / "stages"
    if not sd.is_dir():
        return []
    return sorted(p.stem for p in sd.glob("*.json"))


def _luxe_pipeline_complete(run_id: str) -> bool:
    """True if all four expected pipeline stages have checkpoints."""
    stages = set(_luxe_completed_stages(run_id))
    # We don't know how many workers existed without reading the architect
    # checkpoint, but synthesizer is the last stage — its presence means
    # the pipeline reached the end.
    return "synthesizer" in stages


def _luxe_pr_complete(run_id: str) -> bool:
    p = _luxe_run_dir(run_id) / "pr_state.json"
    if not p.is_file():
        return True  # no PR state means the task didn't open a PR (read-only)
    try:
        d = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not d.get("steps"):
        return False
    return all(s.get("done") for s in d["steps"])


def _read_run_artefacts(run_id: str) -> dict[str, Any]:
    """Pull pr_state, citation lint, validator, stage timings, tokens from run dir."""
    rd = _luxe_run_dir(run_id)
    out: dict[str, Any] = {
        "pr_url": "",
        "pr_opened": False,
        "is_draft": False,
        "test_passed": None,
        "citations_unresolved": 0,
        "citations_total": 0,
        "validator_status": "",
        "validator_verified": 0,
        "validator_removed": 0,
        "stages_completed": [],
        "stages_resumed": [],
        "tokens_total": 0,
        "wall_s_total": 0.0,
        "events_kinds": {},
        "backend_failures": [],   # most recent backend errors surfaced from events
    }
    pr_state = rd / "pr_state.json"
    if pr_state.is_file():
        try:
            data = json.loads(pr_state.read_text())
            out["pr_url"] = data.get("pr_url", "") or ""
            out["pr_opened"] = bool(out["pr_url"])
            out["is_draft"] = bool(data.get("is_draft"))
            out["test_passed"] = data.get("test_passed")
        except json.JSONDecodeError:
            pass

    out["stages_completed"] = _luxe_completed_stages(run_id)

    events = rd / "events.jsonl"
    if events.is_file():
        kind_counts: dict[str, int] = {}
        for line in events.read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("kind", "")
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            if kind == "citation_lint_blocked":
                out["citations_unresolved"] = int(ev.get("unresolved", 0))
            elif kind == "citation_lint_passed":
                out["citations_total"] = int(ev.get("count", 0))
            elif kind == "validator_done":
                out["validator_status"] = ev.get("status", "") or ""
                out["validator_verified"] = int(ev.get("verified_count", 0))
                out["validator_removed"] = int(ev.get("removed_count", 0))
            elif kind == "validator_resumed":
                out["stages_resumed"].append("validator")
                out["validator_status"] = ev.get("status", "") or ""
                out["validator_verified"] = int(ev.get("verified_count", 0))
            elif kind in ("architect_resumed", "synthesizer_resumed"):
                out["stages_resumed"].append(kind.replace("_resumed", ""))
            elif kind == "worker_resumed":
                out["stages_resumed"].append(f"worker_{ev.get('index', '?')}")
            elif kind == "finish":
                out["wall_s_total"] = float(ev.get("total_wall_s", 0.0))
            elif kind == "architect_done":
                out["tokens_total"] += int(ev.get("tokens", 0))
            elif kind == "worker_end":
                # tokens not in this event; we'll include them via stages
                pass
            elif kind == "synthesizer_done":
                out["tokens_total"] += int(ev.get("tokens", 0))
        out["events_kinds"] = kind_counts

    # Per-stage tokens come from the stage checkpoints.
    for stage in out["stages_completed"]:
        try:
            sd = json.loads((rd / "stages" / f"{stage}.json").read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out["tokens_total"] += int(sd.get("prompt_tokens", 0))
        out["tokens_total"] += int(sd.get("completion_tokens", 0))
        if stage.startswith("worker_") and isinstance(sd.get("metrics"), dict):
            m = sd["metrics"]
            out["tokens_total"] += int(m.get("prompt_tokens", 0))
            out["tokens_total"] += int(m.get("completion_tokens", 0))

    return out


# --- repo resolution ------------------------------------------------------

def _resolve_repo(fixture: Fixture, work_dir: Path) -> tuple[Path | None, str]:
    """Returns (path, error_message). path is None on failure."""
    if fixture.repo_path:
        p = Path(fixture.repo_path).expanduser().resolve()
        if not p.is_dir():
            return None, f"repo_path not a directory: {p}"
        if fixture.base_sha:
            r = subprocess.run(["git", "checkout", "-q", fixture.base_sha], cwd=p,
                               capture_output=True, text=True, check=False)
            if r.returncode != 0:
                return None, f"git checkout {fixture.base_sha} failed: {r.stderr.strip()}"
        return p, ""
    if fixture.repo_url:
        target = work_dir / f"{fixture.id}-clone"
        if target.exists():
            # Reuse existing clone; checkout base_sha
            if fixture.base_sha:
                r = subprocess.run(["git", "checkout", "-q", fixture.base_sha],
                                   cwd=target, capture_output=True, text=True, check=False)
                if r.returncode != 0:
                    return None, f"git checkout {fixture.base_sha} failed: {r.stderr.strip()}"
            return target, ""
        r = subprocess.run(["git", "clone", "--quiet", fixture.repo_url, str(target)],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            return None, f"git clone failed: {r.stderr.strip()[:200]}"
        if fixture.base_sha:
            r2 = subprocess.run(["git", "checkout", "-q", fixture.base_sha], cwd=target,
                                capture_output=True, text=True, check=False)
            if r2.returncode != 0:
                return None, f"git checkout {fixture.base_sha} failed: {r2.stderr.strip()}"
        return target, ""
    return None, "fixture has neither repo_path nor repo_url"


def _head_sha(repo: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                       capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


# --- subprocess helpers ----------------------------------------------------

def _ensure_luxe_importable() -> None:
    """Fail fast if `luxe` isn't importable from the active Python environment.

    The runner spawns `<sys.executable> -m luxe.cli` per fixture; if luxe isn't
    installed in the same env, every fixture errors with ModuleNotFoundError
    and 0 wall time. Surface this once, up front, with venv guidance.
    """
    try:
        import luxe  # noqa: F401
    except ImportError as e:
        repo_root = Path(__file__).parent.parent.parent
        candidate = repo_root / ".venv" / "bin" / "python"
        msg = [
            f"luxe is not importable from this Python ({sys.executable}).",
            f"  ImportError: {e}",
            "",
            "Activate the project venv first:",
            f"  source {repo_root}/.venv/bin/activate",
            "  python -m benchmarks.maintain_suite.run ...",
            "",
            "Or invoke the venv's python directly:",
            f"  {candidate} -m benchmarks.maintain_suite.run ...",
        ]
        sys.stderr.write("\n".join(msg) + "\n")
        sys.exit(2)


def _run_capture(cmd: list[str], log_dir: Path,
                 env: dict | None = None) -> tuple[int, str, str]:
    """Run cmd; tee stdout/stderr to log files; return (rc, stdout, stderr)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    (log_dir / "stdout.log").write_text(proc.stdout or "")
    (log_dir / "stderr.log").write_text(proc.stderr or "")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _stderr_excerpt(text: str, max_chars: int = 400) -> str:
    """Last few lines of stderr — used in state.last_error so the user sees
    *what* broke without grepping through log files."""
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return "...(truncated) " + text[-max_chars:]


def _diagnose_silent_failure(diag, log_dir: Path) -> list[str]:
    """When luxe ran but produced no work, scan logs for the actual cause and
    return a list of human-readable diagnostic lines for the verdict block."""
    notes: list[str] = []
    if diag.tokens_total == 0 and diag.wall_s < 5.0:
        notes.append(
            f"luxe ran for {diag.wall_s:.1f}s with 0 tokens — model calls "
            "never landed; likely a backend-config issue upstream of luxe"
        )
    # Scan stderr for known failure signatures.
    se = (log_dir / "stderr.log")
    if se.is_file():
        text = se.read_text(errors="replace")
        for pat, msg in [
            ("4xx-401", "oMLX is rejecting auth — set OMLX_API_KEY env var "
                        "and re-run"),
            ("4xx-403", "oMLX returned 403 forbidden — check API key permissions"),
            ("ConnectError", "couldn't reach oMLX — is `brew services start omlx` running?"),
            ("model is loading", "oMLX was still loading models — give it a minute "
                                  "and re-run"),
            ("out of memory", "oMLX hit OOM — model roster may be too large for RAM"),
            ("ModuleNotFoundError", "Python import error — check the .venv setup"),
        ]:
            if pat in text:
                notes.append(msg)
    return notes


def _is_silent_failure(diag) -> bool:
    """Heuristic: luxe terminated 'cleanly' but did no real work.
    Distinguishes a config/auth problem from a genuine grade-failure."""
    return diag.tokens_total == 0 and diag.wall_s < 5.0


def _extract_run_id(text: str) -> str:
    m = re.search(r"run_id=([0-9a-f]{8,})", text)
    return m.group(1) if m else ""


def _luxe_maintain(repo: Path, fixture: Fixture, log_dir: Path
                   ) -> tuple[int, str, str]:
    """Returns (rc, run_id, stderr_excerpt). stderr_excerpt is set when rc != 0."""
    cmd = [
        sys.executable, "-m", "luxe.cli", "maintain",
        str(repo), fixture.goal,
        "--task", fixture.task_type, "--mode", "auto",
        "--yes",  # script-mode; --allow-dirty would still prompt if dirty
    ]
    rc, out, err = _run_capture(cmd, log_dir)
    excerpt = _stderr_excerpt(err) if rc != 0 else ""
    return rc, _extract_run_id(out + err), excerpt


def _luxe_resume(run_id: str, log_dir: Path) -> tuple[int, str]:
    """Returns (rc, stderr_excerpt). stderr_excerpt is set when rc != 0."""
    cmd = [sys.executable, "-m", "luxe.cli", "resume", run_id, "--yes"]
    rc, _, err = _run_capture(cmd, log_dir)
    return rc, _stderr_excerpt(err) if rc != 0 else ""


# --- diagnostics ----------------------------------------------------------

@dataclass
class Diagnostics:
    """Per-fixture diagnostic record — distilled from luxe run artefacts."""
    fixture_id: str
    run_id: str = ""
    wall_s: float = 0.0
    tokens_total: int = 0
    stages_completed: list[str] = field(default_factory=list)
    stages_resumed: list[str] = field(default_factory=list)
    validator_status: str = ""
    validator_verified: int = 0
    validator_removed: int = 0
    citations_unresolved: int = 0
    citations_total: int = 0
    pr_url: str = ""
    pr_opened: bool = False
    is_draft: bool = False
    test_passed: bool | None = None
    events_kinds: dict[str, int] = field(default_factory=dict)


def build_diagnostics(state: FixtureState, artefacts: dict) -> Diagnostics:
    return Diagnostics(
        fixture_id=state.fixture_id,
        run_id=state.luxe_run_id,
        wall_s=float(artefacts.get("wall_s_total", 0.0)),
        tokens_total=int(artefacts.get("tokens_total", 0)),
        stages_completed=list(artefacts.get("stages_completed", [])),
        stages_resumed=list(artefacts.get("stages_resumed", [])),
        validator_status=str(artefacts.get("validator_status", "")),
        validator_verified=int(artefacts.get("validator_verified", 0)),
        validator_removed=int(artefacts.get("validator_removed", 0)),
        citations_unresolved=int(artefacts.get("citations_unresolved", 0)),
        citations_total=int(artefacts.get("citations_total", 0)),
        pr_url=str(artefacts.get("pr_url", "")),
        pr_opened=bool(artefacts.get("pr_opened", False)),
        is_draft=bool(artefacts.get("is_draft", False)),
        test_passed=artefacts.get("test_passed"),
        events_kinds=dict(artefacts.get("events_kinds", {})),
    )


# --- aggregate summary ----------------------------------------------------

def aggregate_diagnostics(diags: list[Diagnostics],
                          results: list[FixtureResult]) -> dict:
    """Produce config-tuning hints from observed run telemetry."""
    if not diags:
        return {}

    avg_wall = sum(d.wall_s for d in diags) / len(diags)
    avg_tokens = sum(d.tokens_total for d in diags) / len(diags)
    n_validator_ambiguous = sum(1 for d in diags if d.validator_status == "ambiguous")
    n_test_failed = sum(1 for d in diags if d.test_passed is False)
    n_citations_blocked = sum(1 for d in diags if d.citations_unresolved > 0)
    n_drafts = sum(1 for d in diags if d.is_draft)

    pass_rate_by_task: dict[str, dict[str, int]] = {}
    for r in results:
        # Look up via fixture_id; we'd need the fixture for task_type but
        # the diagnostic doesn't carry it. We compute pass rate overall.
        pass

    return {
        "fixtures_diagnosed": len(diags),
        "avg_wall_s": round(avg_wall, 1),
        "avg_tokens": int(avg_tokens),
        "validator_ambiguous_count": n_validator_ambiguous,
        "test_failed_count": n_test_failed,
        "citations_blocked_count": n_citations_blocked,
        "draft_pr_count": n_drafts,
        "tuning_hints": _tuning_hints(diags, results),
    }


def _tuning_hints(diags: list[Diagnostics],
                  results: list[FixtureResult]) -> list[str]:
    hints: list[str] = []
    if not diags:
        return hints
    n = len(diags)
    n_amb = sum(1 for d in diags if d.validator_status == "ambiguous")
    if n_amb / n > 0.3:
        hints.append(
            f"validator_status=ambiguous in {n_amb}/{n} fixtures: consider a "
            "stronger validator model or tighter worker prompts to reduce "
            "fabricated citations"
        )
    n_blocked = sum(1 for d in diags if d.citations_unresolved > 0)
    if n_blocked / n > 0.2:
        hints.append(
            f"citation_lint_blocked in {n_blocked}/{n} fixtures: synthesizer "
            "may need stronger 'preserve path:line + snippet' guidance"
        )
    long_runs = [d for d in diags if d.wall_s > 1800]  # > 30 min
    if long_runs:
        hints.append(
            f"{len(long_runs)} fixture(s) ran >30 min: consider raising "
            "worker max_steps cautiously OR reducing scope/decomposition"
        )
    test_failures = [d for d in diags if d.test_passed is False]
    if test_failures:
        hints.append(
            f"{len(test_failures)} fixture(s) had failing tests at PR-open: "
            "draft PRs were opened with test output in the body"
        )
    n_resumed = sum(1 for d in diags if d.stages_resumed)
    if n_resumed:
        hints.append(
            f"{n_resumed} fixture(s) resumed from stage cache — checkpoint "
            "system is exercising itself in production"
        )
    return hints


# --- decision: skip / fresh / resume --------------------------------------

class Decision(str, Enum):
    SKIP_DONE = "skip_done"
    SKIP_REQUIRED_ENV = "skip_required_env"
    SKIP_DRY_RUN = "skip_dry_run"
    RUN_FRESH = "run_fresh"
    RUN_RESUME = "run_resume"


def decide(
    fixture: Fixture, state: FixtureState, *,
    force: bool, retry_errors: bool, retry_skipped: bool,
) -> tuple[Decision, str]:
    if force:
        return Decision.RUN_FRESH, "--force"
    if state.status == FixtureStatus.DONE:
        return Decision.SKIP_DONE, "already done; pass --force to re-run"
    if state.status == FixtureStatus.SKIPPED and not retry_skipped:
        return Decision.SKIP_DONE, f"previously skipped: {state.last_error or 'env'}"
    if state.status == FixtureStatus.ERROR and not retry_errors:
        return Decision.SKIP_DONE, "previous error; pass --retry-errors to retry"
    missing = [v for v in fixture.required_env if not os.environ.get(v)]
    if missing:
        return Decision.SKIP_REQUIRED_ENV, f"missing env: {', '.join(missing)}"
    if state.luxe_run_id and _luxe_run_exists(state.luxe_run_id):
        if _luxe_pipeline_complete(state.luxe_run_id) and \
                _luxe_pr_complete(state.luxe_run_id):
            # Pipeline + PR both done; just grade.
            return Decision.RUN_RESUME, "all stages complete; will only grade"
        return Decision.RUN_RESUME, (
            "resuming from stage cache: "
            f"{','.join(_luxe_completed_stages(state.luxe_run_id)) or '(none)'}"
        )
    return Decision.RUN_FRESH, "new run"


# --- per-fixture orchestration --------------------------------------------

def _load_cached_diag(output: Path, fixture_id: str) -> Diagnostics | None:
    p = _fixture_dir(output, fixture_id) / "diagnostics.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return Diagnostics(**{k: v for k, v in d.items()
                          if k in Diagnostics.__dataclass_fields__})


def _heal_stale_silent_failure(state: FixtureState, output: Path) -> bool:
    """If a previously-DONE fixture's cached diagnostics show a silent
    failure (wall<5s + tokens=0), reclassify the state to ERROR so
    --retry-errors picks it up automatically. Returns True if reclassified.

    This fixes a pre-fix-build state ledger: runs from before the silent-
    failure→ERROR change were saved as DONE, which prevents --retry-errors
    from targeting them. We retro-apply the classification on load.
    """
    if state.status != FixtureStatus.DONE:
        return False
    diag = _load_cached_diag(output, state.fixture_id)
    if diag is None:
        return False
    if diag.tokens_total == 0 and diag.wall_s < 5.0:
        state.status = FixtureStatus.ERROR
        state.last_error = (
            "silent failure detected from cached diagnostics "
            f"(wall={diag.wall_s:.1f}s, tokens=0); reclassified for retry"
        )
        save_state(output, state)
        return True
    return False


def run_fixture(
    fixture: Fixture,
    output: Path,
    work_dir: Path,
    *,
    force: bool = False,
    retry_errors: bool = False,
    retry_skipped: bool = False,
    dry_run: bool = False,
    log: callable = print,
) -> tuple[FixtureResult, Diagnostics]:
    """Execute one fixture with full recovery semantics. Persists state."""
    fdir = _fixture_dir(output, fixture.id)
    state = load_state(output, fixture.id)
    state.fixture_id = fixture.id

    # Self-heal: if the prior run silent-failed but was saved as DONE
    # (pre-fix builds did this), reclassify to ERROR so retry semantics work.
    if _heal_stale_silent_failure(state, output):
        log(f"  ↻ reclassified prior DONE → ERROR (silent failure detected "
            "in cached diagnostics)")

    decision, reason = decide(
        fixture, state,
        force=force, retry_errors=retry_errors, retry_skipped=retry_skipped,
    )
    log(f"  → decision: {decision.value}  ({reason})")
    if dry_run and decision in (Decision.RUN_FRESH, Decision.RUN_RESUME):
        return (FixtureResult(fixture_id=fixture.id, skipped=True,
                              skipped_reason="dry_run"),
                Diagnostics(fixture_id=fixture.id))
    if decision == Decision.SKIP_REQUIRED_ENV:
        state.status = FixtureStatus.SKIPPED
        state.last_error = reason
        save_state(output, state)
        append_history(output, {
            "fixture": fixture.id, "decision": decision.value, "reason": reason,
        })
        return (FixtureResult(fixture_id=fixture.id, skipped=True,
                              skipped_reason=reason),
                Diagnostics(fixture_id=fixture.id))
    if decision == Decision.SKIP_DONE:
        # Re-load result from disk so the summary is consistent.
        rp = fdir / "result.json"
        if rp.is_file():
            try:
                d = json.loads(rp.read_text())
                fr = FixtureResult(**{k: v for k, v in d.items()
                                       if k in FixtureResult.__dataclass_fields__})
            except (json.JSONDecodeError, OSError, TypeError):
                fr = FixtureResult(fixture_id=fixture.id, skipped=True,
                                   skipped_reason=reason)
        else:
            fr = FixtureResult(fixture_id=fixture.id, skipped=True,
                               skipped_reason=reason)
        # Re-load diagnostics if present
        dp = fdir / "diagnostics.json"
        diag = Diagnostics(fixture_id=fixture.id)
        if dp.is_file():
            try:
                dd = json.loads(dp.read_text())
                diag = Diagnostics(**{k: v for k, v in dd.items()
                                       if k in Diagnostics.__dataclass_fields__})
            except (json.JSONDecodeError, OSError, TypeError):
                pass
        return fr, diag

    # RUN_FRESH or RUN_RESUME
    if force:
        state.luxe_run_id = ""  # discard cached run

    repo, err = _resolve_repo(fixture, work_dir)
    if repo is None:
        state.status = FixtureStatus.ERROR
        state.last_error = err
        state.attempts += 1
        state.last_attempt_ts = time.time()
        save_state(output, state)
        append_history(output, {"fixture": fixture.id, "error": err})
        return (FixtureResult(fixture_id=fixture.id, error=err),
                Diagnostics(fixture_id=fixture.id))

    state.repo_path_used = str(repo)
    state.base_sha_used = fixture.base_sha or _head_sha(repo)
    state.attempts += 1
    state.last_attempt_ts = time.time()
    state.status = FixtureStatus.RUNNING
    save_state(output, state)

    # Spawn the right command.
    if decision == Decision.RUN_RESUME:
        log(f"  → invoking `luxe resume {state.luxe_run_id}`")
        rc, err_excerpt = _luxe_resume(state.luxe_run_id, fdir)
        run_id = state.luxe_run_id
        if rc != 0 and err_excerpt:
            log(f"  ! luxe resume rc={rc}: {err_excerpt[:200]}")
    else:
        log(f"  → invoking `luxe maintain` (fresh)")
        rc, run_id, err_excerpt = _luxe_maintain(repo, fixture, fdir)
        if not run_id:
            state.status = FixtureStatus.ERROR
            state.last_error = (
                f"no run_id captured (rc={rc}); stderr: {err_excerpt}"
                if err_excerpt else f"no run_id captured (rc={rc})"
            )
            save_state(output, state)
            append_history(output, {
                "fixture": fixture.id, "rc": rc, "error": state.last_error,
            })
            log(f"  ! {state.last_error[:200]}")
            return (FixtureResult(fixture_id=fixture.id, error=state.last_error),
                    Diagnostics(fixture_id=fixture.id))
        state.luxe_run_id = run_id
        save_state(output, state)

    artefacts = _read_run_artefacts(run_id)
    fr = grade_fixture(
        fixture, repo,
        pr_url=artefacts["pr_url"],
        pr_opened=artefacts["pr_opened"],
        citations_unresolved=artefacts["citations_unresolved"],
        citations_total=artefacts["citations_total"],
        base_sha=state.base_sha_used,
    )
    diag = build_diagnostics(state, artefacts)

    # Persist artefacts even on silent failure — the result.json + diag are
    # useful breadcrumbs.
    (fdir / "result.json").write_text(json.dumps(fr.to_dict(), indent=2))
    (fdir / "diagnostics.json").write_text(json.dumps(asdict(diag), indent=2,
                                                      default=str))

    # State classification: a "silent failure" (wall<5s + tokens=0) means
    # luxe terminated cleanly but never reached the model. We mark it ERROR
    # rather than DONE so `--retry-errors` automatically picks it up next
    # time without the user needing --force.
    if _is_silent_failure(diag):
        notes = _diagnose_silent_failure(diag, fdir)
        state.status = FixtureStatus.ERROR
        state.last_error = (
            "silent failure (luxe never reached the model): "
            + (notes[0] if notes else f"wall={diag.wall_s:.1f}s, tokens=0")
        )
    else:
        state.status = FixtureStatus.DONE
        state.last_error = ""

    save_state(output, state)
    append_history(output, {
        "fixture": fixture.id, "decision": decision.value,
        "rc": rc, "score": fr.score, "passed": fr.passed,
        "run_id": run_id, "wall_s": diag.wall_s,
        "status": state.status.value,
    })
    return fr, diag


# --- top-level driver -----------------------------------------------------

def _load_fixtures(path: Path) -> list[Fixture]:
    raw = yaml.safe_load(path.read_text()) or {}
    return [Fixture.from_dict(d) for d in (raw.get("fixtures") or [])]


def _verdict(r: FixtureResult) -> str:
    if r.error: return "ERROR"
    if r.skipped: return "SKIP"
    if r.passed: return "PASS"
    return "FAIL"


def _describe_outcome(fixture: Fixture) -> str:
    """One-line summary of how the fixture is graded."""
    eo = fixture.expected_outcome
    kind = eo.get("kind", "?")
    if kind == "tests_pass":
        return f"diff non-empty AND `{eo.get('command', '?')}` returns rc=0"
    if kind == "regex_present":
        return f"changed files contain regex `{eo.get('pattern', '?')}`"
    if kind == "regex_absent":
        return f"changed files do NOT contain regex `{eo.get('pattern', '?')}`"
    if kind == "manual_review":
        return f"manual_review: {eo.get('criteria', '')[:70]}"
    return f"unknown outcome kind: {kind}"


def main() -> int:
    _ensure_luxe_importable()
    parser = argparse.ArgumentParser(prog="luxe acceptance suite")
    parser.add_argument("--fixtures", default=None,
                        help="Path to fixtures.yaml (default: alongside this file)")
    parser.add_argument("--output", default="./acceptance",
                        help="Where to write per-fixture state + results")
    parser.add_argument("--id", action="append", default=[],
                        help="Run only this fixture id (repeatable)")
    parser.add_argument("--all", action="store_true",
                        help="Run every fixture in the file")
    parser.add_argument("--force", action="store_true",
                        help="Force-run selected fixtures even if previously DONE; "
                             "discards cached luxe_run_id")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Re-run fixtures whose last status was ERROR")
    parser.add_argument("--retry-skipped", action="store_true",
                        help="Re-run fixtures whose last status was SKIPPED "
                             "(e.g. after setting required env vars)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print decisions without invoking luxe")
    parser.add_argument("--work-dir", default=None,
                        help="Persistent clone dir (default: temp). Reuse to "
                             "avoid re-cloning between invocations")
    args = parser.parse_args()

    fixtures_path = Path(args.fixtures) if args.fixtures else \
        Path(__file__).parent / "fixtures.yaml"
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    fixtures = _load_fixtures(fixtures_path)
    if args.id:
        wanted = set(args.id)
        fixtures = [f for f in fixtures if f.id in wanted]
        unknown = wanted - {f.id for f in fixtures}
        if unknown:
            print(f"unknown fixture id(s): {sorted(unknown)}")
            return 2
    elif not args.all:
        print("No fixtures selected. Pass --all or --id <id>.")
        print(f"Available fixtures in {fixtures_path}:")
        for f in fixtures:
            print(f"  {f.id}\t{f.task_type}\t{f.goal[:60]}")
        return 2
    if not fixtures:
        print("No matching fixtures.")
        return 2

    # Work dir: persistent (clones are reused) or temp (fresh each run)
    cleanup_work_dir = False
    if args.work_dir:
        work_dir = Path(args.work_dir).expanduser().resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        td = tempfile.mkdtemp(prefix="luxe-acceptance-")
        work_dir = Path(td)
        cleanup_work_dir = True

    print(f"\n━━━ luxe acceptance suite")
    print(f"fixtures: {fixtures_path}  ({len(fixtures)} selected)")
    print(f"output:   {output}")
    print(f"work_dir: {work_dir}")

    results: list[FixtureResult] = []
    diags: list[Diagnostics] = []
    try:
        for f in fixtures:
            print(f"\n━━━ {f.id}  [{f.task_type}]  {f.goal[:80]}")
            print(f"      grading: {_describe_outcome(f)}")
            try:
                r, d = run_fixture(
                    f, output, work_dir,
                    force=args.force,
                    retry_errors=args.retry_errors,
                    retry_skipped=args.retry_skipped,
                    dry_run=args.dry_run,
                )
            except KeyboardInterrupt:
                print(f"  [interrupted by user; state preserved]")
                raise
            except Exception as e:
                print(f"  [unexpected error: {type(e).__name__}: {e}]")
                r = FixtureResult(fixture_id=f.id, error=f"{type(e).__name__}: {e}")
                d = Diagnostics(fixture_id=f.id)
            results.append(r)
            diags.append(d)
            # Differentiate cached-skip from a fresh run so warnings/diagnostics
            # below aren't read as live information when they're stale.
            cached_skip = (r.skipped and "already done" in (r.skipped_reason or ""))
            print(f"  {_verdict(r):5s}  score={r.score}/{r.max_score}  "
                  f"wall={d.wall_s:.0f}s  tokens={d.tokens_total}  "
                  f"diff={r.diff_files}f  "
                  f"validator={d.validator_status or '-'}  "
                  f"cite={d.citations_unresolved}/{d.citations_total}"
                  + ("  [cached]" if cached_skip else ""))
            if cached_skip:
                # Cached display: just the score, don't pretend the cached
                # warnings/criteria are from this invocation.
                print(f"        (cached from prior run; --force to re-run)")
                continue
            # Per-criterion breakdown so the verdict reasoning is visible.
            for c in r.criteria_breakdown:
                mark = "✓" if c["earned"] == c["weight"] else (
                    "·" if c["earned"] == 0 and c["weight"] == 0 else "✗"
                )
                print(f"        {mark} {c['criterion']}  "
                      f"({c['earned']}/{c['weight']})  {c['detail'][:90]}")
            if d.stages_resumed:
                print(f"        resumed: {','.join(d.stages_resumed)}")
            # Silent-failure diagnostics for this run only.
            if _is_silent_failure(d) and not r.skipped and not r.error:
                fdir = output / f.id
                for note in _diagnose_silent_failure(d, fdir):
                    print(f"        ⚠ {note}")

        summary = summarize(results)
        summary["diagnostics"] = aggregate_diagnostics(diags, results)

        # Global silent-failure alert: when most fixtures had wall<5s+tokens=0
        # the issue is upstream of luxe (auth, network, oMLX) — surface it
        # ABOVE the per-fixture grades so it's the first thing the user sees.
        attempted = [(r, d) for r, d in zip(results, diags)
                     if not r.skipped and not r.error]
        n_silent = sum(1 for _, d in attempted if _is_silent_failure(d))
        upstream_issue = attempted and n_silent >= max(1, len(attempted) // 2)

        (output / "summary.json").write_text(json.dumps(summary, indent=2))

        print(f"\n━━━ Summary")
        if upstream_issue:
            print(f"  ⚠ {n_silent}/{len(attempted)} attempted fixtures had "
                  "near-zero wall time and zero tokens — luxe never reached "
                  "the model. Likely upstream config issue.")
            # Aggregate diagnostic notes from the silent failures.
            seen: set[str] = set()
            for r, d in attempted:
                if not _is_silent_failure(d):
                    continue
                fdir = output / r.fixture_id
                for note in _diagnose_silent_failure(d, fdir):
                    if note not in seen:
                        seen.add(note)
                        print(f"    → {note}")
            print()
        print(f"  fixtures   : {summary['fixtures']}")
        print(f"  passed     : {summary['passed']}")
        print(f"  failed     : {summary['failed']}")
        print(f"  errored    : {summary['errored']}")
        print(f"  skipped    : {summary['skipped']}")
        print(f"  score      : {summary['score']}/{summary['max_score']}")
        print(f"  v1 release : {'YES' if summary['v1_release_gate'] else 'NO'} "
              f"(needs ≥8 of ≥10 passing)")
        d_agg = summary.get("diagnostics", {})
        if d_agg.get("tuning_hints"):
            print(f"\n  Tuning hints:")
            for h in d_agg["tuning_hints"]:
                print(f"    - {h}")
        return 0 if summary["v1_release_gate"] else 1
    finally:
        if cleanup_work_dir:
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
