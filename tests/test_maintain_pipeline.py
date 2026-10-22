"""`maintain_pipeline` end to end, with the model, gh and oMLX stubbed.

Characterization tests written BEFORE the 2026-10-22 decomposition of
`maintain_pipeline` (C901 55): they pin what the benchmark/maintain path
does on each branch it takes — the events.jsonl sequence, the goal and phase
of every `run_single` pass, the report and `incomplete` flag handed to the
PR cycle, the console lines, the exit codes, and the teardown — so the split
into helpers can be checked against the function it replaced.

Real: git (a tmp repo), the BM25/symbol indices, the config, the run dir
under the hermetic $HOME, the repo lock, the diff, the spec validator, the
step-limit and reprompt logic. Stubbed: `run_single` (scripted passes that
may write files), `Backend`, `pr.preflight`/`pr.open_pr`, the cleanup turn,
and the MCP config/manager.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from luxe import maintain
from luxe import pr as pr_mod
from luxe.agents.loop import AgentResult
from luxe.agents.prompts import REPROMPT_SPEC_TEMPLATE
from luxe.citations import Citation, CitationResult, LintResult, SpecFinding
from luxe.run_state import PRState, run_dir
from luxe import steplimit
from tests._git import git


_SWITCHES = ("LUXE_REPROMPT_ON_DOC", "LUXE_STEP_LIMIT_LABEL",
             "LUXE_STEP_LIMIT_FINISH", "LUXE_CLEANUP_TURN",
             "LUXE_TOOL_BUDGET_CTX")


@dataclass
class Pass:
    """One scripted `run_single` pass: what it returns and what it writes."""
    text: str = "Done."
    aborted: bool = False
    reason: str = ""
    writes: dict[str, str] = field(default_factory=dict)
    tool_calls_total: int = 2


@dataclass
class Trace:
    exit_code: int | None
    events: list[dict]
    passes: list[dict]
    pr_calls: list[dict]
    preflight_calls: list[dict]
    cleanup_calls: list[dict]
    unloads: int
    console: str
    synthesizer: str | None
    run_id: str

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]

    def event(self, kind: str) -> dict:
        found = [e for e in self.events if e["kind"] == kind]
        assert len(found) == 1, (kind, self.kinds())
        return found[0]


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.mp = monkeypatch
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n")
        (self.repo / "README.md").write_text("# calc\n")
        self._git("init", "-q")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "base")
        self.base_sha = self._git("rev-parse", "HEAD").strip()

        self.script: list[Pass] = []
        self.passes: list[dict] = []
        self.pr_calls: list[dict] = []
        self.preflight_calls: list[dict] = []
        self.cleanup_calls: list[dict] = []
        self.unloads = 0
        self.pr_result: Any = PRState(pr_url="https://example.invalid/pr/1")
        self.preflight_error: Exception | None = None
        self.cleanup_result = {"candidates": [], "deleted": [], "error": "",
                               "reply": None}
        self.mcp_servers: list = []
        self.mcp_events: list[str] = []
        self.buf = io.StringIO()

        for name in _SWITCHES:
            monkeypatch.delenv(name, raising=False)
        from luxe.tools import fs
        monkeypatch.setattr(fs, "_REPO_ROOT", fs._REPO_ROOT)
        monkeypatch.setattr(maintain, "console", Console(
            file=self.buf, width=400, color_system=None, force_terminal=False))
        self._install_stubs()

    def _git(self, *args: str) -> str:
        return git(self.repo, *args, strip=False)

    def _install_stubs(self) -> None:
        h = self
        mp = self.mp

        def fake_run_single(backend, role_cfg, **kw):
            h.passes.append({
                "goal": kw["goal"], "phase": kw["phase"],
                "task_type": kw["task_type"],
                "max_steps": role_cfg.max_steps,
                "languages": sorted(kw["languages"] or []),
                "extra_tool_defs": kw["extra_tool_defs"],
                "extra_tool_fns": kw["extra_tool_fns"],
                "run_id": kw["run_id"], "backend_model": backend.model,
                "diff_before": pr_mod.diff_against_base(h.repo, h.base_sha),
            })
            p = h.script.pop(0) if h.script else Pass()
            for rel, body in p.writes.items():
                f = h.repo / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(body)
            return AgentResult(final_text=p.text, aborted=p.aborted,
                               abort_reason=p.reason, steps=3,
                               tool_calls_total=p.tool_calls_total,
                               prompt_tokens=100, completion_tokens=10,
                               wall_s=1.0, peak_context_pressure=0.25)

        class FakeBackend:
            def __init__(self, base_url="", model="", **kw):
                self.base_url, self.model = base_url, model

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def unload_all_loaded(self):
                h.unloads += 1
                return {"m1": True, "m2": False}

        def fake_preflight(repo_path, **kw):
            h.preflight_calls.append({"repo_path": repo_path, **kw})
            if h.preflight_error is not None:
                raise h.preflight_error
            return pr_mod.PRPreflight(base_branch="main", base_sha=h.base_sha,
                                      branch_name="luxe/x", test_command="pytest -q")

        def fake_open_pr(spec, **kw):
            h.pr_calls.append({"run_id": spec.run_id, **kw})
            kw["on_event"]("commit", {"sha": "abc"})
            if isinstance(h.pr_result, Exception):
                raise h.pr_result
            return h.pr_result

        def fake_cleanup(backend, **kw):
            h.cleanup_calls.append(kw)
            return dict(h.cleanup_result)

        from luxe.agents import single
        from luxe import backend as backend_mod, cleanup
        from luxe.mcp import client as mcp_client
        mp.setattr(single, "run_single", fake_run_single)
        mp.setattr(backend_mod, "Backend", FakeBackend)
        mp.setattr(pr_mod, "load_pr_config", lambda *a, **k: pr_mod.PRConfig(test_commands={}))
        mp.setattr(pr_mod, "preflight", fake_preflight)
        mp.setattr(pr_mod, "open_pr", fake_open_pr)
        mp.setattr(cleanup, "run_cleanup_turn", fake_cleanup)
        mp.setattr(mcp_client, "load_mcp_config",
                   lambda *a, **k: mcp_client.MCPClientConfig(
                       servers=list(h.mcp_servers)))

        class FakeMCP:
            def __init__(self, cfg):
                h.mcp_events.append(
                    "init:" + ",".join(s.name for s in cfg.servers))

            def start(self):
                h.mcp_events.append("start")
                return self

            def discover_tools(self, *, only_for_task=None):
                h.mcp_events.append(f"discover:{only_for_task}")
                return ([{"name": "mcp__a__t"}], {"mcp__a__t": lambda **k: ""})

            def server_status(self):
                return [{"name": "a", "down": False, "down_reason": ""},
                        {"name": "b", "down": True, "down_reason": "refused"}]

            def close(self):
                h.mcp_events.append("close")
                raise RuntimeError("close errors are swallowed")

        mp.setattr(mcp_client, "MCPClientManager", FakeMCP)

    def run(self, *, task: str = "implement", goal: str = "Add a mul function",
            spec_yaml_path: str | None = None, save_report: bool = False,
            output_dir: str = "", keep_loaded: bool = False,
            skip_confirm: bool = True) -> Trace:
        exit_code = None
        try:
            maintain.maintain_pipeline(
                str(self.repo), goal, task, None, False, skip_confirm, False,
                output_dir, save_report, keep_loaded, spec_yaml_path)
        except SystemExit as e:
            exit_code = e.code
        runs = list((Path.home() / ".luxe" / "runs").glob("*/events.jsonl"))
        events: list[dict] = []
        run_id = ""
        if runs:
            assert len(runs) == 1
            run_id = runs[0].parent.name
            for line in runs[0].read_text().splitlines():
                rec = json.loads(line)
                rec.pop("ts")
                assert rec.pop("run_id") == run_id
                events.append(rec)
        synth = run_dir(run_id) / "synthesizer.md" if run_id else None
        return Trace(
            exit_code=exit_code, events=events, passes=self.passes,
            pr_calls=self.pr_calls, preflight_calls=self.preflight_calls,
            cleanup_calls=self.cleanup_calls, unloads=self.unloads,
            console=self.buf.getvalue(),
            synthesizer=synth.read_text() if synth and synth.exists() else None,
            run_id=run_id)

    def lock_is_free(self) -> bool:
        from luxe.locks import LockHeld, acquire_repo_lock
        try:
            with acquire_repo_lock(str(self.repo.resolve()), "probe"):
                return True
        except LockHeld:
            return False


@pytest.fixture
def h(tmp_path, monkeypatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


_MAIN_DONE = {
    "kind": "single_mode_done", "wall_s": 1.0, "prompt_tokens": 100,
    "completion_tokens": 10, "tool_calls_total": 2, "schema_rejects": 0,
    "aborted": False, "abort_reason": "", "final_text_chars": 5,
    "peak_context_pressure": 0.25,
}


# --- read-only and write happy paths -------------------------------------

def test_read_only_task_runs_one_pass_and_opens_no_pr(h):
    t = h.run(task="review", goal="Review calc")
    assert t.exit_code is None
    assert t.kinds() == ["preflight_ok", "read_budget_applied",
                         "single_mode_done", "citation_lint_passed"]
    assert t.events[0] == {"kind": "preflight_ok", "base_branch": "main",
                           "branch_name": "luxe/x",
                           "test_command": "pytest -q"}
    assert t.event("single_mode_done") == _MAIN_DONE
    assert t.event("citation_lint_passed") == {
        "kind": "citation_lint_passed", "count": 0, "summary": ""}
    [p] = t.passes
    assert (p["goal"], p["phase"], p["task_type"], p["run_id"]) == (
        "Review calc", "main", "review", t.run_id)
    assert p["extra_tool_defs"] is None and p["extra_tool_fns"] is None
    assert p["languages"] == ["python"]
    assert t.pr_calls == [] and t.cleanup_calls == []
    assert t.synthesizer == "Done."
    assert "(read-only task; no PR)" in t.console
    # The report prints last; the unload line comes from the teardown.
    assert t.console.endswith(
        "=" * 60 + "\nDone.\n· Unloaded 1/2 model(s) from oMLX "
        "(use --keep-loaded to skip)\n")
    assert t.unloads == 1 and h.lock_is_free()
    from luxe import search, symbols
    assert search.get_index() is None and symbols.get_index() is None


def test_write_task_records_diff_stat_cleans_up_and_opens_the_pr(h):
    h.script = [Pass(writes={"src/calc.py": "def add(a, b):\n    return a + b\n"
                             "\n\ndef mul(a, b):\n    return a * b\n"})]
    t = h.run()
    assert t.kinds() == ["preflight_ok", "read_budget_applied",
                         "single_mode_done", "diff_stat",
                         "citation_lint_passed", "cleanup_turn"]
    assert t.event("diff_stat") == {"kind": "diff_stat",
                                    "checkpoint": "after_main_pass",
                                    "additions": 4, "deletions": 0}
    assert t.event("cleanup_turn") == {"kind": "cleanup_turn",
                                       "candidates": [], "deleted": [],
                                       "error": "", "reply": None}
    [c] = t.cleanup_calls
    assert c["goal"] == "Add a mul function" and c["base_sha"] == h.base_sha
    [pr] = t.pr_calls
    assert pr["report_text"] == "Done." and pr["incomplete"] is False
    assert (pr["task_type"], pr["goal"], pr["test_command"],
            pr["branch_name"], pr["watch_ci"]) == (
        "implement", "Add a mul function", "pytest -q", "luxe/x", False)
    assert "pr commit: {'sha': 'abc'}" in t.console
    assert "✓ PR opened: https://example.invalid/pr/1" in t.console
    assert t.preflight_calls[0]["confirm_callback"]() is True


def test_header_and_test_command_lines(h):
    t = h.run(task="review", goal="Review calc")
    assert f"luxe maintain  run_id={t.run_id}" in t.console
    assert "Branch: luxe/x  Base: main@" + h.base_sha[:8] in t.console
    assert "Tests: pytest -q" in t.console


def test_empty_final_text_writes_no_report_and_skips_lint(h):
    h.script = [Pass(text="")]
    t = h.run(task="review", goal="Review calc")
    assert t.kinds() == ["preflight_ok", "read_budget_applied",
                         "single_mode_done"]
    assert t.synthesizer is None and "=" * 60 not in t.console


def test_read_budget_comes_from_the_role_the_run_uses(h, monkeypatch):
    from luxe.config import load_config
    from luxe.tools import fs
    num_ctx = 65536  # not the shipped 32768, so a hardcoded window shows

    def load(path):
        cfg = load_config(path)
        cfg.roles["monolith"].num_ctx = num_ctx
        return cfg

    monkeypatch.setattr(maintain, "load_config", load)
    t = h.run(task="review", goal="Review calc")
    assert t.event("read_budget_applied") == {
        "kind": "read_budget_applied",
        "max_bytes": fs.budget_for_ctx(num_ctx), "num_ctx": num_ctx}
    assert (f"· Tool read budget: {fs.budget_for_ctx(num_ctx):,} bytes "
            f"(LUXE_TOOL_BUDGET_CTX=1, num_ctx={num_ctx})") in t.console


def test_read_budget_off_emits_no_event(h, monkeypatch):
    monkeypatch.setenv("LUXE_TOOL_BUDGET_CTX", "0")
    t = h.run(task="review", goal="Review calc")
    assert "read_budget_applied" not in t.kinds()
    assert "Tool read budget" not in t.console


def test_save_report_and_keep_loaded(h, tmp_path):
    out = tmp_path / "out"
    t = h.run(task="review", goal="Review calc", save_report=True,
              output_dir=str(out), keep_loaded=True)
    assert (out / f"report_{t.run_id}.md").read_text() == "Done."
    assert t.unloads == 0 and "Unloaded" not in t.console


# --- diff failures --------------------------------------------------------

def test_git_diff_failure_is_recorded_and_skips_the_reprompt(h, monkeypatch):
    monkeypatch.setenv("LUXE_REPROMPT_ON_DOC", "1")

    def boom(repo, base):
        raise pr_mod.GitDiffError("index.lock")

    monkeypatch.setattr(maintain, "_diff_against_base", boom)
    h.script = [Pass(aborted=True, reason="Max steps reached (30)")]
    t = h.run(task="document", goal="Document calc in CONFIG.md")
    failed = [e for e in t.events if e["kind"] == "diff_stat_failed"]
    assert failed == [
        {"kind": "diff_stat_failed", "checkpoint": "after_main_pass",
         "error": "index.lock"},
        {"kind": "diff_stat_failed", "checkpoint": "reprompt_gate",
         "error": "index.lock"},
    ]
    assert "reprompt_fired" not in t.kinds()
    assert "step_limit_incomplete" not in t.kinds()
    assert len(t.passes) == 1
    assert "diff unavailable (index.lock) — under-engagement gate skipped" \
        in t.console


# --- reprompt gates -------------------------------------------------------

def test_v1_3_reprompt_fires_on_a_small_doc_diff(h, monkeypatch):
    monkeypatch.setenv("LUXE_REPROMPT_ON_DOC", "1")
    h.script = [Pass(text="first", writes={"CONFIG.md": "# Config\n"}),
                Pass(text="second", aborted=True, reason="stuck",
                     writes={"CONFIG.md": "# Config\n\nmore\n"},
                     tool_calls_total=5)]
    t = h.run(task="document", goal="Document calc in CONFIG.md")
    assert t.kinds() == ["preflight_ok", "read_budget_applied",
                         "single_mode_done", "diff_stat",
                         "citation_lint_passed", "reprompt_fired",
                         "reprompt_done", "diff_stat", "cleanup_turn"]
    assert t.event("reprompt_fired") == {
        "kind": "reprompt_fired", "additions": 1, "deletions": 0,
        "threshold": 10, "gate": "v1_3_directive"}
    assert t.event("reprompt_done") == {
        "kind": "reprompt_done", "second_pass_tool_calls": 5,
        "second_pass_completion_tokens": 10, "second_pass_aborted": True}
    assert t.events[-2] == {"kind": "diff_stat",
                            "checkpoint": "after_reprompt_pass",
                            "additions": 3, "deletions": 0}
    second = t.passes[1]
    assert second["phase"] == "reprompt"
    assert second["goal"] == maintain._v1_3_followup_goal(
        "Document calc in CONFIG.md", *second["diff_before"], "first")
    merged = "first\n\n--- Reprompt 2nd pass ---\nsecond"
    assert t.synthesizer == merged
    [pr] = t.pr_calls
    assert pr["report_text"] == merged
    assert "Reprompt 2nd pass  (diff +1/-0 below threshold 10)" in t.console


def test_v1_3_reprompt_on_stranded_prose_carries_the_prior_report(
        h, monkeypatch):
    monkeypatch.setenv("LUXE_REPROMPT_ON_DOC", "1")
    prose = "# Config\n" + "The X switch does Y.\n" * 80
    h.script = [Pass(text=prose), Pass(text="")]
    t = h.run(task="document", goal="Document calc in CONFIG.md")
    assert t.event("reprompt_fired") == {
        "kind": "reprompt_fired", "additions": 0, "deletions": 0,
        "threshold": 10, "gate": "v1_3_directive"}
    goal = t.passes[1]["goal"]
    assert goal == maintain._v1_3_followup_goal(
        "Document calc in CONFIG.md", 0, 0, "", prose)
    assert prose[:500] in goal
    # No second-pass text: the report stays the main pass's prose.
    assert t.synthesizer == prose


def test_v1_3_reprompt_is_off_by_default(h):
    h.script = [Pass(writes={"CONFIG.md": "# Config\n"})]
    t = h.run(task="document", goal="Document calc in CONFIG.md")
    assert "reprompt_fired" not in t.kinds() and len(t.passes) == 1


def _spec_file(tmp_path: Path) -> str:
    p = tmp_path / "spec.yaml"
    p.write_text(
        "goal: Add mul and div\n"
        "requirements:\n"
        "  - id: R1\n    must: add mul\n    done_when: mul defined\n"
        "    kind: regex_present\n    pattern: 'def mul'\n"
        "  - id: R2\n    must: add div\n    done_when: div defined\n"
        "    kind: regex_present\n    pattern: 'def div'\n")
    return str(p)


def test_spec_reprompt_names_the_unmet_requirement(h, tmp_path, monkeypatch):
    monkeypatch.setenv("LUXE_REPROMPT_ON_DOC", "1")
    h.script = [Pass(text="", writes={"src/m.py": "def mul(a, b):\n    return a*b\n"}),
                Pass(text="second")]
    t = h.run(goal="Add mul and div", spec_yaml_path=_spec_file(tmp_path))
    assert t.kinds() == ["preflight_ok", "read_budget_applied",
                         "single_mode_done", "diff_stat", "spec_validation",
                         "reprompt_fired", "reprompt_done", "diff_stat",
                         "cleanup_turn"]
    assert t.event("spec_validation") == {
        "kind": "spec_validation", "all_satisfied": False, "total": 2,
        "error": None, "unsatisfied_ids": ["R2"]}
    assert t.event("reprompt_fired") == {
        "kind": "reprompt_fired", "additions": 2, "deletions": 0,
        "threshold": 10, "gate": "spec"}
    goal = t.passes[1]["goal"]
    assert goal.startswith(REPROMPT_SPEC_TEMPLATE.format(goal="Add mul and div"))
    assert "R2" in goal and "R1" not in goal
    # Empty main text: the merged report is the joiner + the second pass.
    assert t.synthesizer == "\n\n--- Reprompt 2nd pass ---\nsecond"
    assert "(spec: 1/2 requirement(s) unmet)" in t.console


def test_satisfied_spec_suppresses_both_gates(h, tmp_path, monkeypatch):
    monkeypatch.setenv("LUXE_REPROMPT_ON_DOC", "1")
    h.script = [Pass(writes={"src/m.py": "def mul(): ...\ndef div(): ...\n"})]
    t = h.run(task="document", goal="Add mul and div",
              spec_yaml_path=_spec_file(tmp_path))
    assert t.event("spec_validation")["all_satisfied"] is True
    assert "reprompt_fired" not in t.kinds() and len(t.passes) == 1


def test_spec_is_not_validated_without_the_switch(h, tmp_path):
    h.script = [Pass(writes={"src/m.py": "x = 1\n"})]
    t = h.run(goal="Add mul and div", spec_yaml_path=_spec_file(tmp_path))
    assert "spec_validation" not in t.kinds() and len(t.passes) == 1


def test_spec_validation_error_skips_the_reprompt(h, tmp_path, monkeypatch):
    monkeypatch.setenv("LUXE_REPROMPT_ON_DOC", "1")
    from luxe import spec_validator

    def broken(spec, repo, base_sha):
        # Unmet verdicts, but only because the diff was unreadable.
        return spec_validator.ValidationResult(
            spec=spec, error="git diff failed",
            results=[spec_validator.RequirementResult(
                requirement=r, satisfied=False, detail="not evaluated")
                for r in spec.requirements])

    monkeypatch.setattr(spec_validator, "validate", broken)
    h.script = [Pass(writes={"src/m.py": "x = 1\n"})]
    t = h.run(goal="Add mul and div", spec_yaml_path=_spec_file(tmp_path))
    assert t.event("spec_validation")["error"] == "git diff failed"
    assert "reprompt_fired" not in t.kinds() and len(t.passes) == 1
    assert "spec validation ERROR: git diff failed — reprompt gate skipped" \
        in t.console


# --- step limit -----------------------------------------------------------

def test_step_limit_with_a_change_is_labelled_and_drafted(h):
    h.script = [Pass(aborted=True, reason="Max steps reached (30)",
                     writes={"src/calc.py": "half\n"})]
    t = h.run()
    assert t.kinds() == ["preflight_ok", "read_budget_applied",
                         "single_mode_done", "diff_stat",
                         "citation_lint_passed", "step_limit_incomplete",
                         "cleanup_turn"]
    assert t.event("step_limit_incomplete") == {
        "kind": "step_limit_incomplete", "additions": 1, "deletions": 2,
        "abort_reason": "Max steps reached (30)"}
    labelled = steplimit.label_report("Done.")
    assert t.synthesizer == labelled
    [pr] = t.pr_calls
    assert pr["incomplete"] is True and pr["report_text"] == labelled
    assert "labelled incomplete (draft PR)" in t.console


def test_step_limit_label_can_be_disabled(h, monkeypatch):
    monkeypatch.setenv("LUXE_STEP_LIMIT_LABEL", "0")
    h.script = [Pass(aborted=True, reason="Max steps reached (30)",
                     writes={"src/calc.py": "half\n"})]
    t = h.run()
    assert "step_limit_incomplete" not in t.kinds()
    assert t.pr_calls[0]["incomplete"] is False
    assert t.synthesizer == "Done."


def test_step_limit_without_a_change_is_not_labelled(h):
    h.script = [Pass(aborted=True, reason="Max steps reached (30)")]
    t = h.run()
    assert "step_limit_incomplete" not in t.kinds()
    assert t.pr_calls[0]["incomplete"] is False


def test_finish_pass_that_ends_on_its_own_clears_the_label(h, monkeypatch):
    monkeypatch.setenv("LUXE_STEP_LIMIT_FINISH", "1")
    h.script = [Pass(aborted=True, reason="Max steps reached (30)",
                     writes={"src/calc.py": "half\n"}),
                Pass(text="fin", tool_calls_total=4)]
    t = h.run()
    assert t.kinds() == ["preflight_ok", "read_budget_applied",
                         "single_mode_done", "diff_stat",
                         "citation_lint_passed", "step_limit_finish_begin",
                         "step_limit_finish_done", "cleanup_turn"]
    assert t.event("step_limit_finish_begin") == {
        "kind": "step_limit_finish_begin", "additions": 1, "deletions": 2}
    assert t.event("step_limit_finish_done") == {
        "kind": "step_limit_finish_done", "aborted": False,
        "abort_reason": "", "tool_calls": 4}
    fin = t.passes[1]
    assert fin["phase"] == "step_limit_finish"
    assert fin["max_steps"] == steplimit.FINISH_MAX_STEPS
    assert fin["goal"].startswith(steplimit.finish_goal(
        "Add a mul function", "")[:40])
    assert "+half" in fin["goal"]
    assert t.pr_calls[0]["incomplete"] is False
    # The finish pass's text is NOT folded into the report.
    assert t.pr_calls[0]["report_text"] == "Done."


def test_finish_pass_that_also_hits_the_limit_stays_labelled(h, monkeypatch):
    monkeypatch.setenv("LUXE_STEP_LIMIT_FINISH", "1")
    h.script = [Pass(aborted=True, reason="Max steps reached (30)",
                     writes={"src/calc.py": "half\n"}),
                Pass(aborted=True, reason="Max steps reached (8)")]
    t = h.run()
    assert t.kinds()[-3:] == ["step_limit_finish_done",
                              "step_limit_incomplete", "cleanup_turn"]
    assert t.event("step_limit_incomplete")["abort_reason"] == \
        "Max steps reached (30)"
    assert t.pr_calls[0]["incomplete"] is True


def test_reprompt_step_limit_is_labelled_with_its_prefix(h, monkeypatch):
    monkeypatch.setenv("LUXE_REPROMPT_ON_DOC", "1")
    h.script = [Pass(text="first", writes={"CONFIG.md": "# C\n"}),
                Pass(text="", aborted=True, reason="Max steps reached (30)")]
    t = h.run(task="document", goal="Document calc in CONFIG.md")
    assert t.event("step_limit_incomplete")["abort_reason"] == \
        "reprompt: Max steps reached (30)"
    # No second-pass text: the report is the main pass's, labelled.
    assert t.synthesizer == steplimit.label_report("first")


# --- cleanup turn ---------------------------------------------------------

def test_cleanup_turn_off_makes_no_call(h, monkeypatch):
    monkeypatch.setenv("LUXE_CLEANUP_TURN", "0")
    t = h.run()
    assert t.cleanup_calls == [] and "cleanup_turn" not in t.kinds()


def test_cleanup_turn_with_candidates_is_announced(h):
    h.cleanup_result = {"candidates": ["a.py", "b.py"], "deleted": ["a.py"],
                        "error": "boom", "reply": "DELETE a.py"}
    t = h.run()
    assert t.event("cleanup_turn")["reply"] == "DELETE a.py"
    assert "cleanup turn: 2 candidate(s), deleted ['a.py'] — boom" in t.console


# --- PR outcomes and early exits ------------------------------------------

def test_no_pr_url_says_no_diff(h):
    h.pr_result = PRState()
    t = h.run()
    assert t.exit_code is None and "No PR opened (no diff produced)" in t.console


def test_no_mutations_exits_4_and_still_tears_down(h):
    h.pr_result = pr_mod.NoMutationsError("no diff")
    t = h.run()
    assert t.exit_code == 4
    assert "Status: failed_no_mutations_produced." in t.console
    assert t.unloads == 1 and h.lock_is_free()


def test_pr_error_exits_5_with_resume_hint(h):
    h.pr_result = pr_mod.PRError("push rejected")
    t = h.run()
    assert t.exit_code == 5
    assert f"Resume with: luxe pr {t.run_id}" in t.console
    assert "=" * 60 not in t.console  # report never printed
    assert h.lock_is_free()


@pytest.mark.parametrize("err", [pr_mod.GhAuthError("gh not authed"),
                                 pr_mod.DirtyTreeError("dirty")])
def test_preflight_errors_exit_2_before_any_run(h, err):
    h.preflight_error = err
    t = h.run()
    assert t.exit_code == 2 and t.events == [] and t.passes == []
    assert f"✗ {err}" in t.console


def test_held_lock_exits_3(h):
    from luxe.locks import acquire_repo_lock
    with acquire_repo_lock(str(h.repo.resolve()), "other"):
        import luxe.locks as locks
        h.mp.setattr(locks, "_is_pid_alive", lambda pid: True)
        t = h.run()
    assert t.exit_code == 3 and t.kinds() == ["preflight_ok"]
    assert t.passes == []


def test_non_tty_without_skip_confirm_passes_no_callback(h, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO())
    t = h.run(task="review", goal="Review calc", skip_confirm=False)
    assert t.preflight_calls[0]["confirm_callback"] is None


# --- lint -----------------------------------------------------------------

def test_blocking_lint_lists_findings_and_orphans(h, monkeypatch):
    res = LintResult(
        citations=[CitationResult(citation=Citation(path="src/x.py", line=3),
                                  status="missing_file", detail="gone")],
        spec_findings=[
            SpecFinding(kind="spec_violation", path="tests/t.py",
                        glob="tests/**", sdd_path="r.sdd"),
            SpecFinding(kind="spec_orphan", path="lib/o.py", glob=None,
                        sdd_path=None)])
    import luxe.citations as citations
    monkeypatch.setattr(citations, "lint_report", lambda *a, **k: res)
    t = h.run(task="review", goal="Review calc")
    assert t.event("citation_lint_blocked") == {
        "kind": "citation_lint_blocked", "unresolved": 1,
        "spec_violations": 1, "summary": res.summary()}
    assert "`src/x.py:3` — missing_file: gone" in t.console
    assert "spec_violation `tests/t.py` forbidden by `r.sdd` (glob `tests/**`)" \
        in t.console
    assert "1 spec_orphan warning(s)" in t.console
    assert "`lib/o.py` (no Owns: glob covers this path)" in t.console


# --- MCP ------------------------------------------------------------------

def test_mcp_server_enabled_for_the_task_reaches_the_pass(h):
    from luxe.mcp.client import MCPServerConfig
    h.mcp_servers = [MCPServerConfig(name="a", enabled_for=["review"]),
                     MCPServerConfig(name="c", enabled_for=["implement"])]
    t = h.run(task="review", goal="Review calc")
    assert h.mcp_events == ["init:a", "start", "discover:review", "close"]
    assert [d["name"] for d in t.passes[0]["extra_tool_defs"]] == ["mcp__a__t"]
    assert list(t.passes[0]["extra_tool_fns"]) == ["mcp__a__t"]
    assert "MCP: 1 tool(s) from 1 server(s)" in t.console
    assert "MCP server b DOWN: refused" in t.console


def test_mcp_server_not_enabled_is_never_started(h):
    from luxe.mcp.client import MCPServerConfig
    h.mcp_servers = [MCPServerConfig(name="c", enabled_for=["implement"])]
    h.run(task="review", goal="Review calc")
    assert h.mcp_events == []
