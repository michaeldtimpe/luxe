"""Tests for LuxeConfig.cache_dir() resolution + dispatch wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from luxe_cli.registry import LuxeConfig


def _cfg(**overrides) -> LuxeConfig:
    base = dict(agents=[])
    base.update(overrides)
    return LuxeConfig(**base)


def test_cache_dir_default_is_local_cache_under_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = _cfg()
    p = cfg.cache_dir()
    assert p == tmp_path / "local-cache"
    assert p.is_dir()


def test_cache_dir_honors_absolute_path(tmp_path):
    target = tmp_path / "elsewhere"
    cfg = _cfg(local_cache_dir=str(target))
    p = cfg.cache_dir()
    assert p == target
    assert p.is_dir()


def test_cache_dir_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = _cfg(local_cache_dir="~/my-cache")
    p = cfg.cache_dir()
    assert p == tmp_path / "my-cache"
    assert p.is_dir()


def test_cache_dir_creates_missing_dir(tmp_path):
    target = tmp_path / "fresh" / "nested"
    cfg = _cfg(local_cache_dir=str(target))
    assert not target.exists()
    cfg.cache_dir()
    assert target.is_dir()


def test_real_agents_yaml_resolves_local_cache(tmp_path, monkeypatch):
    """The shipped config defaults to local-cache/, not cwd."""
    monkeypatch.chdir(tmp_path)
    from luxe_cli.registry import load_config

    cfg = load_config()
    p = cfg.cache_dir()
    assert p.name == "local-cache"
    assert p.parent == tmp_path
