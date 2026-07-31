"""`luxe smoke` — the minutes-scale aliveness drill for the fallback kit.

A fallback that isn't exercised is indistinguishable from not having one
(2026-07-29: champion weights silently gone, TUI crash, 210s startup — all
discovered DURING the outage luxe existed for). This drill answers "will this
host actually work right now" without a benchmark: manifest resolved, weights
real on disk, endpoint up, and one real generation + one real tool call on the
main model, plus a generation on the fallback (which exercises the weight
swap). Read-only against the repo; the only side effect is model loads.

Exit code 0 = every step passed (warnings allowed), 1 = at least one FAIL.
Runnable anywhere: `luxe smoke` on each fleet host, and the M4 prep script's
final gate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from luxe.backend import Backend, BackendError

_PING_PROMPT = [{"role": "user",
                 "content": "Reply with exactly: OK"}]
_TOOL_PROMPT = [{"role": "user",
                 "content": "Call the read_file tool on the path "
                            "'README.md'. Do not answer in prose."}]
_TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from the repository.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}]


@dataclass
class SmokeStep:
    name: str
    state: str            # "pass" | "warn" | "fail"
    detail: str = ""
    seconds: float = 0.0


@dataclass
class SmokeReport:
    steps: list[SmokeStep] = field(default_factory=list)

    def add(self, name: str, state: str, detail: str = "",
            seconds: float = 0.0) -> None:
        self.steps.append(SmokeStep(name, state, detail, seconds))

    @property
    def failed(self) -> bool:
        return any(s.state == "fail" for s in self.steps)


def _ping(backend: Backend, model: str, report: SmokeReport,
          label: str) -> bool:
    """One real generation on `model`. The first request pays the weight load
    (oMLX lazy-loads), so the timing here IS the cold-turn number."""
    backend.model = model
    t0 = time.monotonic()
    try:
        resp = backend.chat(_PING_PROMPT, max_tokens=16, temperature=0.0)
    except BackendError as e:
        report.add(label, "fail", f"{model}: {e}",
                   time.monotonic() - t0)
        return False
    dt = time.monotonic() - t0
    if (resp.text or "").strip():
        report.add(label, "pass", f"{model} answered in {dt:.1f}s", dt)
        return True
    report.add(label, "fail",
               f"{model}: empty response (the 'deleted weights' signature "
               "— check `luxe pull --list` for dangling entries)", dt)
    return False


def _tool_ping(backend: Backend, model: str, report: SmokeReport) -> None:
    backend.model = model
    t0 = time.monotonic()
    try:
        resp = backend.chat(_TOOL_PROMPT, tools=_TOOL_SCHEMA,
                            max_tokens=256, temperature=0.0)
    except BackendError as e:
        report.add("tool call", "fail", f"{model}: {e}", time.monotonic() - t0)
        return
    dt = time.monotonic() - t0
    called = any(tc.name == "read_file" for tc in resp.tool_calls)
    if called:
        report.add("tool call", "pass",
                   f"{model} called read_file in {dt:.1f}s", dt)
    else:
        report.add("tool call", "fail",
                   f"{model} produced no tool call (template dropping "
                   "`tools`? see chat/modelcaps.py)", dt)


# --- coding / chat drills (2026-07-30) ---------------------------------------
#
# `luxe smoke` proves the host serves and generates; these prove the AGENTIC
# pipeline: real run_single turns against a planted scratch repo. --code
# exercises the full write path (read → edit → run tests); --chat exercises
# the read-tool conversational path. Both are plumbing proofs, not capability
# tests — the planted bug is deliberately trivial. The scratch repo is kept on
# failure (its path is in the report) and deleted on success.

_DRILL_CALC_BUGGY = (
    "def add(a, b):\n"
    "    return a - b  # planted bug\n"
    "\n"
    "\n"
    "def mul(a, b):\n"
    "    return a * b\n"
)
_DRILL_TEST = (
    "from calc import add, mul\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
    "\n"
    "\n"
    "def test_mul():\n"
    "    assert mul(2, 3) == 6\n"
)
_DRILL_MAGIC = "PLUMBUS-7442"
_DRILL_NOTES = f"Project notes.\nThe magic word is {_DRILL_MAGIC}.\n"

_CODE_GOAL = ("test_calc.py has a failing test. Find the bug in this repo, "
              "fix it, and run pytest to confirm both tests pass.")
_CHAT_GOAL = ("Read the file notes.txt in this repo and reply with ONLY the "
              "magic word it contains.")

# Index-backed tools are withheld (no index is built for a 3-file drill repo);
# the chat drill additionally strips the write surface, mirroring read-only
# chat.
_DRILL_TOOL_DROP = {"bm25_search", "find_symbol"}
_READONLY_DROP = _DRILL_TOOL_DROP | {"write_file", "edit_file", "bash"}


def _resolve_drill_backend(cfg, backend_name: str | None,
                           base_url: str | None):
    """(Backend, model, entry) for a drill. The model comes from the manifest
    of the host the URL POINTS AT — a drill against the m5 must run an m5
    model (it serves the 6-bit pair, not this host's 4-bit pair)."""
    from urllib.parse import urlparse

    from luxe.chat.origin import endpoint_is_local
    from luxe.config import short_hostname

    entry = cfg.backend_entry(backend_name or cfg.default_backend_name())
    url = base_url or entry.base_url
    if endpoint_is_local(url):
        host = short_hostname()
    else:
        host = (urlparse(url).hostname or "").split(".")[0].lower()
    from luxe.secrets import resolve_api_key

    manifest = cfg.host_manifest(host) if host else None
    model = manifest.main if manifest else cfg.model_for_slot("code")
    backend = Backend(base_url=url, model=model,
                      api_key=resolve_api_key(entry.api_key_env),
                      **entry.backend_kwargs())
    return backend, model


def _make_drill_repo(kind: str, files: dict[str, str]):
    import subprocess
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix=f"luxe-{kind}-drill-"))
    for name, content in files.items():
        (root / name).write_text(content)
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=drill@luxe", "-c", "user.name=luxe-drill",
                  "commit", "-qm", "drill: initial state"]):
        subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, timeout=30)
    return root


