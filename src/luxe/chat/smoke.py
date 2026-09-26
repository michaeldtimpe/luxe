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


#: Liveness-probe deadline (seconds). `Backend.health()` otherwise uses the
#: client's GENERATION read timeout (600s local, 2400s for the m5 entry), so a
#: hung endpoint that accepts the socket could hold the drill's very first
#: step for forty minutes. Same few-second bound `/doctor`'s hint probe uses.
_LIVENESS_TIMEOUT_S = 4.0


@dataclass
class SmokeStep:
    name: str
    state: str            # "pass" | "warn" | "fail"
    detail: str = ""
    seconds: float = 0.0
    model: str = ""       # the model a weights/turn step is ABOUT, if any


@dataclass
class SmokeReport:
    steps: list[SmokeStep] = field(default_factory=list)

    def add(self, name: str, state: str, detail: str = "",
            seconds: float = 0.0, model: str = "") -> None:
        self.steps.append(SmokeStep(name, state, detail, seconds, model))

    @property
    def failed(self) -> bool:
        return any(s.state == "fail" for s in self.steps)

    @property
    def stale_evidence(self) -> str:
        """Why this report points at a stale oMLX, or "" when it doesn't.

        Two independent witnesses (luxe.repair): the `oMLX build` line said
        so (process table), or a failed turn's error body names a module
        the running tree no longer has (the 2026-09-11 409s — matched even
        when lsof is mute). Anything else is NOT a restart case: a dead
        endpoint, a missing model, an empty answer each have their own fix.

        A turn whose model's own `weights` step already failed is NOT a
        witness: `[Errno 2] No such file` is also exactly what loading a
        dangling store entry says, and a restart cannot put weights back on
        disk — the weights line has already named the real fix.
        """
        from luxe.repair import is_stale_build_line, looks_stale

        for s in self.steps:
            if is_stale_build_line(s.name, s.state, s.detail):
                return s.detail
        bad_weights = {s.model for s in self.steps
                       if s.name.startswith("weights") and s.model
                       and s.state != "pass"}
        for s in self.steps:
            if s.state != "fail" or (s.model and s.model in bad_weights):
                continue
            if looks_stale(s.detail):
                return s.detail
        return ""


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
                   time.monotonic() - t0, model=model)
        return False
    dt = time.monotonic() - t0
    if (resp.text or "").strip():
        report.add(label, "pass", f"{model} answered in {dt:.1f}s", dt,
                   model=model)
        return True
    # State the observable, then the discriminator — not a single theory.
    # (2026-08-03: this hint used to assert "the 'deleted weights'
    # signature"; a thinking model burning its whole max_tokens budget in
    # the reasoning channel produces the identical empty response, and the
    # old wording sent that investigation to `luxe pull` instead of the
    # token counter.)
    # Defensive: a hint composer must never raise (test doubles and older
    # ChatResponse shapes may lack timing).
    ct = getattr(getattr(resp, "timing", None), "completion_tokens", "?")
    report.add(label, "fail",
               f"{model}: empty response on HTTP 200 "
               f"(completion_tokens={ct}: at/near the max_tokens cap → "
               "reasoning-channel model burned the budget before answering; "
               "near zero → check `luxe pull --list` for a dangling weights "
               "entry)", dt, model=model)
    return False


def _tool_ping(backend: Backend, model: str, report: SmokeReport) -> None:
    backend.model = model
    t0 = time.monotonic()
    try:
        resp = backend.chat(_TOOL_PROMPT, tools=_TOOL_SCHEMA,
                            max_tokens=256, temperature=0.0)
    except BackendError as e:
        report.add("tool call", "fail", f"{model}: {e}", time.monotonic() - t0,
                   model=model)
        return
    dt = time.monotonic() - t0
    called = any(tc.name == "read_file" for tc in resp.tool_calls)
    if called:
        report.add("tool call", "pass",
                   f"{model} called read_file in {dt:.1f}s", dt)
    else:
        # State the observables; the content-channel check is the
        # discriminator. (2026-08-03: the old "template dropping `tools`?
        # see chat/modelcaps.py" hint anchored the coder investigation on
        # the wrong layer — the template was fine, the model was emitting
        # fenced-JSON tool calls into `content` that nothing parsed.)
        has_prose = bool((resp.text or "").strip())
        hint = (
            "content non-empty — the model may be emitting tool calls in an "
            "unparsed dialect; inspect resp.text against "
            "backend.recover_tool_calls_from_text"
            if has_prose else
            "content ALSO empty — tools may not be reaching the prompt; "
            "see chat/modelcaps.py"
        )
        report.add("tool call", "fail",
                   f"{model} produced no tool call ({hint})", dt)


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
                           base_url: str | None,
                           model_override: str | None = None):
    """(Backend, model, entry) for a drill. The model comes from the manifest
    of the host the URL POINTS AT — a drill against the m5 must run an m5
    model (it serves the 6-bit pair, not this host's 4-bit pair).
    `model_override` (`--model`) drills a specific cached model instead —
    e.g. the m5 capacity model, which is a `keep:`, never a `main`."""
    from luxe.chat.origin import host_for_endpoint

    entry = cfg.backend_entry(backend_name or cfg.default_backend_name())
    url = base_url or entry.base_url
    host = host_for_endpoint(url)

    manifest = cfg.host_manifest(host) if host else None
    if model_override:
        model = model_override
    else:
        model = manifest.main if manifest else cfg.model_for_slot("code")
    backend = entry.build_backend(model, base_url=url, backend_cls=Backend)
    return backend, model


