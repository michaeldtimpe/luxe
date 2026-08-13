"""Claude Code diagnostics — pure classifier, probe plumbing, and the chat tool.

No test touches the real Keychain, the real process table, or a real `claude`
install: subprocess.run and the filesystem are stubbed, and `classify()` runs
against canned `Facts`.

The secrecy invariants are load-bearing and tested here, not just documented:
this module parses `ps eww` output (which carries EVERY environment variable's
VALUE) and settings `env:` blocks (a documented place to put an API key), so a
regression that leaks a value is a credential disclosure, not a display bug.
"""

from __future__ import annotations

import json

import pytest

from luxe import claudecode as cc
from luxe.claudecode import (
    Auth,
    Facts,
    Install,
    Proc,
    SessionInfo,
    SettingsFile,
    classify,
    make_claude_code_tool,
)


# --- canned facts ------------------------------------------------------------


def _facts(**kw) -> Facts:
    """A healthy baseline: installed, OAuth login, no overrides, no sessions."""
    base = dict(
        install=Install(present=True, bin_path="/x/claude", version="2.1.231"),
        auth=Auth(oauth_keychain=True, account_email="a@b.c",
                  billing_type="stripe_subscription"),
        settings=[SettingsFile(path="/h/.claude/settings.json", exists=True)],
    )
    base.update(kw)
    return Facts(**base)


def _session(*names: str, readable: bool = True) -> Proc:
    return Proc(pid=101, etime="10:00", argv="claude", kind="session",
                auth_env=list(names), env_readable=readable)


# --- classifier (pure) -------------------------------------------------------


def test_healthy_host_is_ok():
    assert classify(_facts()) == cc.CC_OK


def test_missing_binary_outranks_everything():
    facts = _facts(install=Install(present=False),
                   settings=[SettingsFile(path="/h/s.json", exists=True,
                                          valid=False, error="boom")])
    assert classify(facts) == cc.CC_NOT_INSTALLED


def test_unparseable_settings_is_a_finding():
    facts = _facts(settings=[SettingsFile(path="/h/s.json", exists=True,
                                          valid=False, error="invalid JSON")])
    assert classify(facts) == cc.CC_SETTINGS_INVALID


def test_missing_settings_file_is_not_a_finding():
    facts = _facts(settings=[SettingsFile(path="/h/s.json", exists=False)])
    assert classify(facts) == cc.CC_OK


def test_live_session_with_api_key_is_the_headline():
    """The 2026-08-13 case: a session silently on the Platform API key."""
    facts = _facts(procs=[_session("ANTHROPIC_API_KEY")])
    assert classify(facts) == cc.CC_API_KEY_SESSION


def test_auth_token_counts_as_the_api_path():
    facts = _facts(procs=[_session("ANTHROPIC_AUTH_TOKEN")])
    assert classify(facts) == cc.CC_API_KEY_SESSION


def test_clean_live_session_stays_ok():
    facts = _facts(procs=[_session()])
    assert classify(facts) == cc.CC_OK


def test_helper_process_env_is_not_a_session_finding():
    """Background plumbing (daemon/bg-pty-host) is not an interactive session;
    it must not raise the api-key verdict."""
    helper = Proc(pid=9, etime="1:00", argv="claude daemon run", kind="helper",
                  auth_env=["ANTHROPIC_API_KEY"])
    assert classify(_facts(procs=[helper])) == cc.CC_OK


