"""Split the two powers: a one-line warning when web + bash meet (chat-only).

A model holding BOTH web tools and a shell can fetch content from the
internet and execute it — the shape a prompt-injected page needs. luxe does
not block the combination (an operator may genuinely want it); it says so,
once, at the moment the combination becomes active: `/web` with write mode on,
`/write` with web on, `/bash` (unrestricted) with both on, or a startup posture
(`--web` with `--dev`/`luxe code`) that begins there.

Bash is exposed only in write mode (`make_read_only_role` strips it), so
"bash available" == `write_enabled`.
"""

from __future__ import annotations

import inspect
import io

import pytest
from rich.console import Console

from luxe.chat import cmd_toggles
from luxe.chat.session import ChatSession


class _Ctx:
    def __init__(self, **flags):
        self.session = ChatSession(repo_path="", project_hash="")
        for k, v in flags.items():
            setattr(self.session, k, v)
        self._buf = io.StringIO()
        self.console = Console(file=self._buf, width=400)

    @property
    def out(self) -> str:
        return self._buf.getvalue()


@pytest.fixture(autouse=True)
def _quiet_web(monkeypatch):
    from luxe.web import answers as answers_mod
    from luxe.web import search as search_mod
    monkeypatch.setattr(search_mod, "configured", lambda: False)
    monkeypatch.setattr(answers_mod, "configured", lambda: False)


def _warned(ctx) -> bool:
    return "fetch" in ctx.out and "execute" in ctx.out


def test_notice_is_empty_unless_both_powers_are_on():
    s = ChatSession(repo_path="", project_hash="")
    assert s.fetch_and_execute_notice() == ""
    s.web_enabled = True
    assert s.fetch_and_execute_notice() == ""
    s.web_enabled, s.write_enabled = False, True
    assert s.fetch_and_execute_notice() == ""
    s.unrestricted_bash = True          # /bash alone is inert without /write…
    s.write_enabled = False
    s.web_enabled = True
    assert s.fetch_and_execute_notice() == ""
    s.write_enabled = True
    assert "UNRESTRICTED" in s.fetch_and_execute_notice()
    s.unrestricted_bash = False
    note = s.fetch_and_execute_notice()
    assert note and "\n" not in note and "UNRESTRICTED" not in note


def test_web_on_with_write_on_warns():
    ctx = _Ctx(write_enabled=True)
    cmd_toggles._web_mode("", ctx)
    assert _warned(ctx)


def test_web_on_read_only_does_not_warn():
    ctx = _Ctx()
    cmd_toggles._web_mode("", ctx)
    assert not _warned(ctx)


def test_web_off_does_not_warn():
    ctx = _Ctx(write_enabled=True, web_enabled=True)
    cmd_toggles._web_mode("", ctx)
    assert not _warned(ctx)


def test_write_on_with_web_on_warns():
    ctx = _Ctx(web_enabled=True)
    cmd_toggles._write([], ctx)
    assert _warned(ctx)


def test_write_on_without_web_does_not_warn():
    ctx = _Ctx()
    cmd_toggles._write([], ctx)
    assert not _warned(ctx)


def test_unrestricted_bash_escalation_warns_again():
    ctx = _Ctx(web_enabled=True, write_enabled=True)
    cmd_toggles._bash_mode([], ctx)
    assert _warned(ctx) and "UNRESTRICTED" in ctx.out


def test_unrestricted_bash_without_write_does_not_warn():
    ctx = _Ctx(web_enabled=True)
    cmd_toggles._bash_mode([], ctx)
    assert not _warned(ctx)


def test_bash_back_to_allowlisted_does_not_warn():
    ctx = _Ctx(web_enabled=True, write_enabled=True, unrestricted_bash=True)
    cmd_toggles._bash_mode([], ctx)
    assert not _warned(ctx)


def test_startup_posture_warns_in_both_front_ends():
    from luxe.chat import controller, repl, tui

    s = ChatSession(repo_path="", project_hash="")
    assert controller.fetch_execute_notice_markup(s) == ""
    s.web_enabled = s.write_enabled = True
    assert "fetch" in controller.fetch_execute_notice_markup(s)
    assert "fetch_execute_notice_markup(" in inspect.getsource(repl)
    assert "fetch_execute_notice_markup(" in inspect.getsource(tui)
