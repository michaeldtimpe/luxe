"""Self-repair of a stale oMLX (src/luxe/repair.py) and its call sites.

The 2026-09-11 incident: `luxe smoke` diagnosed a brew-upgraded-underneath
server, printed the runnable fix, and stopped at NOT READY — the fallback
kit was down until a human typed the command it had just printed. These
tests pin that luxe now acts on that ONE signature and refuses to act on
anything else. Everything is simulated: no brew, no lsof, no server.
"""

from __future__ import annotations

import io

import pytest
from click.testing import CliRunner
from rich.console import Console

from luxe import repair as repair_mod
from luxe.staleproc import StaleCheck

STALE_409 = ("oMLX returned 409: {\"error\":{\"message\":\"Model "
             "'Qwen3.6-35B-A3B-4bit' is unavailable after a previous load "
             "failure: VLM load failed: No module named "
             "'transformers.models.qwen3_vl'; LLM fallback also failed: "
             "No module named 'omlx.patches.mlx_lm_mtp'\"}}")


def _stale() -> StaleCheck:
    return StaleCheck(formula="omlx", conclusive=True, stale=True, pid=1166,
                      running_versions=("0.6.3rc3",),
                      installed_versions=("0.6.4",), basis="lsof")


def _fresh() -> StaleCheck:
    return StaleCheck(formula="omlx", conclusive=True, stale=False, pid=2,
                      running_versions=("0.6.4",),
                      installed_versions=("0.6.4",), basis="lsof")


def _mute() -> StaleCheck:
    return StaleCheck(formula="omlx", reason="could not inspect pid 1166")


@pytest.fixture()
def host(monkeypatch):
    """Scripted host: what brew says, what the restart does, what health
    returns, what the process table says before and after."""
    state = {"installed": ("0.6.4",), "restart_ok": True,
             "restart_calls": 0, "health": [False, False, True],
             "after": _fresh(), "local": True}

    monkeypatch.setattr(repair_mod, "_installed_versions",
                        lambda formula: state["installed"])

    def _restart(formula):
        state["restart_calls"] += 1
        return (True, True, "brew services restart ok") if state["restart_ok"] \
            else (True, False, "`brew services restart omlx` exited 1: boom")
    monkeypatch.setattr(repair_mod, "_restart_service", _restart)
    monkeypatch.setattr(repair_mod, "check_omlx", lambda: state["after"])
    monkeypatch.setattr(repair_mod.time, "sleep", lambda s: None)
    from luxe.chat import origin as origin_mod
    monkeypatch.setattr(origin_mod, "endpoint_is_local",
                        lambda url: state["local"])
    repair_mod.reset_cooldown()

    def health():
        seq = state["health"]
        return seq.pop(0) if len(seq) > 1 else seq[0]
    state["health_fn"] = health
    yield state
    repair_mod.reset_cooldown()


def _go(host, **kw):
    kw.setdefault("base_url", "http://127.0.0.1:8000")
    kw.setdefault("health", host["health_fn"])
    return repair_mod.repair_omlx(**kw)


# --- signature --------------------------------------------------------------

@pytest.mark.parametrize("text", [
    STALE_409,
    "VLM load failed: No module named 'transformers.models.qwen3_vl'",
    "[Errno 2] No such file or directory: '.../certifi/cacert.pem'",
    "ImportError: cannot import name 'foo' from 'omlx.patches'",
])
def test_signature_matches_the_incident_bodies(text):
    assert repair_mod.looks_stale(text)


@pytest.mark.parametrize("text", [
    "", "connection refused", "oMLX returned 400: prompt too long",
    "model 'X' not found", "timed out after 1800s with no progress",
])
def test_signature_refuses_ordinary_failures(text):
    assert not repair_mod.looks_stale(text)


# --- the repair --------------------------------------------------------------

def test_restarts_on_process_table_evidence(host):
    res = _go(host, check=_stale())
    assert res.attempted and res.ok
    assert host["restart_calls"] == 1
    assert "brew replaced" in res.reason
    assert any("healthy after" in s for s in res.steps)
    assert any("matches installed" in s for s in res.steps)