@pytest.mark.parametrize("name", ["ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                                  "CLAUDE_CODE_USE_VERTEX"])
def test_endpoint_redirect_outranks_a_credential_swap(name):
    facts = _facts(procs=[_session("ANTHROPIC_API_KEY", name)])
    assert classify(facts) == cc.CC_GATEWAY_OVERRIDE


def test_gateway_override_seen_in_settings_env_block():
    facts = _facts(auth=Auth(oauth_keychain=True,
                             settings_env=["ANTHROPIC_BASE_URL"]))
    assert classify(facts) == cc.CC_GATEWAY_OVERRIDE


def test_ambient_key_warns_about_the_next_launch():
    facts = _facts(auth=Auth(oauth_keychain=True,
                             ambient_env=["ANTHROPIC_API_KEY"]))
    assert classify(facts) == cc.CC_API_KEY_AMBIENT


def test_keychain_key_alone_is_inert():
    """`claude-api`'s stored key does nothing until that wrapper injects it —
    reporting it as an active override would cry wolf on every host."""
    facts = _facts(auth=Auth(oauth_keychain=True, api_key_keychain=True))
    assert classify(facts) == cc.CC_OK


def test_no_credential_anywhere():
    assert classify(_facts(auth=Auth())) == cc.CC_NO_AUTH


def test_api_key_helper_counts_as_a_credential_source():
    assert classify(_facts(auth=Auth(api_key_helper=True))) == cc.CC_OK


def test_unreachable_api_reported_when_probed():
    assert classify(_facts(net_verdict="tls-blocked")) == cc.CC_UNREACHABLE


def test_degraded_network_is_not_unreachable():
    assert classify(_facts(net_verdict="degraded")) == cc.CC_OK


def test_live_api_key_session_outranks_unreachable():
    facts = _facts(procs=[_session("ANTHROPIC_API_KEY")],
                   net_verdict="tls-blocked")
    assert classify(facts) == cc.CC_API_KEY_SESSION


def test_every_verdict_has_advice():
    for verdict in (cc.CC_NOT_INSTALLED, cc.CC_SETTINGS_INVALID,
                    cc.CC_GATEWAY_OVERRIDE, cc.CC_API_KEY_SESSION,
                    cc.CC_UNREACHABLE, cc.CC_NO_AUTH, cc.CC_API_KEY_AMBIENT,
                    cc.CC_OK):
        assert cc._ADVICE[verdict].strip()


# --- rendering ---------------------------------------------------------------


def _render(facts: Facts) -> str:
    report = cc.ClaudeCodeReport(facts=facts, verdict=classify(facts))
    report.advice = cc._ADVICE[report.verdict]
    return "\n".join(text for _, text in cc.render_lines(report))


def test_render_names_the_relaunch_fix_for_a_live_api_session():
    out = _render(_facts(procs=[_session("ANTHROPIC_API_KEY")]))
    assert "Platform API key" in out
    assert "claude-plan" in out


def test_render_flags_an_unreadable_process_environment():
    """Unknown must not render as clean — `ps` showing no environment is a
    different fact from "no auth variables set"."""
    out = _render(_facts(procs=[_session(readable=False)]))
    assert "UNKNOWN" in out


def test_render_flags_mid_session_model_change():
    session = SessionInfo(session_id="abcdef1234", assistant_turns=4,
                          models=["claude-opus-5", "claude-sonnet-5"])
    assert "CHANGED mid-session" in _render(_facts(sessions=[session]))


def test_render_does_not_flag_a_single_model_session():
    session = SessionInfo(session_id="abcdef1234", assistant_turns=4,
                          models=["claude-opus-5"], versions=["2.1.231"])
    assert "CHANGED mid-session" not in _render(_facts(sessions=[session]))


def test_render_never_emits_a_secret_value(monkeypatch):
    """Belt and braces: feed values everywhere a value could survive and assert
    none reaches the output. Only NAMES are allowed through."""
    secret = "sk-ant-SUPERSECRET"
    facts = _facts(
        auth=Auth(oauth_keychain=True, ambient_env=["ANTHROPIC_API_KEY"],
                  settings_env=["ANTHROPIC_BASE_URL"], approved_keys=2),
        procs=[_session("ANTHROPIC_API_KEY")],
        settings=[SettingsFile(path="/h/s.json", exists=True,
                               env_keys=["ANTHROPIC_API_KEY"])],
    )
    out = _render(facts)
    assert secret not in out
    assert "ANTHROPIC_API_KEY" in out          # the NAME is the finding


# --- probe plumbing (subprocess + filesystem stubbed) ------------------------


def test_proc_auth_env_returns_names_only(monkeypatch):
    """`ps eww` hands us every variable's VALUE. Only the matched NAMES may
    leave this function — a value must never be returned or stored."""
    line = ("claude --dangerously-skip-permissions HOME=/Users/x "
            "ANTHROPIC_API_KEY=sk-ant-SUPERSECRET OMLX_API_KEY=hunter2 "
            "PATH=/usr/bin")
    monkeypatch.setattr(cc, "_run", lambda argv: (line, "", 0))
    names, readable = cc._proc_auth_env(1234)
    assert names == ["ANTHROPIC_API_KEY"]
    assert readable is True
    assert not any("SUPERSECRET" in n or "hunter2" in n for n in names)


def test_proc_auth_env_marks_unreadable_environment(monkeypatch):
    monkeypatch.setattr(cc, "_run", lambda argv: ("", "", 1))
    assert cc._proc_auth_env(1234) == ([], False)


def test_proc_auth_env_command_without_environment_is_unreadable(monkeypatch):
    """ps can print the command with no env at all (hardened/other-user
    process). That is UNKNOWN, not "no variables set"."""
    monkeypatch.setattr(cc, "_run", lambda argv: ("claude --resume", "", 0))
    assert cc._proc_auth_env(1234) == ([], False)


@pytest.mark.parametrize("argv,kind", [
    ("claude --dangerously-skip-permissions", "session"),
    ("/Users/x/.local/bin/claude", "session"),
    ("claude daemon run --json-path /x", "helper"),
    ("claude bg-pty-host --bg-pty-host /tmp/x", "helper"),
    ("/bin/zsh -c 'echo claude'", None),
    ("", None),
])
def test_classify_argv(argv, kind):
    assert cc._classify_argv(argv) == kind


def test_keychain_entry_never_reads_the_secret(monkeypatch):
    """Metadata-only: the lookup must not pass `-w`, which would decrypt the
    credential (and prompt for Keychain access)."""
    seen: list[list[str]] = []
    out = ('    "mdat"<timedate>=0x3230323630383133 '
           '"20260813141147Z\\000"\n')

    def fake_run(argv):
        seen.append(argv)
        return out, "", 0

    monkeypatch.setattr(cc, "_run", fake_run)
    exists, modified = cc._keychain_entry("Claude Code-credentials")
    assert exists is True
    assert modified == "2026-08-13 14:11Z"
    assert all("-w" not in argv for argv in seen)


def test_keychain_entry_absent(monkeypatch):
    monkeypatch.setattr(cc, "_run", lambda argv: ("", "not found", 44))
    assert cc._keychain_entry("nope") == (False, "")


def test_settings_env_block_yields_keys_not_values(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "model": "opus",
        "env": {"ANTHROPIC_API_KEY": "sk-ant-SUPERSECRET", "FOO": "bar"},
        "permissions": {"defaultMode": "auto"},
    }))
    monkeypatch.setenv("HOME", str(home))
    files = cc._probe_settings(None)
    top = next(f for f in files if f.path.endswith(".claude/settings.json"))
    assert top.valid is True
    assert top.env_keys == ["ANTHROPIC_API_KEY", "FOO"]
    assert top.model == "opus"
    assert top.permission_mode == "auto"
    assert "SUPERSECRET" not in repr(files)


