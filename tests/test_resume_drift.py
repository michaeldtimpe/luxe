"""Tests for the orchestrator's checkpoint/resume behaviour and drift handling.

These tests verify the data-flow / structural pieces of resume:
- Cached architect results are loaded and the model is not re-invoked.
- Cached worker subtasks are restored into PipelineRun.subtasks.
- Cached validator envelope is reused.
- Cached synthesizer report is reused.
- clear_stages() empties the cache for --force-resume.

The orchestrator's actual model-driven path requires a backend; we test
the checkpoint logic by pre-seeding stages and asserting the agent
functions are NOT called. Tests use monkeypatch to fail loudly if any
stage gets re-invoked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from luxe.agents.loop import AgentResult
from luxe.config import PipelineConfig, RoleConfig, TaskTypeConfig
from luxe.pipeline.orchestrator import PipelineOrchestrator
from luxe.run_state import (
    RunSpec,
    clear_stages,
    init_run_dir,
    list_completed_stages,
    save_stage,
)


@pytest.fixture(autouse=True)
def _isolate_runs_root(tmp_path, monkeypatch):
    monkeypatch.setattr("luxe.run_state.runs_root", lambda: tmp_path / "runs")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    (r / "f.py").write_text("x = 1\n")
    return r


def _config() -> PipelineConfig:
    return PipelineConfig(
        omlx_base_url="http://stub:0",
        models={
            "architect": "stub-architect",
            "worker_read": "stub-worker",
            "validator": "stub-validator",
            "synthesizer": "stub-synth",
        },
        roles={
            "architect": RoleConfig(model_key="architect", num_ctx=8192, max_steps=2),
            "worker_read": RoleConfig(model_key="worker_read", num_ctx=8192, max_steps=2),
            "validator": RoleConfig(model_key="validator", num_ctx=8192, max_steps=2),
            "synthesizer": RoleConfig(model_key="synthesizer", num_ctx=8192, max_steps=2),
        },
        task_types={
            "review": TaskTypeConfig(
                description="rv",
                pipeline=["architect", "worker_read", "validator", "synthesizer"],
                architect_prompt="decompose",
            ),
        },
    )


def _seed_full_run(run_id: str) -> None:
    """Seed all four stage checkpoints so the orchestrator should run zero models."""
    save_stage(run_id, "architect", {
        "raw_text": "[]",
        "objectives": [
            {"title": "Read main", "role": "worker_read", "expected_tools": 2, "scope": "."}
        ],
        "prompt_tokens": 100, "completion_tokens": 50, "wall_s": 1.2,
    })
    save_stage(run_id, "worker_0", {
        "index": 0, "id": "w0",
        "title": "Read main", "role": "worker_read", "scope": ".",
        "expected_tools": 2, "status": "done",
        "result_text": "Found `f.py:1`",
        "tool_calls": [{
            "id": "c0", "name": "read_file",
            "arguments": {"path": "f.py"},
            "result": "x = 1", "error": None, "cached": False,
            "duplicate": False, "bytes_out": 10, "wall_s": 0.1,
        }],
        "metrics": {
            "wall_s": 1.5, "prompt_tokens": 50, "completion_tokens": 25,
            "tool_calls": 1, "schema_rejects": 0,
            "peak_context_pressure": 0.1, "model": "stub-worker",
            "model_swap_s": 0.0, "cache_hits": 0, "cache_misses": 1,
        },
        "escalated_from": None,
    })
    save_stage(run_id, "validator", {
        "raw_text": '{"status":"verified",...}',
        "envelope": {
            "status": "verified",
            "verified": [{"path": "f.py", "line": 1, "snippet": "x = 1",
                          "severity": "info", "description": "trivial"}],
            "removed": [], "summary": "checked",
        },
        "prompt_tokens": 30, "completion_tokens": 15, "wall_s": 0.8,
    })
    save_stage(run_id, "synthesizer", {
        "final_report": "# Report\n\nFound `f.py:1`\n```\nx = 1\n```",
        "prompt_tokens": 40, "completion_tokens": 60, "wall_s": 1.0,
    })


def test_orchestrator_resumes_all_stages_without_model_calls(monkeypatch, repo: Path):
    """When all stages are checkpointed, no model agent should run."""
    spec = RunSpec(run_id="resume-all", goal="g", task_type="review",
                   repo_path=str(repo), base_sha="abc", actual_mode="swarm")
    init_run_dir(spec)
    _seed_full_run(spec.run_id)

    # Make the agent functions blow up if called.
    def _boom(*a, **kw):
        raise AssertionError("agent must not be invoked when checkpoint exists")

    monkeypatch.setattr("luxe.pipeline.orchestrator.run_architect", _boom)
    monkeypatch.setattr("luxe.pipeline.orchestrator.run_worker", _boom)
    monkeypatch.setattr("luxe.pipeline.orchestrator.run_validator", _boom)
    monkeypatch.setattr("luxe.pipeline.orchestrator.run_synthesizer", _boom)
    # _get_backend would try to open httpx; stub it.
    monkeypatch.setattr(PipelineOrchestrator, "_get_backend",
                        lambda self, role: None)

    orch = PipelineOrchestrator(_config(), run_id=spec.run_id)
    run = orch.run(spec.goal, "review", str(repo))

    assert run.final_report.startswith("# Report")
    assert run.validator_envelope is not None
    assert run.validator_envelope.status == "verified"
    assert len(run.validator_envelope.verified) == 1
    assert len(run.subtasks) == 1
    assert run.subtasks[0].result_text == "Found `f.py:1`"
    assert run.subtasks[0].tool_calls[0].name == "read_file"


def test_orchestrator_runs_only_missing_stages(monkeypatch, repo: Path):
    """Architect cached; worker NOT cached → only worker (and downstream) re-run."""
    spec = RunSpec(run_id="resume-mid", goal="g", task_type="review",
                   repo_path=str(repo), base_sha="abc", actual_mode="swarm")
    init_run_dir(spec)
    save_stage(spec.run_id, "architect", {
        "raw_text": "[]",
        "objectives": [
            {"title": "Read main", "role": "worker_read", "expected_tools": 2, "scope": "."}
        ],
        "prompt_tokens": 0, "completion_tokens": 0, "wall_s": 0.0,
    })

    arch_called = {"n": 0}
    worker_called = {"n": 0}

    def fake_arch(*a, **kw):
        arch_called["n"] += 1
        return AgentResult(), []
    def fake_worker(*a, **kw):
        worker_called["n"] += 1
        r = AgentResult(final_text="ran fresh", wall_s=0.5)
        return r
    def fake_validator(*a, **kw):
        from luxe.agents.validator import ValidatorEnvelope
        return AgentResult(final_text='{"status":"cleared"}', wall_s=0.1), \
               ValidatorEnvelope(status="cleared")
    def fake_synth(*a, **kw):
        return AgentResult(final_text="# Synth", wall_s=0.1)

    monkeypatch.setattr("luxe.pipeline.orchestrator.run_architect", fake_arch)
    monkeypatch.setattr("luxe.pipeline.orchestrator.run_worker", fake_worker)
    monkeypatch.setattr("luxe.pipeline.orchestrator.run_validator", fake_validator)
    monkeypatch.setattr("luxe.pipeline.orchestrator.run_synthesizer", fake_synth)
    monkeypatch.setattr(PipelineOrchestrator, "_get_backend",
                        lambda self, role: None)

    orch = PipelineOrchestrator(_config(), run_id=spec.run_id)
    run = orch.run(spec.goal, "review", str(repo))

    assert arch_called["n"] == 0       # architect cached → skipped
    assert worker_called["n"] == 1     # worker not cached → ran fresh
    assert run.final_report == "# Synth"
    # New checkpoints written
    stages = list_completed_stages(spec.run_id)
    assert "worker_0" in stages
    assert "validator" in stages
    assert "synthesizer" in stages


def test_clear_stages_invalidates_cache(repo: Path):
    spec = RunSpec(run_id="clear-test", goal="g", task_type="review",
                   repo_path=str(repo), base_sha="abc")
    init_run_dir(spec)
    _seed_full_run(spec.run_id)
    assert len(list_completed_stages(spec.run_id)) == 4
    n = clear_stages(spec.run_id)
    assert n == 4
    assert list_completed_stages(spec.run_id) == []


def test_no_run_id_means_no_checkpointing(monkeypatch, repo: Path, tmp_path: Path):
    """Backwards compat: run_id=None means no checkpoint persistence."""
    arch_called = {"n": 0}

    def fake_arch(*a, **kw):
        arch_called["n"] += 1
        return AgentResult(), [
            {"title": "x", "role": "worker_read", "expected_tools": 1, "scope": "."}
        ]
    def fake_worker(*a, **kw):
        return AgentResult(final_text="wf", wall_s=0.1)
    def fake_validator(*a, **kw):
        from luxe.agents.validator import ValidatorEnvelope
        return AgentResult(final_text='{"status":"cleared"}'), \
               ValidatorEnvelope(status="cleared")
    def fake_synth(*a, **kw):
        return AgentResult(final_text="rep")

    monkeypatch.setattr("luxe.pipeline.orchestrator.run_architect", fake_arch)
    monkeypatch.setattr("luxe.pipeline.orchestrator.run_worker", fake_worker)
    monkeypatch.setattr("luxe.pipeline.orchestrator.run_validator", fake_validator)
    monkeypatch.setattr("luxe.pipeline.orchestrator.run_synthesizer", fake_synth)
    monkeypatch.setattr(PipelineOrchestrator, "_get_backend",
                        lambda self, role: None)

    # No run_id passed
    orch = PipelineOrchestrator(_config())
    run = orch.run("g", "review", str(repo))
    assert arch_called["n"] == 1
    # No stages directory should be created when run_id is None
    assert not (tmp_path / "runs").is_dir() or not any((tmp_path / "runs").iterdir())
