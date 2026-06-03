"""Tests for the actionable startup build hint (local git refs only, no network)."""

from __future__ import annotations

import pytest

from luxe import buildinfo


def _fake_git(mapping):
    def _git(*args):
        return mapping.get(args)
    return _git


_BEHIND = ("rev-list", "--count", "HEAD..origin/main")
_UPSTREAM = ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
_AHEAD = ("rev-list", "--count", "@{upstream}..HEAD")
_DIRTY = ("status", "--porcelain")


@pytest.mark.parametrize("mapping,expected", [
    # behind wins over everything
    ({_BEHIND: "3", _UPSTREAM: "origin/main", _AHEAD: "1", _DIRTY: " M x"},
     "3 behind origin/main — git pull"),
    # ahead (with upstream) when not behind
    ({_BEHIND: "0", _UPSTREAM: "origin/feat", _AHEAD: "2", _DIRTY: ""},
     "2 ahead — git push"),
    # dirty when not behind/ahead
    ({_BEHIND: "0", _UPSTREAM: "origin/feat", _AHEAD: "0", _DIRTY: " M x"},
     "uncommitted changes"),
    # no upstream + dirty → dirty (must not raise on missing @{upstream})
    ({_BEHIND: "0", _UPSTREAM: None, _DIRTY: "?? new"},
     "uncommitted changes"),
    # no upstream + clean → None
    ({_BEHIND: "0", _UPSTREAM: None, _DIRTY: ""},
     None),
    # fully clean & current → None
    ({_BEHIND: "0", _UPSTREAM: "origin/main", _AHEAD: "0", _DIRTY: ""},
     None),
])
def test_build_status_hint(monkeypatch, mapping, expected):
    monkeypatch.setattr(buildinfo, "_git", _fake_git(mapping))
    assert buildinfo.build_status_hint() == expected


@pytest.mark.parametrize("mapping,expected", [
    ({("rev-parse", "--short", "HEAD"): "abc1234", _DIRTY: " M x"}, ("abc1234", True)),
    ({("rev-parse", "--short", "HEAD"): "abc1234", _DIRTY: ""}, ("abc1234", False)),
])
def test_version_parts(monkeypatch, mapping, expected):
    monkeypatch.setattr(buildinfo, "_git", _fake_git(mapping))
    assert buildinfo.version_parts() == expected


def test_version_parts_no_git_falls_back(monkeypatch):
    monkeypatch.setattr(buildinfo, "_git", lambda *a: None)
    sha, dirty = buildinfo.version_parts()
    assert dirty is False and sha  # static __version__, not dirty


def test_build_status_hint_no_upstream_does_not_query_ahead(monkeypatch):
    # When @{upstream} is absent we must NOT run the ahead count (it would error).
    calls = []
    base = {_BEHIND: "0", _UPSTREAM: None, _DIRTY: ""}

    def _git(*args):
        calls.append(args)
        return base.get(args)

    monkeypatch.setattr(buildinfo, "_git", _git)
    assert buildinfo.build_status_hint() is None
    assert _AHEAD not in calls