def test_invalid_settings_reported_with_the_parse_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"model": "opus",}')
    monkeypatch.setenv("HOME", str(home))
    top = next(f for f in cc._probe_settings(None) if f.exists)
    assert top.valid is False
    assert "line" in top.error


def test_sessions_read_metadata_only(tmp_path, monkeypatch):
    """The transcript summary must carry configuration facts and NOT a single
    byte of what was said."""
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-Users-x-repo"
    proj.mkdir(parents=True)
    records = [
        {"type": "user", "timestamp": "2026-08-13T10:00:00Z",
         "cwd": "/Users/x/repo", "gitBranch": "main", "version": "2.1.230",
         "message": {"role": "user", "content": "MY PRIVATE QUESTION"}},
        {"type": "assistant", "timestamp": "2026-08-13T10:00:05Z",
         "effort": "high", "version": "2.1.230",
         "message": {"model": "claude-opus-5",
                     "content": [{"type": "text", "text": "MY PRIVATE ANSWER"}]}},
        {"type": "assistant", "timestamp": "2026-08-13T10:05:00Z",
         "version": "2.1.231",
         "message": {"model": "<synthetic>", "content": []}},
    ]
    (proj / "sess-1.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n")
    monkeypatch.setenv("HOME", str(home))

    sessions, errors = cc._probe_sessions()
    assert errors == []
    s = sessions[0]
    assert s.session_id == "sess-1"
    assert s.cwd == "/Users/x/repo"
    assert s.branch == "main"
    assert s.assistant_turns == 2
    assert s.models == ["claude-opus-5"]          # <synthetic> excluded
    assert s.efforts == ["high"]
    assert s.versions == ["2.1.230", "2.1.231"]   # CLI updated mid-session
    assert s.started == "2026-08-13T10:00:00Z"
    assert s.ended == "2026-08-13T10:05:00Z"
    blob = repr(sessions)
    assert "PRIVATE QUESTION" not in blob
    assert "PRIVATE ANSWER" not in blob