def test_restarts_on_error_text_when_lsof_is_mute(host):
    """The 2026-09-11 shape: the 409 body alone must be enough."""
    res = _go(host, check=_mute(), error_text=STALE_409)
    assert res.attempted and res.ok
    assert "names a module" in res.reason


def test_refuses_when_nothing_says_stale(host):
    res = _go(host, check=_fresh(), error_text="connection refused")
    assert not res.attempted
    assert host["restart_calls"] == 0
    assert "not the stale-oMLX signature" in res.reason


def test_force_overrides_signature_but_not_locality(host):
    assert _go(host, check=_fresh(), force=True).attempted
    host["local"] = False
    repair_mod.reset_cooldown()
    res = _go(host, check=_stale(), force=True)
    assert not res.attempted and "not on this machine" in res.reason


def test_refuses_remote_other_engine_and_non_brew(host):
    host["local"] = False
    assert "not on this machine" in _go(host, check=_stale()).reason
    host["local"] = True
    assert "not brew-managed" in _go(host, check=_stale(),
                                     engine="llama-server").reason
    host["installed"] = ()
    assert "not brew-installed" in _go(host, check=_stale()).reason
    assert host["restart_calls"] == 0


def test_never_loops_one_restart_per_cooldown(host):
    first = _go(host, check=_stale())
    assert first.attempted
    host["health"] = [False, False, True]
    second = _go(host, check=_stale())
    assert not second.attempted
    assert "already restarted" in second.reason
    assert host["restart_calls"] == 1


def test_reports_a_restart_that_does_not_come_back(host):
    host["health"] = [False]
    res = _go(host, check=_stale(), wait_s=2)
    assert res.attempted and not res.ok
    assert any("not healthy after" in s for s in res.steps)
    assert "did not recover" in res.detail


def test_reports_a_restart_that_is_still_stale(host):
    host["after"] = _stale()
    res = _go(host, check=_stale())
    assert res.attempted and not res.ok
    assert any("still stale" in s for s in res.steps)


def test_reports_a_failed_brew_call(host):
    host["restart_ok"] = False
    res = _go(host, check=_stale())
    assert res.attempted and not res.ok
    assert host["health"] == [False, False, True]   # never polled
    assert "exited 1" in res.steps[-1]


def test_never_raises(host, monkeypatch):
    monkeypatch.setattr(repair_mod, "_restart_service",
                        lambda f: (_ for _ in ()).throw(RuntimeError("kaboom")))
    res = _go(host, check=_stale())
    assert not res.ok and "repair errored" in res.reason


# --- luxe smoke: repair and re-drill --------------------------------------------

def test_smoke_report_stale_evidence_from_build_line_or_turn_body():
    from luxe.chat.smoke import SmokeReport

    r = SmokeReport()
    r.add("endpoint", "pass", "http://127.0.0.1:8000")
    assert r.stale_evidence == ""
    r.add("main turn", "fail", f"Main-M: {STALE_409}")
    assert "No module named" in r.stale_evidence
    r2 = SmokeReport()
    r2.add("oMLX build", "warn", _stale().detail + " — " + _stale().fix)
    assert "brew replaced" in r2.stale_evidence
    r3 = SmokeReport()
    r3.add("endpoint", "fail", "connection refused — `brew services restart omlx`")
    assert r3.stale_evidence == ""