def endpoint_is_shared(cfg, backend_name: str | None = None,
                       base_url: str | None = None) -> bool:
    """True when the drill's target endpoint may be serving other clients.

    Single-residency ("unload everything but mine") is a policy about a box
    luxe OWNS. Applied to a fleet endpoint it evicts other hosts' models
    mid-turn (B5, 2026-07-30) — so every smoke/drill unload asks this first.
    The entry decides (`BackendEntry.is_shared`: explicit `shared:`, else
    loopback = owned); a `--base-url` that points somewhere else than the
    entry is judged by its own host, since the entry's flag describes a
    different server.
    """
    from luxe.backend import is_loopback_url

    entry = cfg.backend_entry(backend_name or cfg.default_backend_name())
    url = base_url or entry.base_url
    if url.rstrip("/") != entry.base_url.rstrip("/"):
        return not is_loopback_url(url)
    return entry.is_shared()


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


#: Step budget for the code drill. 12 was calibrated on the 6-bit champion,
#: which lands the fix and concludes in 5–6 steps.
_CODE_DRILL_STEPS = 12

#: Low-bit quants need headroom. Measured on m1 (2026-08-10,
#: Qwen3.6-35B-A3B-4bit) from ~/.luxe/runs/smoke-code/events.jsonl: the run
#: takes 14 tool calls over 13 steps, because it probes with SEVEN bash calls
#: before editing at step 9, then verifies with three more. The work is
#: correct — pytest green, exactly the target file changed — it simply does
#: not fit in 12 steps, so the drill reported `aborted` and never reached its
#: own tests/diff assertions. The 6-bit on the same host edits sooner and
#: passes in 6. A budget problem, not a capability one: 20 leaves the measured
#: 13 a comfortable margin. Reproducible — 3/3 fail at 12, 2/2 pass at 20.
_CODE_DRILL_STEPS_LOW_BIT = 20

#: Quant markers that select the larger budget. m1/m4 run 4-bit mains by the
#: 2026-07-30 fallback-kit manifest; 2- and 3-bit are listed so a future
#: low-bit main is not a silent regression.
_LOW_BIT_MARKERS = ("-2bit", "-3bit", "-4bit")


def _code_drill_steps(model: str) -> int:
    """Step budget for `model`'s code drill (see the constants above)."""
    name = (model or "").lower()
    if any(marker in name for marker in _LOW_BIT_MARKERS):
        return _CODE_DRILL_STEPS_LOW_BIT
    return _CODE_DRILL_STEPS


def _drill_role(cfg, drop: set[str], max_steps: int):
    role = cfg.role("monolith")
    return role.model_copy(update={
        "max_steps": max_steps,
        "temperature": 0.0,
        "tools": [t for t in role.tools if t not in drop],
    })