def _drill_role(cfg, drop: set[str], max_steps: int):
    role = cfg.role("monolith")
    return role.model_copy(update={
        "max_steps": max_steps,
        "temperature": 0.0,
        "tools": [t for t in role.tools if t not in drop],
    })


def _run_drill_turn(backend, role, goal: str, task_type: str, repo, report,
                    label: str):
    """One real run_single turn against the drill repo. Returns the
    AgentResult or None (failure already reported)."""
    from luxe.agents.single import run_single
    from luxe.tools import fs as fs_mod

    prior_root = getattr(fs_mod, "_REPO_ROOT", None)
    fs_mod.set_repo_root(str(repo))
    try:
        # Single-residency policy: a drill must not leave two models loaded
        # (the m5 ended up with both after a drill ran beside a warm model).
        backend.unload_all_loaded(except_for=[backend.model])
    except Exception:
        pass
    t0 = time.monotonic()
    try:
        result = run_single(backend, role, goal=goal, task_type=task_type,
                            run_id=f"smoke-{label}")
    except BackendError as e:
        report.add(f"{label} agent", "fail", f"{e}", time.monotonic() - t0)
        return None
    finally:
        fs_mod._REPO_ROOT = prior_root  # restore, including the None case
    dt = time.monotonic() - t0
    if getattr(result, "aborted", False):
        # run_single contains backend failures into result.aborted — a drill
        # must surface the reason, not grade an empty transcript.
        report.add(f"{label} agent", "fail",
                   f"{backend.model}: aborted — "
                   f"{getattr(result, 'abort_reason', '') or 'no reason'}", dt)
        return None
    report.add(f"{label} agent", "pass",
               f"{backend.model}: {result.steps} step(s), "
               f"{result.tool_calls_total} tool call(s), {dt:.0f}s", dt)
    return result


def run_code_drill(cfg, *, backend_name: str | None = None,
                   base_url: str | None = None) -> SmokeReport:
    """--code: plant a one-line bug + failing test, let the model fix it,
    verify with pytest + git diff OURSELVES (deterministic, model-free)."""
    import subprocess
    import sys

    report = SmokeReport()
    backend, model = _resolve_drill_backend(cfg, backend_name, base_url)
    repo = _make_drill_repo("code", {"calc.py": _DRILL_CALC_BUGGY,
                                     "test_calc.py": _DRILL_TEST})
    report.add("drill repo", "pass", str(repo))

    result = _run_drill_turn(backend, _drill_role(cfg, _DRILL_TOOL_DROP, 12),
                             _CODE_GOAL, "bugfix", repo, report, "code")
    ok = result is not None
    if ok and result.tool_calls_total == 0:
        report.add("tool use", "fail", "the model made no tool calls")
        ok = False

    if ok:
        tests = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                               cwd=str(repo), capture_output=True, text=True,
                               timeout=120)
        if tests.returncode == 0:
            report.add("tests", "pass", "pytest green after the fix")
        else:
            tail = (tests.stdout or tests.stderr).strip().splitlines()[-1:]
            report.add("tests", "fail",
                       f"pytest still failing: {' '.join(tail)}")
            ok = False
        diff = subprocess.run(["git", "-C", str(repo), "diff",
                               "--name-only"],
                              capture_output=True, text=True, timeout=30)
        touched = [f for f in diff.stdout.split() if f]
        if touched == ["calc.py"]:
            report.add("diff", "pass", "exactly calc.py changed")
        elif touched:
            report.add("diff", "warn", f"changed: {', '.join(touched)}")
        else:
            report.add("diff", "fail", "no changes in the working tree")
            ok = False

    if ok and not report.failed:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)
    else:
        report.add("kept", "warn", f"scratch repo left for post-mortem: {repo}")
    return report