def _smoke_cli(monkeypatch, tmp_path, reports, repair_result):
    """Drive `luxe smoke` with scripted drill results and a scripted repair."""
    from luxe import cli
    from luxe.chat import smoke as smoke_mod

    cfg_path = tmp_path / "chat.yaml"
    cfg_path.write_text("models:\n  monolith: Champ\nroles:\n  monolith:\n"
                        "    model_key: monolith\n")
    monkeypatch.setattr(cli, "_default_chat_config", lambda: str(cfg_path))
    runs: list[int] = []

    def run_smoke(cfg, **kw):
        runs.append(1)
        return reports[min(len(runs) - 1, len(reports) - 1)]
    monkeypatch.setattr(smoke_mod, "run_smoke", run_smoke)
    repairs: list[str] = []

    def fake_repair(cfg, base_url, evidence, **kw):
        repairs.append(evidence)
        return repair_result
    monkeypatch.setattr(cli, "_smoke_self_repair", fake_repair)

    class _B:
        def __init__(self, *a, **k): ...
        def unload_all_loaded(self, *a, **k): return {}
    import luxe.backend as backend_mod
    monkeypatch.setattr(backend_mod, "Backend", _B)
    return runs, repairs


def _stale_report():
    from luxe.chat.smoke import SmokeReport
    r = SmokeReport()
    r.add("endpoint", "pass", "http://127.0.0.1:8000")
    r.add("oMLX build", "warn", _stale().detail + " — " + _stale().fix)
    r.add("main turn", "fail", f"Main-M: {STALE_409}")
    return r


def _ok_report():
    from luxe.chat.smoke import SmokeReport
    r = SmokeReport()
    r.add("endpoint", "pass", "http://127.0.0.1:8000")
    r.add("oMLX build", "pass", "0.6.4 (matches installed)")
    r.add("main turn", "pass", "Main-M answered in 0.9s")
    return r


def test_smoke_repairs_and_redrills_to_ready(monkeypatch, tmp_path):
    from luxe import cli
    from luxe.repair import RepairResult

    runs, repairs = _smoke_cli(monkeypatch, tmp_path,
                               [_stale_report(), _ok_report()],
                               RepairResult(attempted=True, ok=True,
                                            reason="stale", seconds=9))
    res = CliRunner().invoke(cli.main, ["smoke"])
    assert res.exit_code == 0, res.output
    assert len(runs) == 2 and len(repairs) == 1
    assert "after repair" in res.output
    assert res.output.rstrip().endswith(res.output.rstrip()[-20:]) \
        and "READY" in res.output.splitlines()[-1]


def test_smoke_no_fix_only_diagnoses(monkeypatch, tmp_path):
    from luxe import cli
    from luxe.repair import RepairResult

    runs, repairs = _smoke_cli(monkeypatch, tmp_path, [_stale_report()],
                               RepairResult(attempted=True, ok=True))
    res = CliRunner().invoke(cli.main, ["smoke", "--no-fix"])
    assert res.exit_code == 1
    assert len(runs) == 1 and repairs == []
    assert "NOT READY" in res.output


def test_smoke_does_not_redrill_when_repair_refused(monkeypatch, tmp_path):
    from luxe import cli
    from luxe.repair import RepairResult

    runs, repairs = _smoke_cli(monkeypatch, tmp_path, [_stale_report()],
                               RepairResult(attempted=False, reason="cooldown"))
    res = CliRunner().invoke(cli.main, ["smoke"])
    assert res.exit_code == 1
    assert len(runs) == 1 and len(repairs) == 1
    assert "after repair" not in res.output


def test_smoke_never_repairs_a_non_stale_failure(monkeypatch, tmp_path):
    from luxe import cli
    from luxe.chat.smoke import SmokeReport
    from luxe.repair import RepairResult

    dead = SmokeReport()
    dead.add("endpoint", "fail", "connection refused — `brew services restart omlx`")
    runs, repairs = _smoke_cli(monkeypatch, tmp_path, [dead],
                               RepairResult(attempted=True, ok=True))
    res = CliRunner().invoke(cli.main, ["smoke"])
    assert res.exit_code == 1
    assert repairs == [] and len(runs) == 1


# --- luxe repair / luxe ready --fix -----------------------------------------------

def _cli_cfg(monkeypatch, tmp_path):
    from luxe import cli
    cfg_path = tmp_path / "chat.yaml"
    cfg_path.write_text("models:\n  monolith: Champ\nroles:\n  monolith:\n"
                        "    model_key: monolith\n")
    monkeypatch.setattr(cli, "_default_chat_config", lambda: str(cfg_path))

    class _B:
        def __init__(self, *a, **k): ...
        def health(self): return True
    import luxe.backend as backend_mod
    monkeypatch.setattr(backend_mod, "Backend", _B)


