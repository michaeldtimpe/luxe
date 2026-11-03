"""LUXE_BASH_NOOP (opt-in, 2026-11): trailing no-op suffixes on bench bash.

The champion types `npm test 2>&1`, `... | tail -40`, `... || true`; each is
refused, each refusal is an idle call, and three end the run with no report
(acceptance/validation_context_ab_2026_11). Pins: the suffix grammar, that
OFF is byte-identical, that hint mode never executes anything, that strip
mode runs only a validated PREFIX of the request (a chain hidden before the
suffix is still refused and never runs), and the counters maintain records.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from luxe.tools import fs, shell


@pytest.fixture
def repo(tmp_path: Path):
    fs.set_repo_root(tmp_path)
    shell.set_noop_mode(None)
    shell.set_bench_venv_path(False)
    yield tmp_path
    shell.set_noop_mode(None)
    shell.set_bench_venv_path(False)
    fs._REPO_ROOT = None


@pytest.mark.parametrize("cmd,cleaned", [
    ("npm test 2>&1", "npm test"),
    ("npm test --silent 2>&1 || true", "npm test --silent"),
    ("npm test 2>&1 | tail -40", "npm test"),
    ("pytest -q | head -n 50", "pytest -q"),
    ("pytest -q|tail -5", "pytest -q"),
    ("pytest -q | tail", "pytest -q"),
    ("cargo test --quiet 2>&1 | tail --lines=20", "cargo test --quiet"),
])
def test_suffix_grammar_strips_only_known_trailing_noops(cmd, cleaned):
    got, reasons = shell.strip_noop_suffixes(cmd)
    assert got == cleaned and reasons


@pytest.mark.parametrize("cmd", [
    'grep "a | tail -5" f',      # inside quotes: not a suffix
    "cat x > out",               # a real redirect (writes a file)
    "echo a && touch X",         # a chain
    "pytest 2>/dev/null",        # not in the grammar
    "2>&1",                      # nothing left
    "npm test | grep FAIL",      # pipe to something else
])
def test_suffix_grammar_leaves_everything_else(cmd):
    got, reasons = shell.strip_noop_suffixes(cmd)
    assert reasons == [] or got == ""


def test_off_is_byte_identical(repo):
    shell.set_noop_mode(None)
    out, err = shell._bash({"command": "npm test 2>&1"})
    assert out == ""
    assert err == ("Shell chain/redirect operators not allowed: ['>&']. Issue "
                   "separate bash calls for each command, or use "
                   "read_file/write_file/edit_file instead of redirects.")
    assert shell.noop_stats() == {}


@pytest.mark.parametrize("mode", ["", "on", "1", "STRIP"])
def test_unknown_mode_is_off(mode):
    shell.set_noop_mode(mode)
    assert shell._noop_mode is None


def test_hint_names_the_command_and_runs_nothing(repo):
    shell.set_noop_mode("hint")
    out, err = shell._bash(
        {"command": "python -c \"open('ran', 'w').write('x')\" 2>&1 | tail -5"})
    assert out == ""
    assert err.startswith("Shell chain/redirect operators not allowed:")
    assert "stderr is already included" in err and "clipped" in err
    assert "Run instead: `python -c \"open('ran', 'w').write('x')\"`" in err
    assert not (repo / "ran").exists()
    assert shell.noop_stats() == {"hint": 1}


def test_strip_runs_the_cleaned_command_with_a_note(repo):
    # echo/ls, not python: m5's non-interactive PATH has no `python` (127)
    shell.set_noop_mode("strip")
    out, err = shell._bash({"command": "echo hello 2>&1 || true"})
    assert err is None
    assert out.startswith("[luxe ran `echo hello`")
    assert out.endswith("hello\n")
    assert shell.noop_stats() == {"strip": 1}


def test_strip_keeps_the_exit_code(repo):
    shell.set_noop_mode("strip")
    out, err = shell._bash({"command": "ls definitely-missing-file 2>&1"})
    assert err and err.startswith("exit code ") and err != "exit code 0"
    assert err != "exit code 127"
    assert out.startswith("[luxe ran `ls definitely-missing-file`")


@pytest.mark.parametrize("cmd", [
    "echo a; touch X 2>&1",
    "echo a && touch X | tail -5",
    "touch X 2>&1",                   # not allowlisted once cleaned
    "echo $(touch X) 2>&1",
])
def test_strip_never_rescues_a_chain_or_a_disallowed_binary(repo, cmd):
    shell.set_noop_mode("strip")
    out, err = shell._bash({"command": cmd})
    assert out == "" and err
    assert not (repo / "X").exists()
    assert "strip" not in shell.noop_stats()


def test_unrestricted_path_is_untouched(repo):
    shell.set_noop_mode("strip")
    out, err = shell._bash({"command": "echo a 2>&1"}, unrestricted=True)
    assert err is None and out == "a\n"


def test_cancellable_runner_carries_the_note(repo):
    shell.set_noop_mode("strip")
    token = SimpleNamespace(requested=False)  # the chat cancel-token shape
    out, err = shell._bash({"command": "echo hi 2>&1"}, cancel=token)
    assert err is None
    assert out.startswith("[luxe ran `echo hi`") and out.endswith("hi\n")


# --- LUXE_BENCH_PY_PATH (opt-in, 2026-11) ------------------------------------

def test_bench_env_unchanged_when_off(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    shell.set_bench_venv_path(False)
    assert shell._bench_env()["PATH"] == "/usr/bin:/bin"


def test_bench_env_puts_the_venv_bin_first_when_on(monkeypatch):
    import sys
    from pathlib import Path as _P
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    shell.set_bench_venv_path(True)
    try:
        path = shell._bench_env()["PATH"]
    finally:
        shell.set_bench_venv_path(False)
    assert path == str(_P(sys.executable).parent) + ":/usr/bin:/bin"


def test_python_resolves_with_a_python_less_path(repo, monkeypatch):
    """The m5 case: a PATH with no unversioned `python`."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    shell.set_bench_venv_path(True)
    try:
        out, err = shell._bash({"command": "python -c \"print('py-ok')\""})
    finally:
        shell.set_bench_venv_path(False)
    assert err is None and out.strip() == "py-ok"
