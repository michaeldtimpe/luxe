"""Tests for pipeline data models."""

from luxe.pipeline.model import PipelineRun, StageMetrics, Status, Subtask


def test_subtask_defaults():
    sub = Subtask(title="Test", role="worker_read")
    assert sub.status == Status.PENDING
    assert sub.expected_tools == 3


def test_pipeline_run_events():
    run = PipelineRun(goal="test", task_type="review")
    run.add_event("start", detail="testing")
    assert len(run.events) == 1
    assert run.events[0]["kind"] == "start"
    assert "ts" in run.events[0]


def test_stage_summary():
    run = PipelineRun(goal="test", task_type="review")
    run.subtasks = [
        Subtask(title="A", role="worker_read", metrics=StageMetrics(
            wall_s=5.0, prompt_tokens=100, completion_tokens=50, tool_calls=3,
            model="model-a",
        )),
        Subtask(title="B", role="worker_read", metrics=StageMetrics(
            wall_s=3.0, prompt_tokens=80, completion_tokens=40, tool_calls=2,
            model="model-a",
        )),
        Subtask(title="C", role="worker_analyze", metrics=StageMetrics(
            wall_s=10.0, prompt_tokens=200, completion_tokens=100, tool_calls=5,
            model="model-b",
        )),
    ]
    summary = run.stage_summary
    assert summary["worker_read"].wall_s == 8.0
    assert summary["worker_read"].tool_calls == 5
    assert summary["worker_analyze"].wall_s == 10.0