def test_sessions_missing_projects_dir_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cc._probe_sessions() == ([], [])


def test_tail_lines_bounds_a_huge_transcript(tmp_path):
    path = tmp_path / "big.jsonl"
    path.write_text("".join(f'{{"n": {i}}}\n' for i in range(200_000)))
    lines, truncated = cc._tail_lines(path, max_bytes=4096)
    assert truncated is True
    assert len(lines) < 500
    # every surviving line is whole (the partial head line is dropped)
    assert all(json.loads(line) for line in lines)


# --- chat tool ---------------------------------------------------------------


def test_tool_returns_verdict_and_json(monkeypatch):
    report = cc.ClaudeCodeReport(facts=_facts(procs=[_session("ANTHROPIC_API_KEY")]))
    report.verdict = cc.CC_API_KEY_SESSION
    report.advice = cc._ADVICE[cc.CC_API_KEY_SESSION]
    monkeypatch.setattr(cc, "full_report", lambda **kw: report)

    defn, fn = make_claude_code_tool()
    assert defn.name == "claude_code_diag"
    out, err = fn({"check": "status"})
    assert err is None
    assert out.startswith(f"verdict: {cc.CC_API_KEY_SESSION}")
    payload = json.loads(out.split("\n", 2)[2])
    assert payload["live_sessions"][0]["auth_env"] == ["ANTHROPIC_API_KEY"]


def test_tool_never_raises(monkeypatch):
    def _boom(**kw):
        raise OSError(60, "Operation timed out")

    monkeypatch.setattr(cc, "full_report", _boom)
    _, fn = make_claude_code_tool()
    out, err = fn({})
    assert out == ""
    # errno 60 maps to the OSError subclass TimeoutError — assert on the
    # message, which is what the model actually reads.
    assert "Operation timed out" in err


def test_full_report_status_does_no_network(monkeypatch):
    """`check="status"` is the offline form — it must not touch netdiag."""
    from luxe import netdiag

    def _boom(*a, **k):
        raise AssertionError("run_ladder must not be called for check=status")

    monkeypatch.setattr(netdiag, "run_ladder", _boom)
    monkeypatch.setattr(cc, "resolve_bin", lambda: None)
    report = cc.full_report(check="status")
    assert report.verdict == cc.CC_NOT_INSTALLED
    assert report.facts.net_verdict == ""
