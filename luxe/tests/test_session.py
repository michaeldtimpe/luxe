"""Tests for Session.prune() and the OSError guard on read_all()."""

from __future__ import annotations

import json
from pathlib import Path

from cli.session import Session


def _make(tmp_path: Path, n: int) -> Session:
    sess = Session.new(tmp_path, first_prompt="test")
    for i in range(n):
        sess.append({"role": "user", "content": f"m{i}"})
    return sess


def test_prune_removes_oldest_keeps_last(tmp_path):
    sess = _make(tmp_path, 10)
    removed = sess.prune(max_turns=3)
    assert removed == 7
    events = sess.read_all()
    assert len(events) == 3
    # Oldest kept should be m7; newest m9.
    contents = [e["content"] for e in events]
    assert contents == ["m7", "m8", "m9"]


def test_prune_noop_when_under_cap(tmp_path):
    sess = _make(tmp_path, 5)
    removed = sess.prune(max_turns=200)
    assert removed == 0
    assert len(sess.read_all()) == 5


def test_prune_default_max_turns_200(tmp_path):
    sess = _make(tmp_path, 5)
    # Default max_turns=200 — small session stays untouched.
    removed = sess.prune()
    assert removed == 0


def test_read_all_missing_path_returns_empty(tmp_path):
    sess = Session(path=tmp_path / "does-not-exist.jsonl", session_id="nope")
    assert sess.read_all() == []


def test_read_all_skips_bad_json_lines(tmp_path):
    path = tmp_path / "sess.jsonl"
    path.write_text(
        '{"role": "user", "content": "ok"}\n'
        'not-json\n'
        '{"role": "assistant", "content": "also-ok"}\n'
    )
    sess = Session(path=path, session_id="sess")
    events = sess.read_all()
    assert len(events) == 2
    assert events[0]["content"] == "ok"
    assert events[1]["content"] == "also-ok"


def test_read_all_returns_empty_on_oserror(tmp_path, monkeypatch):
    # Simulate a file that exists() returns True for but open() fails.
    path = tmp_path / "fake.jsonl"
    path.write_text("{}\n")
    sess = Session(path=path, session_id="fake")

    def _boom(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", _boom)
    # Must not raise; returns empty.
    assert sess.read_all() == []


def test_prune_is_atomic(tmp_path):
    # After prune, the .jsonl.tmp tempfile must not linger.
    sess = _make(tmp_path, 300)
    sess.prune(max_turns=50)
    leftovers = list(tmp_path.glob("*.jsonl.tmp"))
    assert leftovers == []
    # And the on-disk content roundtrips cleanly.
    reloaded = [json.loads(l) for l in sess.path.read_text().splitlines() if l.strip()]
    assert len(reloaded) == 50