def _run_drill_turn(backend, role, goal: str, task_type: str, repo, report,
                    label: str, *, shared: bool = False):
    """One real run_single turn against the drill repo. Returns the
    AgentResult or None (failure already reported)."""
    from luxe.agents.single import run_single
    from luxe.tools import fs as fs_mod

    prior_root = getattr(fs_mod, "_REPO_ROOT", None)
    fs_mod.set_repo_root(str(repo))
    if not shared:
        try:
            # Single-residency policy: a drill must not leave two models
            # loaded (the m5 ended up with both after a drill ran beside a
            # warm model). OWNED endpoints only — on a shared one the other
            # residents are someone else's live session (chat.sdd: a remote
            # drill never unloads that server's models).
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
                   base_url: str | None = None,
                   model: str | None = None) -> SmokeReport:
    """--code: plant a one-line bug + failing test, let the model fix it,
    verify with pytest + git diff OURSELVES (deterministic, model-free)."""
    import subprocess
    import sys

    report = SmokeReport()
    backend, model = _resolve_drill_backend(cfg, backend_name, base_url, model)
    repo = _make_drill_repo("code", {"calc.py": _DRILL_CALC_BUGGY,
                                     "test_calc.py": _DRILL_TEST})
    report.add("drill repo", "pass", str(repo))

    steps = _code_drill_steps(backend.model)
    result = _run_drill_turn(backend,
                             _drill_role(cfg, _DRILL_TOOL_DROP, steps),
                             _CODE_GOAL, "bugfix", repo, report, "code",
                             shared=endpoint_is_shared(cfg, backend_name,
                                                       base_url))
    ok = result is not None
    if ok and result.tool_calls_total == 0:
        report.add("tool use", "fail", "the model made no tool calls")
        ok = False

    if ok:
        try:
            tests = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                                   cwd=str(repo), capture_output=True,
                                   text=True, timeout=120)
        except subprocess.TimeoutExpired:
            # The model can write an infinite loop into calc.py; that is a
            # drill FAILURE to report, not a traceback that loses the table.
            tests = None
        if tests is None:
            report.add("tests", "fail",
                       "pytest did not finish in 120s (the edit hangs?)")
            ok = False
        elif tests.returncode == 0:
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
                   base_url: str | None = None,
                   model: str | None = None) -> SmokeReport:
    """--chat: read-only conversational turn that must READ a file to answer
    (the magic word exists only in the repo, never in the prompt)."""
    report = SmokeReport()
    backend, model = _resolve_drill_backend(cfg, backend_name, base_url, model)
    repo = _make_drill_repo("chat", {"notes.txt": _DRILL_NOTES})
    report.add("drill repo", "pass", str(repo))

    result = _run_drill_turn(backend, _drill_role(cfg, _READONLY_DROP, 8),
                             _CHAT_GOAL, "review", repo, report, "chat",
                             shared=endpoint_is_shared(cfg, backend_name,
                                                       base_url))
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


def check_expected_model(cfg, expected: str, *,
                         base_url: str | None = None,
                         backend_name: str | None = None) -> tuple[bool, str]:
    """Identity preflight: does the target endpoint serve a model whose id
    contains `expected` (case-insensitive)?

    Ported from micro-mind's `bench-run --expect-model` (lessons.md
    2026-08-03): a health check on a port is not an identity check — a
    stale server answered four different "candidate" bake-off runs with
    byte-identical traces from the wrong model. For n-rep acceptance
    nights the same trap silently invalidates every rep, so the preflight
    is one command: `luxe smoke --expect-model <substr> …` before the run.

    Returns (ok, detail) — never raises; an unreachable endpoint is a
    failure here (unlike micro-mind, luxe never spawns its own server, so
    "nothing listening" cannot resolve to the right model later).
    """
    entry = cfg.backend_entry(backend_name or cfg.default_backend_name())
    url = base_url or entry.base_url
    backend = entry.build_backend("", base_url=url, backend_cls=Backend)
    try:
        served = backend.list_models()
    except Exception as e:  # noqa: BLE001 — preflight must report, not raise
        return False, f"endpoint {url} unreachable for identity check: {e}"
    hits = [m for m in served if expected.lower() in m.lower()]
    if hits:
        return True, f"{url} serves {hits[0]}"
    return False, (f"{url} serves {served or ['<nothing>']} — no id contains "
                   f"{expected!r}. A stale or wrong server is answering; "
                   "kill it or fix the backend url.")


