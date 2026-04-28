"""Tests for config loading and validation."""

from pathlib import Path

from luxe.config import load_config


def test_load_default_config(config_path: Path):
    cfg = load_config(config_path)
    assert cfg.omlx_base_url.startswith("http")
    assert "architect" in cfg.roles
    assert "worker_read" in cfg.roles
    assert "validator" in cfg.roles
    assert "synthesizer" in cfg.roles


def test_model_for_role(config_path: Path):
    cfg = load_config(config_path)
    model = cfg.model_for_role("architect")
    assert "7B" in model or "7b" in model.lower()


def test_task_types(config_path: Path):
    cfg = load_config(config_path)
    assert "review" in cfg.task_types
    assert "implement" in cfg.task_types
    review = cfg.task_type("review")
    assert "architect" in review.pipeline
    assert "synthesizer" in review.pipeline


def test_role_configs(config_path: Path):
    cfg = load_config(config_path)
    arch = cfg.role("architect")
    assert arch.max_steps > 0
    assert arch.tools == []

    worker = cfg.role("worker_read")
    assert "read_file" in worker.tools
    assert "grep" in worker.tools
