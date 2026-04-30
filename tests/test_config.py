"""Tests for config loading and validation."""

from pathlib import Path

import pytest

from luxe.config import RoleConfig, load_config


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


# --- prompt-shaping bake-off RoleConfig extensions --

def test_role_config_prompt_shaping_defaults():
    """system_prompt_id, task_prompt_id, repeat_penalty must default such
    that existing configs (which omit them entirely) load unchanged."""
    rc = RoleConfig(model_key="x")
    assert rc.system_prompt_id == "baseline"
    assert rc.task_prompt_id == "baseline"
    assert rc.repeat_penalty is None


def test_role_config_prompt_shaping_overrides_round_trip():
    """Explicit overrides must round-trip through model_dump/model_validate
    so YAML overlays from the bench harness preserve them."""
    rc = RoleConfig(
        model_key="x",
        system_prompt_id="cot",
        task_prompt_id="cot",
        repeat_penalty=1.05,
        temperature=0.3,
    )
    dumped = rc.model_dump()
    rc2 = RoleConfig.model_validate(dumped)
    assert rc2.system_prompt_id == "cot"
    assert rc2.task_prompt_id == "cot"
    assert rc2.repeat_penalty == 1.05
    assert rc2.temperature == 0.3


def test_existing_yaml_loads_without_new_fields(config_path: Path):
    """The shipped configs/single_64gb.yaml does not list the new fields;
    loading must succeed and use defaults."""
    cfg = load_config(config_path)
    mono = cfg.role("monolith")
    assert mono.system_prompt_id == "baseline"
    assert mono.task_prompt_id == "baseline"
    assert mono.repeat_penalty is None


def test_role_config_repeat_penalty_accepts_float(tmp_path: Path):
    """A YAML overlay setting `repeat_penalty: 1.05` must parse as float
    (mirrors what `make_overlay()` writes for prompt-shaping cells)."""
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "omlx_base_url: http://127.0.0.1:8000\n"
        "models: {monolith: Test-Model}\n"
        "roles:\n"
        "  monolith:\n"
        "    model_key: monolith\n"
        "    tools: [read_file]\n"
        "    repeat_penalty: 1.05\n"
        "    system_prompt_id: cot\n"
        "task_types:\n"
        "  implement: {description: x, pipeline: [monolith]}\n"
    )
    cfg = load_config(overlay)
    mono = cfg.role("monolith")
    assert mono.repeat_penalty == 1.05
    assert mono.system_prompt_id == "cot"