def test_repair_command_exit_codes(monkeypatch, tmp_path):
    from luxe import cli
    from luxe.repair import RepairResult

    _cli_cfg(monkeypatch, tmp_path)
    outcomes = {"r": RepairResult(attempted=False, reason="not stale")}
    monkeypatch.setattr(repair_mod, "repair_omlx", lambda **kw: outcomes["r"])
    res = CliRunner().invoke(cli.main, ["repair"])
    assert res.exit_code == 2 and "no restart" in res.output
    assert "--force" in res.output

    outcomes["r"] = RepairResult(attempted=True, ok=True, reason="stale",
                                 steps=["brew services restart omlx"], seconds=8)
    res = CliRunner().invoke(cli.main, ["repair"])
    assert res.exit_code == 0 and "restarted oMLX" in res.output
    assert "luxe smoke" in res.output

    outcomes["r"] = RepairResult(attempted=True, ok=False, reason="stale",
                                 steps=["not healthy after 60s"])
    res = CliRunner().invoke(cli.main, ["repair"])
    assert res.exit_code == 1 and "omlx.log" in res.output


def test_repair_command_passes_force_through(monkeypatch, tmp_path):
    from luxe import cli
    from luxe.repair import RepairResult

    _cli_cfg(monkeypatch, tmp_path)
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return RepairResult(attempted=True, ok=True)
    monkeypatch.setattr(repair_mod, "repair_omlx", fake)
    CliRunner().invoke(cli.main, ["repair", "--force"])
    assert seen["force"] is True and seen["engine"] == "omlx"