def run_smoke(cfg, *, backend_name: str | None = None,
              base_url: str | None = None,
              skip_fallback: bool = False,
              skip_tools: bool = False) -> SmokeReport:
    """Run the drill against `backend_name` (default: the config's default
    entry), optionally at `base_url`.

    DRILL rule (chat.sdd, same as the agentic drills and `luxe ready`): the
    manifest is the one for the host the ENDPOINT points at. Before
    2026-09-26 this function took no backend at all, so `luxe smoke --backend
    m5` from m1 drilled m1's own endpoint with m1's pair and printed READY
    about a server it never touched.
    """
    from luxe.chat import origin as origin_mod
    from luxe.chat.inspection import endpoint_fixes
    from luxe.modelstore import model_state

    report = SmokeReport()
    entry = cfg.backend_entry(backend_name or cfg.default_backend_name())
    url = base_url or entry.base_url
    fixes = endpoint_fixes(entry)
    local = origin_mod.endpoint_is_local(url)
    shared = endpoint_is_shared(cfg, backend_name, base_url)

    # 0. Never a billable target (luxe.sdd cloud carve-out): a drill spends
    #    tokens by design, and a drill that bills is not an aliveness check.
    if entry.is_billable():
        report.add("backend", "fail",
                   f"{backend_name or cfg.default_backend_name()} is a billable "
                   f"{entry.engine_label()} endpoint — never a smoke target; "
                   "drill a local or fleet backend instead")
        return report

    # 1. Manifest resolution — a typo'd hosts: block dies here, not in an
    #    outage (pydantic silently drops unknown top-level keys).
    host = origin_mod.host_for_endpoint(url)
    manifest = cfg.host_manifest(host) if host else None
    if manifest is None:
        if cfg.hosts:
            report.add("manifest", "fail",
                       f"hosts: has no entry for {host or url!r} — "
                       "add that host to the config's hosts: block")
            return report
        report.add("manifest", "warn",
                   "no hosts: block — smoking the monolith default")
        main = cfg.model_for_slot("chat")
        fallback = ""
        keep: list[str] = []
    else:
        main, fallback, keep = manifest.main, manifest.fallback, manifest.keep
        report.add("manifest", "pass",
                   f"{host}: main {main} · fallback {fallback or '—'}")

    backend = entry.build_backend(main, base_url=url, backend_cls=Backend)

    # 2. Weights really on disk (local oMLX only — dangling symlinks into a
    #    wiped HF cache list fine and load never). The oMLX store is the
    #    only disk layout luxe knows: llama-server (neo) loads from its
    #    preset's paths, so reading ~/.omlx/models there reported a working
    #    host's main as missing. Its main turn below is the proof instead.
    if local and entry.is_omlx():
        for mid in [m for m in [main, fallback, *keep] if m]:
            state = model_state(mid)
            if state == "ok":
                report.add(f"weights {mid}", "pass", "on disk", model=mid)
            else:
                sev = "fail" if mid == main else "warn"
                report.add(f"weights {mid}", sev,
                           f"{state} — `luxe pull {mid}`", model=mid)
    elif local:
        report.add("weights", "pass",
                   f"not checked — {entry.engine_label()} loads its own "
                   "preset's files (the main turn below is the proof)")

    # 3. Endpoint. Bounded: this is a liveness question, not a generation.
    try:
        healthy = backend.health(timeout_s=_LIVENESS_TIMEOUT_S)
    except Exception as e:
        healthy = False
        detail, fix = str(e), fixes["start"]
    else:
        detail, fix = ("not responding", fixes["restart"]) if not healthy \
            else (url, "")
    if not healthy:
        report.add("endpoint", "fail", f"{url}: {detail} — {fix}")
        return report
    report.add("endpoint", "pass", url)

    # 3b. Is that endpoint the build brew installed? A server left running
    #     across a `brew upgrade` executes from a deleted Cellar tree: it
    #     passes health and catalog, then fails the next lazy import with an
    #     error naming whatever module it reached for. On 2026-08-04 that was
    #     `No module named 'transformers.models.qwen3_vl'` on the fallback
    #     turn below — which reads as a missing dependency even though the
    #     installed venv had it. WARN, not FAIL: a stale process may still be
    #     serving fine, and the real turns below fail on their own if it is
    #     not. This line exists so that when they do, the cause is already on
    #     screen. Local oMLX only (lessons.md 2026-08-03/04; chat.sdd: skipped
    #     on a non-oMLX engine — there is no formula to be stale about).
    if local and entry.is_omlx():
        try:
            from luxe.staleproc import check_omlx
            stale = check_omlx()
            if stale.conclusive and stale.stale:
                report.add("oMLX build", "warn",
                           f"{stale.detail} — {stale.fix}")
            elif stale.conclusive:
                report.add("oMLX build", "pass", stale.detail)
        except Exception as e:
            report.add("oMLX build", "warn", f"unchecked ({e})")

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
                           f"{mid} not served — {fixes['served']}")
        if not any(s.name == "catalog" for s in report.steps):
            report.add("catalog", "pass",
                       f"main + fallback in {len(served)}-model catalog")

    # 5-7. Real generations: main ping, main tool call, fallback ping (the
    #      fallback leg exercises the unload+load swap — it's the slow one).
    #      Single-residency before the first ping too: never leave a host
    #      with two models loaded because something else was warm. OWNED
    #      endpoints only: on a shared one those residents are other hosts'
    #      live sessions (chat.sdd: remote drills never unload).
    if not shared:
        try:
            backend.unload_all_loaded(except_for=[main])
        except Exception:
            pass
    if _ping(backend, main, report, "main turn") and not skip_tools:
        _tool_ping(backend, main, report)
    if fallback and not skip_fallback:
        if not shared:
            try:
                backend.unload_all_loaded(except_for=[fallback])
            except Exception:
                pass
        _ping(backend, fallback, report, "fallback turn")
    return report
