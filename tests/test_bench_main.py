"""benchmarks/maintain_suite/run.py main() — the bench driver's contract.

main() is the outer loop every maintain_suite number passes through: fixture
selection, the origin preflight, the work-dir choice, the variant plan, the
per-run progress/ETA and verdict lines, summary.json / comparison.json, the
exit code and the bench-end unload. These pin it with `run_fixture` stubbed
(no model, no clone) and a fake clock, so the console lines are exact.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import benchmarks.maintain_suite.run as br
from benchmarks.maintain_suite.grade import FixtureResult
from benchmarks.maintain_suite.run import Diagnostics


class _Clock:
    """`time` stand-in for run.py: each monotonic() read advances 65s, so
    every run takes 1:05; the wall clock always reads 12:00:00."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        self.t += 65.0
        return self.t

    @staticmethod
    def strftime(_fmt, _t=None):
        return "12:00:00"

    @staticmethod
    def localtime():
        return None


class _UnloadBackend:
    """luxe.backend.Backend stand-in for the bench-end unload."""
    calls: list[str] = []
    fail = False

    def __init__(self, model):
        self.model = model

    def unload_all_loaded(self):
        _UnloadBackend.calls.append(self.model)
        if _UnloadBackend.fail:
            raise RuntimeError("server gone")
        return {"m1": True, "m2": False}


def _fixtures_yaml(tmp_path: Path, ids=("a", "b"), *,
                   repo_url="https://github.com/x/y") -> Path:
    # A remote repo_url: origin_problem only inspects local origins.
    lines = ["fixtures:"]
    for fid in ids:
        lines += [f"  - id: {fid}",
                  f"    repo_url: {repo_url}",
                  f"    goal: goal for {fid}",
                  "    task_type: implement",
                  "    expected_outcome: {kind: regex_present, pattern: x}"]
    p = tmp_path / "fixtures.yaml"
    p.write_text("\n".join(lines) + "\n")
    return p


def _pass(fid: str, **diag) -> tuple[FixtureResult, Diagnostics]:
    r = FixtureResult(fixture_id=fid, score=5, pr_opened=True,
                      diff_produced=True, diff_files=2)
    d = Diagnostics(fixture_id=fid, wall_s=40.0, tokens_total=1500,
                    prompt_tokens=1200, completion_tokens=300)
    for k, v in diag.items():
        setattr(d, k, v)
    return r, d


