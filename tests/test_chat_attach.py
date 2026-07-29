"""Tests for `/attach` — one-shot file attachments injected into the next turn.

Caps/truncation/binary/missing at the command layer; <attached_files>
placement + one-shot clearing at the session layer; transcript provenance.
Follows the test_chat_commands conventions (FakeBackend on the slots module,
isolated HOME, StringIO Console).
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from luxe.chat import commands as cmd
from luxe.chat import slots as slots_mod
from luxe.chat.session import ChatSession
from luxe.config import PipelineConfig, RoleConfig
from luxe.memory import session as session_store


class FakeBackend:
    def __init__(self, base_url="", model="", timeout_s=600.0, api_key=""):
        self.base_url = base_url
        self.model = model

    def unload_all_loaded(self, *, except_for=None):
        return {}


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    monkeypatch.setattr(slots_mod, "Backend", FakeBackend)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture
def ctx(tmp_path: Path):
    cfg = PipelineConfig(
        models={"monolith": "Champ"},
        roles={"monolith": RoleConfig(model_key="monolith")},
    )
    out = io.StringIO()
    console = Console(file=out, force_terminal=False, width=120)
    session = ChatSession()
    c = cmd.CommandContext(console=console, session=session,
                           slots=slots_mod.SlotManager(cfg))
    c._out = out  # type: ignore[attr-defined]
    return c


def _text(ctx) -> str:
    return ctx._out.getvalue()


# --- command layer: reading, caps, refusals ---------------------------------


def test_attach_reads_file_and_stages(ctx, tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("remember the milk")
    cmd.dispatch(f"/attach {f}", ctx)
    assert len(ctx.session.attachments) == 1
    a = ctx.session.attachments[0]
    assert a["path"] == str(f)
    assert a["content"] == "remember the milk"
    assert a["size"] == len(b"remember the milk")
    assert a["sha256"] == hashlib.sha256(b"remember the milk").hexdigest()
    assert a["truncated"] is False
    assert "attached" in _text(ctx) and "NEXT turn" in _text(ctx)


def test_attach_multiple_paths_one_command(ctx, tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("aaa")
    f2.write_text("bbb")
    cmd.dispatch(f"/attach {f1} {f2}", ctx)
    assert [a["content"] for a in ctx.session.attachments] == ["aaa", "bbb"]


def test_attach_expands_home(ctx, tmp_path):
    home = Path(tmp_path / "home")  # the isolated HOME fixture dir
    f = home / "in_home.txt"
    f.write_text("home sweet home")
    cmd.dispatch("/attach ~/in_home.txt", ctx)
    assert ctx.session.attachments
    assert ctx.session.attachments[0]["content"] == "home sweet home"


def test_attach_missing_file_warns(ctx):
    cmd.dispatch("/attach /nope/definitely-missing.txt", ctx)
    assert ctx.session.attachments == []
    assert "no such file" in _text(ctx)


def test_attach_binary_refused(ctx, tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"PNG\x00\x01\x02 not text")
    cmd.dispatch(f"/attach {f}", ctx)
    assert ctx.session.attachments == []
    assert "binary" in _text(ctx)


def test_attach_truncates_at_file_cap(ctx, tmp_path):
    f = tmp_path / "big.log"
    f.write_text("x" * (cmd.ATTACH_MAX_FILE_BYTES + 5000))
    cmd.dispatch(f"/attach {f}", ctx)
    a = ctx.session.attachments[0]
    assert a["truncated"] is True
    assert a["content"].startswith("x" * 100)
    assert "truncated" in a["content"]          # explicit marker in-content
    assert len(a["content"]) < cmd.ATTACH_MAX_FILE_BYTES + 200
    assert a["size"] == cmd.ATTACH_MAX_FILE_BYTES + 5000  # original size kept


def test_attach_total_cap_skips_overflow(ctx, tmp_path):
    files = []
    for i in range(4):  # 4 × 48KB > 128KB total → the last ones must be skipped
        f = tmp_path / f"f{i}.txt"
        f.write_text("y" * cmd.ATTACH_MAX_FILE_BYTES)
        files.append(str(f))
    cmd.dispatch("/attach " + " ".join(files), ctx)
    total = sum(len(a["content"]) for a in ctx.session.attachments)
    assert total <= cmd.ATTACH_MAX_TOTAL_BYTES
    assert len(ctx.session.attachments) == 2
    assert "total attachment cap" in _text(ctx)


def test_attach_bare_shows_usage_or_pending(ctx, tmp_path):
    cmd.dispatch("/attach", ctx)
    assert "Usage" in _text(ctx)
    f = tmp_path / "a.txt"
    f.write_text("z")
    cmd.dispatch(f"/attach {f}", ctx)
    cmd.dispatch("/attach", ctx)
    assert "pending attachments" in _text(ctx)


def test_attach_listed_in_help(ctx):
    cmd.dispatch("/help", ctx)
    assert "/attach" in _text(ctx)


# --- session layer: injection placement + one-shot --------------------------


def test_attached_files_block_sits_below_system_constraints():
    s = ChatSession()
    s.system_constraints.append("answer in French")
    s.attachments.append({"path": "/tmp/a.txt", "content": "file body",
                          "size": 9, "sha256": "s", "truncated": False})
    ctx_text, _ = s.build_extra_context("what does it say?")
    i_sys = ctx_text.index("<system_constraints>")
    i_att = ctx_text.index("<attached_files>")
    i_style = ctx_text.index("<response_style>")  # terse default ON
    assert i_sys < i_att < i_style
    assert '<file path="/tmp/a.txt">' in ctx_text
    assert "file body" in ctx_text


def test_attached_files_top_when_no_constraints():
    s = ChatSession()
    s.attachments.append({"path": "/tmp/a.txt", "content": "body",
                          "size": 4, "sha256": "s", "truncated": False})
    ctx_text, _ = s.build_extra_context("hi")
    stripped = ctx_text.lstrip()
    assert stripped.startswith("<attached_files>")


def test_attachments_are_one_shot():
    s = ChatSession()
    s.attachments.append({"path": "/tmp/a.txt", "content": "body",
                          "size": 4, "sha256": "s", "truncated": False})
    first, _ = s.build_extra_context("turn 1")
    assert "<attached_files>" in first
    assert s.attachments == []                       # cleared on consumption
    second, _ = s.build_extra_context("turn 2")
    assert "<attached_files>" not in second


# --- transcript provenance ---------------------------------------------------


def test_attach_records_transcript_entry(ctx, tmp_path):
    meta = session_store.new_session()
    ctx.session.session_id = meta.session_id
    f = tmp_path / "a.txt"
    f.write_text("payload")
    cmd.dispatch(f"/attach {f}", ctx)

    tp = session_store.session_dir(meta.session_id) / "transcript.jsonl"
    records = [json.loads(l) for l in tp.read_text().splitlines()]
    att = [r for r in records if r["kind"] == "attachment"]
    assert len(att) == 1
    r = att[0]
    assert r["path"] == str(f)
    assert r["size"] == 7
    assert r["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert r["content"] == "payload"
    assert r["truncated"] is False


def test_resume_pairing_ignores_attachment_records(ctx, tmp_path):
    from luxe.chat import resume as resume_mod

    meta = session_store.new_session()
    session_store.append_turn(meta.session_id, "user", text="q", slot="chat")
    session_store.append_turn(meta.session_id, "attachment", path="/x",
                              size=1, sha256="s", content="c")
    session_store.append_turn(meta.session_id, "assistant", text="a", run_id="r0")
    s = ChatSession()
    out = Console(file=io.StringIO(), force_terminal=False, width=100)
    assert resume_mod.resume_into(meta.session_id, s, out) is True
    assert len(s.turns) == 1
    assert s.turns[0].user == "q" and s.turns[0].assistant == "a"
