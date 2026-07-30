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


def run_smoke(cfg, *, base_url: str | None = None,
              skip_fallback: bool = False,
              skip_tools: bool = False) -> SmokeReport:
    """Run the drill against `cfg`'s default backend (or `base_url`)."""
    import os

    from luxe.chat.origin import endpoint_is_local
    from luxe.config import short_hostname
    from luxe.modelstore import model_state

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
    backend = Backend(base_url=url, model=main, timeout_s=entry.timeout_s,
                      api_key=os.environ.get(entry.api_key_env, ""))

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
    if _ping(backend, main, report, "main turn") and not skip_tools:
        _tool_ping(backend, main, report)
    if fallback and not skip_fallback:
        try:
            backend.unload_all_loaded(except_for=[fallback])
        except Exception:
            pass
        _ping(backend, fallback, report, "fallback turn")
    return report