@pytest.fixture
def bench(tmp_path, monkeypatch, capsys):
    """Drive main(). `bench(argv, outcomes)` returns (rc, stdout, calls);
    outcomes maps fixture id (or "<variant_id>/<id>") to (result, diag) or
    an exception to raise; unmapped fixtures pass cleanly. `bench.calls`
    and `bench.events` ("run:<id>" per call) survive a raising main()."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LUXE_BENCH_COLD_CACHE", raising=False)
    monkeypatch.setattr(br, "_ensure_luxe_importable", lambda: None)
    monkeypatch.setattr(br, "time", _Clock())
    monkeypatch.setattr("luxe.backend.Backend", _UnloadBackend)
    _UnloadBackend.calls = []
    _UnloadBackend.fail = False
    calls: list[dict] = []
    events: list[str] = []

    def drive(argv, outcomes=None):
        outcomes = outcomes or {}
        calls.clear()

        def fake_run_fixture(f, output, work_dir, **kw):
            calls.append({"id": f.id, "output": output,
                          "work_dir": work_dir, **kw})
            events.append(f"run:{f.id}")
            v = kw.get("variant")
            out = outcomes.get(f"{v.variant_id}/{f.id}" if v else f.id,
                               outcomes.get(f.id))
            if isinstance(out, BaseException):
                raise out
            return out if out is not None else _pass(f.id)

        monkeypatch.setattr(br, "run_fixture", fake_run_fixture)
        monkeypatch.setattr(sys, "argv", ["run", *argv])
        rc = br.main()
        return rc, capsys.readouterr().out, calls

    drive.calls = calls
    drive.events = events
    return drive


def _base_argv(tmp_path, fixtures: Path) -> list[str]:
    return ["--fixtures", str(fixtures), "--output", str(tmp_path / "acc"),
            "--work-dir", str(tmp_path / "wd")]


# --- selection ---------------------------------------------------------------

def test_no_selection_lists_fixtures_and_exits_2(tmp_path, bench):
    p = _fixtures_yaml(tmp_path)
    rc, out, calls = bench(["--fixtures", str(p),
                            "--output", str(tmp_path / "acc")])
    assert rc == 2 and calls == []
    assert out == ("No fixtures selected. Pass --all or --id <id>.\n"
                   f"Available fixtures in {p}:\n"
                   "  a\timplement\tgoal for a\n"
                   "  b\timplement\tgoal for b\n")


def test_unknown_id_exits_2(tmp_path, bench):
    p = _fixtures_yaml(tmp_path)
    rc, out, calls = bench([*_base_argv(tmp_path, p), "--id", "a",
                            "--id", "zz"])
    assert rc == 2 and calls == []
    assert out == "unknown fixture id(s): ['zz']\n"


def test_id_runs_only_the_named_fixtures_in_file_order(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a", "b", "c"))
    rc, _out, calls = bench([*_base_argv(tmp_path, p), "--id", "c",
                             "--id", "a"])
    assert [c["id"] for c in calls] == ["a", "c"]


def test_origin_problem_refuses_but_dry_run_proceeds(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a",),
                       repo_url=str(tmp_path / "missing-origin"))
    rc, out, calls = bench([*_base_argv(tmp_path, p), "--all", "--dry-run"])
    assert out.startswith("origin preflight failed:\n  - ")
    assert [c["id"] for c in calls] == ["a"]
    assert calls[0]["dry_run"] is True


# --- work dir + flags ---------------------------------------------------------

def test_default_work_dir_is_the_pinned_bench_workspace(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a",))
    _rc, out, calls = bench(["--fixtures", str(p),
                             "--output", str(tmp_path / "acc"), "--all"])
    pinned = (tmp_path / "home" / ".luxe" / "bench-workspace").resolve()
    assert calls[0]["work_dir"] == pinned and pinned.is_dir()
    assert f"work_dir: {pinned}\n" in out


def test_ephemeral_work_dir_is_removed_at_exit(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a",))
    _rc, _out, calls = bench(["--fixtures", str(p), "--all",
                              "--output", str(tmp_path / "acc"),
                              "--ephemeral-work-dir"])
    wd = calls[0]["work_dir"]
    assert wd.name.startswith("luxe-acceptance-")
    assert not wd.exists()


def test_flags_pass_through_to_run_fixture(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a",))
    _rc, _out, calls = bench([*_base_argv(tmp_path, p), "--all", "--force",
                              "--retry-errors", "--retry-skipped",
                              "--dry-run", "--per-fixture-timeout", "1200"])
    c = calls[0]
    assert c["output"] == tmp_path / "acc"
    assert c["work_dir"] == (tmp_path / "wd").resolve()
    assert (c["force"], c["retry_errors"], c["retry_skipped"],
            c["dry_run"]) == (True, True, True, True)
    assert c["per_fixture_timeout_s"] == 1200.0
    assert c["variant"] is None and c["overlay_dir"] is None


def test_flags_default_off(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a",))
    _rc, _out, calls = bench([*_base_argv(tmp_path, p), "--all"])
    c = calls[0]
    assert (c["force"], c["retry_errors"], c["retry_skipped"],
            c["dry_run"], c["per_fixture_timeout_s"]) == (
        False, False, False, False, None)


# --- per-run lines --------------------------------------------------------------

def test_progress_eta_and_verdict_lines(tmp_path, bench):
    """ETA averages FRESH runs only — a cached replay costs no wall."""
    p = _fixtures_yaml(tmp_path, ids=("a", "b", "c", "d"))
    cached_r, cached_d = _pass("a")
    cached_d.cached = True
    _rc, out, _calls = bench([*_base_argv(tmp_path, p), "--all"],
                             {"a": (cached_r, cached_d)})
    lines = out.splitlines()
    runs = [ln for ln in lines if ln.startswith("━━━ run ")]
    assert runs == [
        "━━━ run 1/4  [12:00:00]  a  [implement]  goal for a",
        "━━━ run 2/4  [12:00:00]  b  [implement]  goal for b",
        "━━━ run 3/4  [12:00:00]  ETA ~2min  c  [implement]  goal for c",
        "━━━ run 4/4  [12:00:00]  ETA ~1min  d  [implement]  goal for d",
    ]
    assert ("  PASS   [12:00:00 +1:05]  score=5/5  wall=40s  "
            "tokens=1200/300/1500  gen_tps~8  diff=2f  validator=-  "
            "cite=0/0") in lines
    assert ("  PASS   [12:00:00 +1:05]  score=5/5  wall=40s  "
            "tokens=1200/300/1500  gen_tps~8  diff=2f  validator=-  "
            "cite=0/0  [cached]") in lines
    assert "        (cached from prior run; --force to re-run)" in lines
    assert "      grading: changed files contain regex `x`" in lines


def test_fresh_run_prints_breakdown_and_notes(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a",))
    r = FixtureResult(fixture_id="a", score=1, criteria_breakdown=[
        {"criterion": "pr_opened", "earned": 1, "weight": 1, "detail": "ok"},
        {"criterion": "outcome", "earned": 0, "weight": 3, "detail": "no"},
        {"criterion": "note", "earned": 0, "weight": 0, "detail": "info"},
    ])
    d = Diagnostics(fixture_id="a", wall_s=30.0, tokens_total=10,
                    stages_resumed=["plan", "work"],
                    single_mode={"tool_calls_total": 0,
                                 "final_text_chars": 120},
                    events_kinds={"step_limit_incomplete": 1})
    _rc, out, _calls = bench([*_base_argv(tmp_path, p), "--all"],
                             {"a": (r, d)})
    lines = out.splitlines()
    i = lines.index("        ✓ pr_opened  (1/1)  ok")
    assert lines[i + 1] == "        ✗ outcome  (0/3)  no"
    # earned == weight is tested first, so a 0/0 row reads ✓ (the "·" arm
    # is unreachable); pinned as-is.
    assert lines[i + 2] == "        ✓ note  (0/0)  info"
    assert lines[i + 3] == "        resumed: plan,work"
    assert lines[i + 4].startswith("        ⓘ single mode: model emitted 120")
    assert lines[i + 5].startswith("        ⚠ stopped at the step limit")


def test_unexpected_error_is_recorded_and_bench_continues(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a", "b"))
    rc, out, calls = bench([*_base_argv(tmp_path, p), "--all"],
                           {"a": RuntimeError("boom")})
    assert [c["id"] for c in calls] == ["a", "b"]
    assert "  [unexpected error: RuntimeError: boom]" in out.splitlines()
    summary = json.loads((tmp_path / "acc" / "summary.json").read_text())
    assert (summary["errored"], summary["passed"]) == (1, 1)
    assert rc == 1


def test_keyboard_interrupt_propagates_after_unload_and_cleanup(
        tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a", "b"))
    with pytest.raises(KeyboardInterrupt):
        bench(["--fixtures", str(p), "--all",
               "--output", str(tmp_path / "acc"), "--ephemeral-work-dir"],
              {"a": KeyboardInterrupt()})
    assert [c["id"] for c in bench.calls] == ["a"]
    assert not bench.calls[0]["work_dir"].exists()
    assert _UnloadBackend.calls == ["(bench-end-unload)"]
    assert not (tmp_path / "acc" / "summary.json").exists()


# --- summary, exit code, unload ---------------------------------------------------

def test_release_gate_sets_exit_code(tmp_path, bench):
    ids = tuple(f"f{i}" for i in range(10))
    p = _fixtures_yaml(tmp_path, ids=ids)
    rc, out, _calls = bench([*_base_argv(tmp_path, p), "--all"])
    assert rc == 0
    assert "  v1 release : YES (needs ≥8 of ≥10 passing)" in out.splitlines()

    fail = FixtureResult(fixture_id="f0", score=2)
    rc, out, _calls = bench([*_base_argv(tmp_path, p), "--all", "--force"],
                            {i: (fail, Diagnostics(fixture_id=i, wall_s=9.0))
                             for i in ids[:3]})
    assert rc == 1
    assert "  v1 release : NO (needs ≥8 of ≥10 passing)" in out.splitlines()


def test_summary_block_and_files(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a",))
    _rc, out, _calls = bench([*_base_argv(tmp_path, p), "--all"])
    summary = json.loads((tmp_path / "acc" / "summary.json").read_text())
    assert summary["passed"] == 1
    assert summary["diagnostics"]["fixtures_diagnosed"] == 1
    tail = out.split("━━━ Summary\n", 1)[1].splitlines()
    assert tail[:7] == [
        "  fixtures   : 1", "  passed     : 1", "  failed     : 0",
        "  errored    : 0", "  skipped    : 0", "  score      : 5/5",
        "  v1 release : NO (needs ≥8 of ≥10 passing)",
    ]
    assert not (tmp_path / "acc" / "comparison.json").exists()


def test_upstream_alert_when_most_runs_were_silent(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a", "b"))
    (tmp_path / "acc" / "a").mkdir(parents=True)
    (tmp_path / "acc" / "a" / "stderr.log").write_text("httpx.ConnectError\n")
    silent = {i: (FixtureResult(fixture_id=i),
                  Diagnostics(fixture_id=i, wall_s=1.0)) for i in ("a", "b")}
    _rc, out, _calls = bench([*_base_argv(tmp_path, p), "--all"], silent)
    summary_part = out.split("━━━ Summary\n", 1)[1]
    assert summary_part.startswith(
        "  ⚠ 2/2 attempted fixtures had near-zero wall time and zero tokens")
    # Notes are de-duplicated across fixtures; the stderr scan is per fdir.
    assert summary_part.count("    → luxe ran for 1.0s with 0 tokens") == 1
    assert "    → couldn't reach oMLX" in summary_part
    # The per-run block shows the same diagnosis.
    assert "        ⚠ couldn't reach oMLX — is `brew services start omlx` " \
           "running?" in out.splitlines()


def test_bench_end_unload_line(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a",))
    _rc, out, _calls = bench([*_base_argv(tmp_path, p), "--all"])
    assert out.endswith("\n[bench end] unloaded 1/2 model(s) from oMLX\n")
    _UnloadBackend.fail = True
    rc, out, _calls = bench([*_base_argv(tmp_path, p), "--all"])
    assert out.endswith("\n[bench end] model unload skipped: server gone\n")
    assert rc == 1


def test_cold_cache_clears_once_up_front_and_before_every_fixture(
        tmp_path, bench, monkeypatch):
    p = _fixtures_yaml(tmp_path, ids=("a", "b"))
    monkeypatch.setenv("LUXE_BENCH_COLD_CACHE", "1")
    monkeypatch.setattr(br, "_clear_server_cache",
                        lambda: bench.events.append("clear"))
    _rc, out, _calls = bench([*_base_argv(tmp_path, p), "--all"])
    assert bench.events == ["clear", "clear", "run:a", "clear", "run:b"]
    assert ("cold-cache: ON — oMLX hot+SSD prompt cache cleared before "
            "every fixture (LUXE_BENCH_COLD_CACHE=1)") in out.splitlines()


# --- variants -----------------------------------------------------------------

def _variants_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "variants.yaml"
    p.write_text(body)
    return p


def test_variants_run_variant_major_and_write_comparison(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a", "b"))
    v = _variants_yaml(tmp_path, "variants:\n"
                       "  - {model_label: m1, model_id: M1}\n"
                       "  - {model_label: m2, model_id: M2}\n")
    bail_r = FixtureResult(fixture_id="b", score=1)
    bail_d = Diagnostics(fixture_id="b", wall_s=20.0, tokens_total=40,
                         completion_tokens=40, bailout_type="prose_only")
    rc, out, calls = bench([*_base_argv(tmp_path, p), "--all",
                            "--variants", str(v)],
                           {"mono__m2/b": (bail_r, bail_d)})
    assert rc == 1
    assert [(c["variant"].variant_id, c["id"]) for c in calls] == [
        ("mono__m1", "a"), ("mono__m1", "b"),
        ("mono__m2", "a"), ("mono__m2", "b")]
    assert all(c["overlay_dir"] == tmp_path / "acc" / "_overlays"
               for c in calls)
    lines = out.splitlines()
    assert "variants: 2 cells × 2 fixtures = 4 runs" in lines
    assert "  - mono__m1  (M1)" in lines
    assert ("━━━ run 4/4  [12:00:00]  ETA ~1min  [mono__m2] b  [implement]  "
            "goal for b") in lines
    assert "  v1 release : NO (per-cell ≥8/10 — no cell cleared)" in lines
    assert "━━━ Mode × Model comparison" in lines
    assert any(ln.startswith("  mono__m2 ") and "prose_only×1" in ln
               for ln in lines)
    assert f"  Per-fixture mode breakdown saved: {tmp_path / 'acc'}" \
           "/comparison.json" in lines
    cmp = json.loads((tmp_path / "acc" / "comparison.json").read_text())
    assert cmp["fixtures"] == 2
    assert cmp["variants"]["mono__m2"] == {
        "passed": 1, "failed": 1, "errored": 0, "avg_wall_s": 30.0,
        "avg_tokens": 770, "avg_prompt_tokens": 600,
        "avg_completion_tokens": 170, "gen_tps_wall": 5.67,
        "microstep_rejects_total": 0, "bailouts": {"prose_only": 1},
        "fixtures": 2,
    }


def test_zero_variants_exits_2(tmp_path, bench):
    p = _fixtures_yaml(tmp_path, ids=("a",))
    v = _variants_yaml(tmp_path, "variants: []\n")
    rc, out, calls = bench([*_base_argv(tmp_path, p), "--all",
                            "--variants", str(v)])
    assert rc == 2 and calls == []
    assert out == f"variants file {v} loaded zero variants — nothing to do.\n"


def test_retired_mode_in_variants_file_is_refused(tmp_path):
    v = _variants_yaml(tmp_path, "variants:\n"
                       "  - {model_label: a, model_id: A, mode: swarm}\n")
    with pytest.raises(ValueError, match="mono-only"):
        br._load_variants(v)


def test_variants_file_parses_overrides(tmp_path):
    v = _variants_yaml(tmp_path, "variants:\n"
                       "  - {model_label: a, model_id: A, mode: mono,\n"
                       "     system_prompt_id: cot, task_prompt_id: sot,\n"
                       "     task_overlay_id: ov, temperature: 0.2,\n"
                       "     repeat_penalty: 1.1}\n")
    [a] = br._load_variants(v)
    assert (a.system_prompt_id, a.task_prompt_id, a.task_overlay_id,
            a.temperature, a.repeat_penalty, a.max_steps) == (
        "cot", "sot", "ov", 0.2, 1.1, None)


# --- the luxe maintain invocation ----------------------------------------------

def test_luxe_maintain_keeps_models_loaded(tmp_path, monkeypatch):
    """maintain_suite.sdd Must-not: never bench without --keep-loaded."""
    seen = {}

    def fake_capture(cmd, log_dir, *, env=None, timeout_s=None):
        seen.update(cmd=cmd, env=env, timeout_s=timeout_s)
        return 0, "run_id=0123456789abcdef\n", ""

    monkeypatch.setattr(br, "_run_capture", fake_capture)
    monkeypatch.delenv("LUXE_WRITE_PRESSURE", raising=False)
    f = br.Fixture(id="a", goal="g", task_type="implement",
                   expected_outcome={"kind": "regex_present"})
    rc, run_id, excerpt = br._luxe_maintain(tmp_path, f, tmp_path,
                                            timeout_s=1200.0)
    assert (rc, run_id, excerpt) == (0, "0123456789abcdef", "")
    assert "--keep-loaded" in seen["cmd"]
    assert seen["cmd"][2:5] == ["luxe.cli", "maintain", str(tmp_path)]
    assert seen["timeout_s"] == 1200.0
    assert seen["env"]["LUXE_WRITE_PRESSURE"] == "1"