def test_ready_fix_restarts_only_on_a_stale_build_line(monkeypatch, tmp_path):
    from luxe import cli
    from luxe.chat import inspection
    from luxe.repair import RepairResult

    _cli_cfg(monkeypatch, tmp_path)
    docs = [inspection.Doctor(), inspection.Doctor()]
    docs[0].add("endpoint", inspection.OK, "http://127.0.0.1:8000")
    docs[0].add("oMLX build", inspection.WARN, _stale().detail, _stale().fix)
    docs[1].add("endpoint", inspection.OK, "http://127.0.0.1:8000")
    docs[1].add("oMLX build", inspection.OK, "0.6.4 (matches installed)")
    calls = []
    monkeypatch.setattr(cli, "build_ready_doctor",
                        lambda cfg, repo: docs[min(len(calls), 1)])
    monkeypatch.setattr(cli, "_smoke_self_repair",
                        lambda cfg, url, ev, **kw: (calls.append(ev),
                                              RepairResult(attempted=True,
                                                           ok=True))[1])
    res = CliRunner().invoke(cli.main, ["ready", "--fix", "--repo", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert len(calls) == 1 and "after repair" in res.output
    assert "READY" in res.output and "warnings" not in res.output.splitlines()[-2]

    # Not stale → --fix says so and restarts nothing.
    calls.clear()
    monkeypatch.setattr(cli, "build_ready_doctor", lambda cfg, repo: docs[1])
    res = CliRunner().invoke(cli.main, ["ready", "--fix", "--repo", str(tmp_path)])
    assert res.exit_code == 0 and calls == []
    assert "nothing to restart" in res.output


def test_ready_without_fix_never_restarts(monkeypatch, tmp_path):
    from luxe import cli
    from luxe.chat import inspection

    _cli_cfg(monkeypatch, tmp_path)
    doc = inspection.Doctor()
    doc.add("oMLX build", inspection.WARN, _stale().detail, _stale().fix)
    monkeypatch.setattr(cli, "build_ready_doctor", lambda cfg, repo: doc)
    monkeypatch.setattr(cli, "_smoke_self_repair",
                        lambda *a: (_ for _ in ()).throw(AssertionError("restarted")))
    res = CliRunner().invoke(cli.main, ["ready", "--repo", str(tmp_path)])
    assert res.exit_code == 0 and "brew services restart omlx" in res.output


# --- chat: repair before degrade ---------------------------------------------------

class _ManifestBackend:
    served = ["Main-M", "Fb-M"]

    def __init__(self, base_url="", model="", timeout_s=600.0, api_key="", **kw):
        self.base_url = base_url or "http://127.0.0.1:8000"
        self.model = model

    def health(self): return True
    def list_models(self): return list(self.served)
    def unload_all_loaded(self, *, except_for=None): return {}
    def thermal_guard(self, *a, **k): return True


def _manifest_slots(monkeypatch):
    import luxe.config as config_mod
    from luxe.chat import slots as slots_mod
    from luxe.config import HostManifest, PipelineConfig, RoleConfig

    monkeypatch.setattr(slots_mod, "Backend", _ManifestBackend)
    monkeypatch.setattr(config_mod, "short_hostname", lambda: "here")
    cfg = PipelineConfig(models={"monolith": "Champ"},
                         roles={"monolith": RoleConfig(model_key="monolith")},
                         hosts={"here": HostManifest(main="Main-M", fallback="Fb-M")})
    sm = slots_mod.SlotManager(cfg)
    sm.backend.model = "Main-M"
    return sm


def test_slots_repair_fires_before_degrade_on_the_signature(monkeypatch, host):
    from luxe.repair import RepairResult
    sm = _manifest_slots(monkeypatch)
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return RepairResult(attempted=True, ok=True, reason="stale", seconds=7)
    monkeypatch.setattr(repair_mod, "repair_omlx", fake)
    notice = sm.try_self_repair(STALE_409) or sm.note_turn_failure()
    assert "restarted a stale oMLX" in notice and "/retry" in notice
    assert sm.degraded_to is None          # NOT degraded — wrong diagnosis
    assert sm.stats.repairs == 1
    assert seen["engine"] == "omlx" and seen["error_text"] == STALE_409


def test_slots_ordinary_failure_still_degrades(monkeypatch, host):
    sm = _manifest_slots(monkeypatch)
    monkeypatch.setattr(repair_mod, "repair_omlx",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("called")))
    notice = sm.try_self_repair("connection reset") or sm.note_turn_failure()
    assert notice and "Fb-M" in notice
    assert sm.degraded_to == "Fb-M"


def test_slots_refused_repair_falls_through_to_degrade(monkeypatch, host):
    from luxe.repair import RepairResult
    sm = _manifest_slots(monkeypatch)
    monkeypatch.setattr(repair_mod, "repair_omlx",
                        lambda **kw: RepairResult(attempted=False, reason="cooldown"))
    notice = sm.try_self_repair(STALE_409) or sm.note_turn_failure()
    assert notice and "Fb-M" in notice and sm.stats.repairs == 0


def test_slots_failed_repair_reports_and_does_not_degrade(monkeypatch, host):
    from luxe.repair import RepairResult
    sm = _manifest_slots(monkeypatch)
    monkeypatch.setattr(repair_mod, "repair_omlx",
                        lambda **kw: RepairResult(attempted=True, ok=False,
                                                  reason="stale",
                                                  steps=["not healthy after 60s"]))
    notice = sm.try_self_repair(STALE_409)
    assert "did not come back" in notice and "omlx.log" in notice


def test_slots_repair_never_raises(monkeypatch, host):
    sm = _manifest_slots(monkeypatch)
    monkeypatch.setattr(repair_mod, "repair_omlx",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    assert sm.try_self_repair(STALE_409) is None


# --- /repair --------------------------------------------------------------------

def _chat_ctx(monkeypatch):
    from luxe.chat import commands as cmd
    from luxe.chat.session import ChatSession

    sm = _manifest_slots(monkeypatch)
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=100)
    return cmd.CommandContext(console=console, session=ChatSession(), slots=sm), out


def test_slash_repair_is_listed_and_narrates(monkeypatch, host):
    from luxe.chat import commands as cmd
    from luxe.repair import RepairResult

    assert any(row[0] == "/repair" for row in cmd._HELP_ROWS)
    ctx, out = _chat_ctx(monkeypatch)
    monkeypatch.setattr(repair_mod, "repair_omlx",
                        lambda **kw: RepairResult(attempted=True, ok=True,
                                                  reason="stale",
                                                  steps=["brew services restart omlx",
                                                         "endpoint healthy after 6s"],
                                                  seconds=6))
    cmd.dispatch("/repair", ctx)
    text = out.getvalue()
    assert "brew services restart omlx" in text and "restarted oMLX" in text
    assert ctx.slots.stats.repairs == 1


def test_slash_repair_refusal_and_force(monkeypatch, host):
    from luxe.chat import commands as cmd
    from luxe.repair import RepairResult

    ctx, out = _chat_ctx(monkeypatch)
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return RepairResult(attempted=False, reason="not the stale-oMLX signature")
    monkeypatch.setattr(repair_mod, "repair_omlx", fake)
    cmd.dispatch("/repair", ctx)
    assert "no restart" in out.getvalue() and "--force" in out.getvalue()
    assert seen["force"] is False
    cmd.dispatch("/repair --force", ctx)
    assert seen["force"] is True


def test_web_help_row_names_the_browser():
    from luxe.chat import commands as cmd
    row = next(r for r in cmd._HELP_ROWS if r[0] == "/web")
    assert "browser" in row[2] and "web_page" in row[2] and "--web" in row[2]


# --- kit review 2026-09-26 (#7, #15) ---------------------------------------

def test_restart_uses_brew_under_the_prefix_not_bare_path(monkeypatch, tmp_path):
    """`ssh m1 luxe smoke` runs in a non-login shell with no Homebrew on
    PATH; a bare `brew` failed there right after staleproc — which finds the
    prefix itself — proved the formula is installed."""
    brew = tmp_path / "bin" / "brew"
    brew.parent.mkdir()
    brew.write_text("#!/bin/sh\nexit 0\n")
    brew.chmod(0o755)
    monkeypatch.setattr(repair_mod, "_brew_prefix", lambda: str(tmp_path))
    seen = []

    class _Proc:
        returncode, stdout, stderr = 0, "", ""

    def _run(argv, **kw):
        seen.append(argv)
        return _Proc()
    monkeypatch.setattr(repair_mod.subprocess, "run", _run)
    ran, ok, _msg = repair_mod._restart_service("omlx")
    assert ran and ok
    assert seen[0][0] == str(brew)


def test_cooldown_is_not_armed_when_brew_never_ran(host, monkeypatch):
    """A restart that never happened must not lock out the next, correctly
    environed attempt for five minutes."""
    monkeypatch.setattr(repair_mod, "_restart_service",
                        lambda f: (False, False, "`brew` not found"))
    res = _go(host, check=_stale())
    assert res.attempted and not res.ok
    assert repair_mod._in_cooldown() == 0.0
    monkeypatch.setattr(repair_mod, "_restart_service",
                        lambda f: (True, True, "brew services restart ok"))
    assert _go(host, check=_stale()).ok


def test_cooldown_is_armed_once_brew_ran_even_if_it_failed(host):
    host["restart_ok"] = False
    _go(host, check=_stale())
    assert repair_mod._in_cooldown() > 0


def test_one_stale_build_predicate_for_smoke_and_doctor():
    from luxe.chat import inspection

    detail = _stale().detail
    assert repair_mod.is_stale_build_line("oMLX build", "warn", detail)
    assert repair_mod.is_stale_build_line("oMLX build", inspection.WARN, detail)
    assert not repair_mod.is_stale_build_line("oMLX build", "pass", detail)
    assert not repair_mod.is_stale_build_line("endpoint", "warn", detail)
    assert not repair_mod.is_stale_build_line(
        "oMLX build", "warn", "0.6.4 (matches installed)")
