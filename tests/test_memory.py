"""Tests for the chat memory package (session persistence + project memory).

All tests isolate ~/.luxe by pointing HOME at a tmp dir, and use a tmp-dir
`repo_root` mock (never a real repo).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from luxe.memory import project, session


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Path.home() reads HOME on POSIX; confirm isolation took.
    assert Path.home() == home
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    return r


# --- project memory -------------------------------------------------------


def test_project_hash_deterministic_12_chars(repo: Path):
    h1 = project.project_hash(repo)
    h2 = project.project_hash(repo)
    assert h1 == h2
    assert len(h1) == 12


def test_load_memory_empty_by_default(repo: Path):
    mem = project.load_memory(repo)
    assert mem.is_empty()
    assert project.render_block(mem) == ""


def test_curated_memory_md_is_injected(repo: Path):
    (repo / ".luxe").mkdir()
    project.repo_memory_file(repo).write_text("Always run `pytest -q`.\nUse ruff.\n")
    mem = project.load_memory(repo)
    block = project.render_block(mem)
    assert "<project_memory>" in block
    assert "Always run `pytest -q`." in block
    assert "Use ruff." in block


def test_auto_fact_not_injected_until_promoted(repo: Path):
    f = project.add_fact(repo, "prefer concise output", kind="pref")
    assert f.confidence == "auto"
    mem = project.load_memory(repo)
    # auto fact present in store but NOT injected
    assert any(x.id == f.id for x in mem.facts)
    assert mem.injected_facts == []
    assert project.render_block(mem) == ""

    # promote → now injected
    assert project.promote_fact(repo, f.id) is True
    mem2 = project.load_memory(repo)
    assert len(mem2.injected_facts) == 1
    block = project.render_block(mem2)
    assert "prefer concise output" in block


def test_promote_missing_fact_returns_false(repo: Path):
    assert project.promote_fact(repo, "nope") is False


def test_forget_fact(repo: Path):
    f = project.add_fact(repo, "x", confidence="manual")
    assert project.forget_fact(repo, f.id) is True
    mem = project.load_memory(repo)
    assert mem.facts == []


def test_user_added_manual_fact_injects_immediately(repo: Path):
    project.add_fact(repo, "this repo uses uv", source="user", confidence="manual")
    mem = project.load_memory(repo)
    assert "this repo uses uv" in project.render_block(mem)


def test_does_not_read_repo_root_claude_md(repo: Path):
    """memory.sdd: must not read the repo-root CLAUDE.md."""
    (repo / "CLAUDE.md").write_text("SECRET_CLAUDE_INSTRUCTION should never leak\n")
    mem = project.load_memory(repo)
    block = project.render_block(mem)
    assert "SECRET_CLAUDE_INSTRUCTION" not in block
    assert mem.is_empty()


def test_render_block_caps_length(repo: Path):
    (repo / ".luxe").mkdir()
    project.repo_memory_file(repo).write_text("z" * 10_000)
    mem = project.load_memory(repo)
    block = project.render_block(mem, max_chars=500)
    # body capped; tags add a little overhead
    assert len(block) < 600


# --- non-UTF-8 memory.md: must sanitize at the boundary, not just survive ---
#
# 2026-08-24: a `.luxe/memory.md` starting with gzip magic (0x1f 0x8b) raised
# UnicodeDecodeError out of a bare read_text(). Reading it losslessly with
# errors="surrogateescape" fixes the crash but is NOT enough on its own for
# `load_memory`/`render_block`: their output escapes this module into a chat
# request body. httpx encodes JSON bodies with ensure_ascii=False then
# `.encode("utf-8")` (httpx/_content.py:177-178), which raises
# UnicodeEncodeError on a lone surrogate — so text handed to a caller must
# already be valid UTF-8. `load_memory` uses errors="replace" for exactly
# this reason; these tests pin the property httpx actually needs, not just
# "it didn't raise on read."


def _write_undecodable_memory_md(repo: Path) -> bytes:
    (repo / ".luxe").mkdir(exist_ok=True)
    raw = b"\x1f\x8b\x08\x00" + b"not valid utf-8: \xff\xfe\x80\x81" * 4
    project.repo_memory_file(repo).write_bytes(raw)
    return raw


def test_load_memory_curated_md_is_wire_safe_on_undecodable_bytes(repo: Path):
    """The exact operation httpx performs on a JSON request body."""
    _write_undecodable_memory_md(repo)
    mem = project.load_memory(repo)
    mem.curated_md.encode("utf-8")            # must not raise


def test_render_block_is_wire_safe_on_undecodable_bytes(repo: Path):
    raw = _write_undecodable_memory_md(repo)
    mem = project.load_memory(repo)
    block = project.render_block(mem)
    block.encode("utf-8")                      # must not raise
    assert "<project_memory>" in block          # still rendered, not dropped


def test_splice_block_still_preserves_undecodable_bytes_on_disk(repo: Path):
    """The property the read/replace vs read/surrogateescape split has to
    preserve: splice_block's own round trip stays byte-exact even though
    load_memory (a different, lossy reader of the same file) does not."""
    raw = _write_undecodable_memory_md(repo)
    project.splice_block(repo, "notes", "a fresh note", stamp="")
    after = project.repo_memory_file(repo).read_bytes()
    assert after.startswith(raw)
    assert b"<!-- luxe:notes begin" in after


# --- session persistence --------------------------------------------------


def test_new_session_round_trip(repo: Path):
    meta = session.new_session(repo_path=str(repo), title="t")
    session.append_turn(meta.session_id, "user", text="hello")
    session.append_turn(meta.session_id, "assistant", text="hi", run_id="r1")
    loaded = session.load_session(meta.session_id)
    assert loaded is not None
    m2, records = loaded
    assert m2.session_id == meta.session_id
    assert m2.repo_path == str(repo)
    assert [r["kind"] for r in records] == ["user", "assistant"]
    assert records[0]["text"] == "hello"
    assert records[1]["run_id"] == "r1"


def test_load_missing_session_returns_none():
    assert session.load_session("deadbeef") is None


def test_append_fold_records_version(repo: Path):
    meta = session.new_session(repo_path=str(repo))
    session.append_fold(meta.session_id, 0, "trunc-v1", "[user] hi")
    fold = session.session_dir(meta.session_id) / "fold.jsonl"
    assert fold.is_file()
    assert "trunc-v1" in fold.read_text()


def test_list_sessions_orders_recent_first(repo: Path):
    a = session.new_session(repo_path=str(repo), title="a")
    time.sleep(0.01)
    b = session.new_session(repo_path=str(repo), title="b")
    ids = [m.session_id for m in session.list_sessions()]
    assert ids[0] == b.session_id
    assert a.session_id in ids


def test_gc_sessions_keeps_recent_and_drops_old(repo: Path):
    # An old session (last_active far in the past) outside keep_recent gets dropped.
    old = session.new_session(repo_path=str(repo), title="old")
    meta = session.load_meta(old.session_id)
    meta.last_active = time.time() - 60 * 86400  # 60 days ago
    session._write_meta(meta)

    fresh = session.new_session(repo_path=str(repo), title="fresh")

    removed = session.gc_sessions(keep_recent=1, retention_days=30)
    assert removed == 1
    remaining = {m.session_id for m in session.list_sessions()}
    assert fresh.session_id in remaining
    assert old.session_id not in remaining


def test_gc_keeps_old_session_if_within_keep_recent(repo: Path):
    old = session.new_session(repo_path=str(repo))
    meta = session.load_meta(old.session_id)
    meta.last_active = time.time() - 60 * 86400
    session._write_meta(meta)
    # keep_recent large enough to protect it despite age
    removed = session.gc_sessions(keep_recent=50, retention_days=30)
    assert removed == 0
    assert session.load_meta(old.session_id) is not None