def run_chat_drill(cfg, *, backend_name: str | None = None,
                   base_url: str | None = None) -> SmokeReport:
    """--chat: read-only conversational turn that must READ a file to answer
    (the magic word exists only in the repo, never in the prompt)."""
    report = SmokeReport()
    backend, model = _resolve_drill_backend(cfg, backend_name, base_url)
    repo = _make_drill_repo("chat", {"notes.txt": _DRILL_NOTES})
    report.add("drill repo", "pass", str(repo))

    result = _run_drill_turn(backend, _drill_role(cfg, _READONLY_DROP, 8),
                             _CHAT_GOAL, "review", repo, report, "chat")
    ok = result is not None
    if ok:
        if result.tool_calls_total == 0:
            report.add("tool use", "fail", "the model made no tool calls")
            ok = False
        if _DRILL_MAGIC in (result.final_text or ""):
            report.add("answer", "pass", f"magic word recovered ({model})")
        else:
            report.add("answer", "fail",
                       "reply lacks the magic word — it didn't really read "
                       "the file")
            ok = False

    if ok and not report.failed:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)
    else:
        report.add("kept", "warn", f"scratch repo left for post-mortem: {repo}")
    return report


def run_smoke(cfg, *, base_url: str | None = None,
              skip_fallback: bool = False,
              skip_tools: bool = False) -> SmokeReport:
    """Run the drill against `cfg`'s default backend (or `base_url`)."""
    from luxe.chat.origin import endpoint_is_local
    from luxe.config import short_hostname
    from luxe.modelstore import model_state
    from luxe.secrets import resolve_api_key

    report = SmokeReport()

    # 1. Manifest resolution — a typo'd hosts: block dies here, not in an
    #    outage (pydantic silently drops unknown top-level keys).
    manifest = cfg.host_manifest()
    if manifest is None:
        if cfg.hosts:
            report.add("manifest", "fail",
                       f"hosts: has no entry for {short_hostname()!r} — "
                       "add this host to configs/chat.yaml")
            return report
        report.add("manifest", "warn",
                   "no hosts: block — smoking the monolith default")
        main = cfg.model_for_slot("chat")
        fallback = ""
        keep: list[str] = []
    else:
        main, fallback, keep = manifest.main, manifest.fallback, manifest.keep
        report.add("manifest", "pass",
                   f"{short_hostname()}: main {main} · "
                   f"fallback {fallback or '—'}")

    entry = cfg.backend_entry(cfg.default_backend_name())
    url = base_url or entry.base_url
    backend = Backend(base_url=url, model=main,
                      api_key=resolve_api_key(entry.api_key_env),
                      **entry.backend_kwargs())

    # 2. Weights really on disk (local endpoints only — dangling symlinks into
    #    a wiped HF cache list fine and load never).
    if endpoint_is_local(url):
        for mid in [m for m in [main, fallback, *keep] if m]:
            state = model_state(mid)
            if state == "ok":
                report.add(f"weights {mid}", "pass", "on disk")
            else:
                sev = "fail" if mid == main else "warn"
                report.add(f"weights {mid}", sev,
                           f"{state} — `luxe pull {mid}`")

    # 3. Endpoint.
    try:
        healthy = backend.health()
    except Exception as e:
        healthy = False
        detail = str(e)
    else:
        detail = "not responding" if not healthy else url
    if not healthy:
        report.add("endpoint", "fail",
                   f"{url}: {detail} — `brew services restart omlx`")
        return report
    report.add("endpoint", "pass", url)

    # 4. Catalog.
    try:
        served = set(backend.list_models())
    except Exception as e:
        served = set()
        report.add("catalog", "warn", f"list_models failed: {e}")
    if served:
        for mid in [m for m in [main, fallback] if m]:
            if mid not in served:
                report.add("catalog", "fail",
                           f"{mid} not served — restart oMLX after "
                           "provisioning")
        if not any(s.name == "catalog" for s in report.steps):
            report.add("catalog", "pass",
                       f"main + fallback in {len(served)}-model catalog")

    # 5-7. Real generations: main ping, main tool call, fallback ping (the
    #      fallback leg exercises the unload+load swap — it's the slow one).
    #      Single-residency before the first ping too: never leave a host
    #      with two models loaded because something else was warm.
    try:
        backend.unload_all_loaded(except_for=[main])
    except Exception:
        pass
    if _ping(backend, main, report, "main turn") and not skip_tools:
        _tool_ping(backend, main, report)
    if fallback and not skip_fallback:
        try:
            backend.unload_all_loaded(except_for=[fallback])
        except Exception:
            pass
        _ping(backend, fallback, report, "fallback turn")
    return report
