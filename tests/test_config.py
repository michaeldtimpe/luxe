"""Tests for config loading and validation."""

from pathlib import Path

from luxe.config import load_config


def test_load_default_config(config_path: Path):
    cfg = load_config(config_path)
    assert cfg.omlx_base_url.startswith("http")
    assert "monolith" in cfg.roles


def test_model_for_role(config_path: Path):
    cfg = load_config(config_path)
    model = cfg.model_for_role("monolith")
    assert model  # non-empty model id


def test_task_types(config_path: Path):
    cfg = load_config(config_path)
    assert "review" in cfg.task_types
    assert "implement" in cfg.task_types
    review = cfg.task_type("review")
    assert "monolith" in review.pipeline


def test_role_configs(config_path: Path):
    cfg = load_config(config_path)
    mono = cfg.role("monolith")
    assert mono.max_steps > 0
    assert "read_file" in mono.tools
    assert "edit_file" in mono.tools
