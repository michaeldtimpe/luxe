"""Secrets never reach a subprocess the MODEL controls (2026-10-22).

Threat model: a model — a backdoored fine-tune, a poisoned chat template, or
a prompt injection carried in by a file or a web page — steers the agent into
reading credentials and sending them somewhere. luxe never exports a secret
into its own environment, but the wrappers that launch it do (`luxe-web` runs
`set -a; . ~/.luxe/secrets.env`, and so do the bench launch scripts), and every
model-driven subprocess used to inherit that environment whole. The bash
allowlist includes `python`/`pip`/`npx`/`make`, so even "allowlisted" bash
could read `os.environ` and reach the network.

`luxe.secrets.scrubbed_env()` is the one place that decides what a
model-driven child may see. It is a DENYLIST by design: an allowlist would
break JAVA_HOME / GOPATH / CARGO_HOME / … in arbitrary projects, and the
fallback kit has to work in whatever repo it is pointed at.

Every value here is FAKE; the suite runs under the hermetic HOME conftest
provides, so the real ~/.luxe/secrets.env is never read.
"""

from __future__ import annotations

import inspect
import io
import sys
from pathlib import Path

import pytest

import luxe.secrets as secrets
from luxe.tools import fs, shell

FAKE = "abcdef1234567890-fake"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "absent.env")
    secrets.clear_cache()
    yield
    secrets.clear_cache()


# --- is_secret_name ----------------------------------------------------------

@pytest.mark.parametrize("name", [
    "OMLX_API_KEY", "OMLX_API_KEY_M5", "BRAVE_API_KEY", "BRAVE_ANSWERS_API_KEY",
    "RELAY_TOKEN_ALPHA", "GITHUB_TOKEN", "GH_TOKEN", "AWS_SECRET_ACCESS_KEY",
    "DB_PASSWORD", "MYSQL_PASSWD", "GOOGLE_APPLICATION_CREDENTIALS",
    "SSH_PRIVATE_KEY_DATA", "SESSION_COOKIE", "AZURE_DEVOPS_PAT",
    "NPM_CONFIG__AUTH", "REGISTRY_AUTH", "APIKEY", "github_token",
    "KEYCHAIN_PASSWORD",   # a benign word does not shield a real hit
])
def test_credential_shaped_names_are_secret(name):
    assert secrets.is_secret_name(name)


@pytest.mark.parametrize("name", [
    "PATH", "HOME", "USER", "LANG", "SHELL", "TMPDIR", "JAVA_HOME", "GOPATH",
    "CARGO_HOME", "RUSTUP_HOME", "VIRTUAL_ENV", "PYTHONPATH", "NODE_PATH",
    # the explicit keep-list / benign-word list
    "SSH_AUTH_SOCK", "GPG_TTY", "GPG_AGENT_INFO", "TOKENIZERS_PARALLELISM",
    "GOPRIVATE", "GONOPRIVATE", "KEYCHAIN_PATH", "KEYTIMEOUT",
    # git's env config protocol: the name holds a config KEY, not a secret
    "GIT_CONFIG_KEY_0", "GIT_CONFIG_KEY_12",
    "UV_KEYRING_PROVIDER", "PYTHON_KEYRING_BACKEND",
])
def test_ordinary_and_keep_listed_names_are_not_secret(name):
    assert not secrets.is_secret_name(name)


def test_git_env_config_survives_the_scrub(tmp_path, monkeypatch):
    """The bench pins `commit.gpgsign=false` through GIT_CONFIG_COUNT +
    GIT_CONFIG_KEY_0 + GIT_CONFIG_VALUE_0. Scrubbing only the KEY half left
    git reading a count with no key — `fatal: unable to parse command-line
    config` on every git call, which `pr.is_dirty` reports as a dirty tree,
    so every bench run died at preflight (m5 no-harm A/B, 2026-10-22)."""
    from luxe import pr
    from tests._git import git
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("a\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "init")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    env = secrets.scrubbed_env()
    assert env["GIT_CONFIG_KEY_0"] == "commit.gpgsign"
    assert not pr.is_dirty(repo)


def test_every_name_in_secrets_env_is_secret(tmp_path, monkeypatch):
    """A name that matches no pattern is still a secret if the operator put
    it in secrets.env — that file IS the list of things not to leak."""
    f = tmp_path / "secrets.env"
    f.write_text("BROWSERBASE_PROJECT_ID=proj-0000-fake\nexport ODDLY_NAMED=x\n")
    monkeypatch.setattr(secrets, "SECRETS_PATH", f)
    secrets.clear_cache()
    assert secrets.is_secret_name("BROWSERBASE_PROJECT_ID")
    assert secrets.is_secret_name("ODDLY_NAMED")


def test_a_name_luxe_resolved_is_secret(monkeypatch):
    monkeypatch.setattr(secrets, "_from_keychain", lambda name: "")
    monkeypatch.setenv("UNPATTERNED_RELAY", "whatever-value")
    assert not secrets.is_secret_name("UNPATTERNED_RELAY")
    secrets.resolve_api_key("UNPATTERNED_RELAY")
    assert secrets.is_secret_name("UNPATTERNED_RELAY")


# --- scrubbed_env ------------------------------------------------------------

def test_scrubbed_env_drops_secrets_and_keeps_the_rest():
    base = {"PATH": "/bin", "HOME": "/h", "JAVA_HOME": "/j", "GOPATH": "/g",
            "CARGO_HOME": "/c", "SSH_AUTH_SOCK": "/s", "OMLX_API_KEY": FAKE,
            "GITHUB_TOKEN": FAKE, "DB_PASSWORD": FAKE}
    out = secrets.scrubbed_env(base)
    assert set(out) == {"PATH", "HOME", "JAVA_HOME", "GOPATH", "CARGO_HOME",
                        "SSH_AUTH_SOCK"}
    assert "OMLX_API_KEY" in base, "the input mapping must not be mutated"


def test_scrubbed_env_defaults_to_the_process_env(monkeypatch):
    monkeypatch.setenv("FAKE_TEST_TOKEN", FAKE)
    monkeypatch.setenv("FAKE_PLAIN_VAR", "visible")
    out = secrets.scrubbed_env()
    assert "FAKE_TEST_TOKEN" not in out
    assert out["FAKE_PLAIN_VAR"] == "visible"
    assert out.get("PATH") == __import__("os").environ.get("PATH")


# --- the real model-driven spawns see the scrubbed env -----------------------

@pytest.fixture
def repo(tmp_repo: Path, monkeypatch):
    fs.set_repo_root(tmp_repo)
    monkeypatch.setenv("FAKE_TEST_TOKEN", FAKE)
    monkeypatch.setenv("FAKE_PLAIN_VAR", "visible")
    yield tmp_repo
    fs._REPO_ROOT = None


_PROBE = 'echo "[$FAKE_TEST_TOKEN][$FAKE_PLAIN_VAR]"'


def test_benchmark_bash_runs_with_secrets_scrubbed(repo):
    out, err = shell.TOOL_FNS["bash"]({"command": _PROBE})
    assert err is None
    assert out.strip() == "[][visible]"


def test_chat_allowlisted_bash_runs_with_secrets_scrubbed(repo):
    out, err = shell.make_bash_fn()({"command": _PROBE})
    assert err is None
    assert out.strip() == "[][visible]"


def test_chat_unrestricted_bash_runs_with_secrets_scrubbed(repo):
    out, err = shell.make_bash_fn(unrestricted=True)(
        {"command": "env | grep '^FAKE_' | sort"})
    assert err is None
    assert "FAKE_TEST_TOKEN" not in out
    assert "FAKE_PLAIN_VAR=visible" in out


def test_chat_cancellable_bash_runs_with_secrets_scrubbed(repo):
    class _Tok:
        requested = False
    out, err = shell.make_bash_fn(cancel=_Tok())({"command": _PROBE})
    assert err is None
    assert out.strip() == "[][visible]"


def test_chat_bash_keeps_the_venv_path_prefix(repo):
    env = shell._chat_bash_env()
    assert env["PATH"].startswith(str(Path(sys.executable).parent))
    leaked = "FAKE_TEST_TOKEN" in env    # (never let pytest print the env)
    assert not leaked


def test_the_bench_bash_tool_description_is_unchanged():
    """Golden-pinned: scrubbing changes the spawn, never the advertised tool."""
    (d,) = shell.tool_defs()
    assert d.description == (
        "Run a shell command (allowlisted binaries: "
        + ", ".join(sorted(shell._ALLOWLIST)) + "). Scoped to repo root.")


def test_analysis_tools_run_with_secrets_scrubbed(repo):
    from luxe.tools import analysis

    out, err = analysis._run_tool(
        [sys.executable, "-c",
         "import os; print('T=' + os.environ.get('FAKE_TEST_TOKEN', '')); "
         "print('P=' + os.environ.get('FAKE_PLAIN_VAR', ''))"])
    assert err is None
    assert FAKE not in out
    assert "P=visible" in out


def test_maintain_test_step_runs_with_secrets_scrubbed(repo):
    from luxe import pr as pr_mod
    from luxe.pr import PRConfig, PRState, RunSpec

    spec = RunSpec(run_id="scrub01", goal="g", task_type="bugfix",
                   repo_path=str(repo), base_sha="", base_branch="main")
    state = PRState()
    state.test_command = 'test -z "$FAKE_TEST_TOKEN" && test -n "$FAKE_PLAIN_VAR"'
    pr_mod._do_test(spec, state, PRConfig(test_commands=[], test_timeout_s=30))
    assert state.test_passed is True, state.test_output_tail


def test_spec_validator_tests_pass_runs_with_secrets_scrubbed(repo):
    from luxe import spec_validator
    from luxe.spec import Requirement

    req = Requirement(id="R1", must="m", done_when="d", kind="tests_pass",
                      command='test -z "$FAKE_TEST_TOKEN" && test -n "$FAKE_PLAIN_VAR"')
    assert spec_validator._eval_tests_pass(req, repo).satisfied is True


def test_gitchange_verify_runs_with_secrets_scrubbed(repo):
    from rich.console import Console

    from luxe.gitkit import apply as apply_mod

    ok, tail = apply_mod._run_verify(
        "sh -c 'test -z \"$FAKE_TEST_TOKEN\" && test -n \"$FAKE_PLAIN_VAR\"'",
        repo, timeout=30, console=Console(file=io.StringIO()),
        reader=lambda _p: "y")
    assert ok is True, tail


def test_smoke_code_drill_pytest_runs_with_secrets_scrubbed():
    """The drill runs pytest over code the model just edited. Asserted on the
    source (the drill needs a live model to reach that line)."""
    from luxe.chat import smoke

    src = inspect.getsource(smoke)
    i = src.index('"-m", "pytest", "-q"]')
    assert "env=scrubbed_env()" in src[i:i + 200]


# --- luxe's OWN git calls run with the scrubbed env --------------------------
#
# A hook (`.git/hooks/*`) or a config-driven command (`diff.external`,
# `core.sshCommand`, filters) runs inside git's environment the next time luxe
# itself calls git. fs write tools refuse `.git/**` (test_tools.py), but a
# model with bash — or a repo that arrived poisoned — can still plant one, so
# every git luxe spawns for itself gets `scrubbed_env()` as its base. argv and
# config pins are unchanged; `gh` is NOT scrubbed (it runs no repo hooks and
# may need GH_TOKEN); the keep-list (SSH_AUTH_SOCK, GPG_TTY) still passes, so
# signing and ssh push keep working.

_HOOK_PROBE = ('#!/bin/sh\n'
               'echo "[$FAKE_TEST_TOKEN][$FAKE_PLAIN_VAR][$SSH_AUTH_SOCK]" '
               '> "{mark}"\n')
_ID = ("-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false")


def _plant(repo: Path, tmp_path: Path, hook: str) -> Path:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    mark = tmp_path / f"mark-{hook}"
    h = repo / ".git" / "hooks" / hook
    h.write_text(_HOOK_PROBE.format(mark=mark))
    h.chmod(0o755)
    return mark


@pytest.fixture
def git_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_TEST_TOKEN", FAKE)
    monkeypatch.setenv("FAKE_PLAIN_VAR", "visible")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    repo = tmp_path / "g"
    repo.mkdir()
    return repo


_SCRUBBED = "[][visible][/tmp/fake-agent.sock]"


def test_gitcmd_run_in_hooks_see_the_scrubbed_env(git_env, tmp_path):
    from luxe import gitcmd

    mark = _plant(git_env, tmp_path, "pre-commit")
    (git_env / "a.txt").write_text("a\n")
    gitcmd.run_in(git_env, "add", "a.txt")
    proc = gitcmd.run_in(git_env, *_ID, "commit", "-qm", "x")
    assert proc.returncode == 0, proc.stderr
    assert mark.read_text().strip() == _SCRUBBED


def test_gitcmd_run_hooks_see_the_scrubbed_env(git_env, tmp_path):
    from luxe import gitcmd

    mark = _plant(git_env, tmp_path, "pre-commit")
    (git_env / "a.txt").write_text("a\n")
    gitcmd.run(git_env, "add", "a.txt")
    proc = gitcmd.run(git_env, *_ID, "commit", "-qm", "x")
    assert proc.returncode == 0, proc.stderr
    assert mark.read_text().strip() == _SCRUBBED


def test_git_diff_tool_external_diff_sees_the_scrubbed_env(git_env, tmp_path):
    """`diff.external` planted in .git/config runs under the READ-ONLY
    git_diff tool."""
    import subprocess

    from luxe import gitcmd
    from luxe.tools import git as git_tools

    _plant(git_env, tmp_path, "unused")
    mark = tmp_path / "mark-ext"
    ext = tmp_path / "ext.sh"
    ext.write_text(_HOOK_PROBE.format(mark=mark))
    ext.chmod(0o755)
    (git_env / "a.txt").write_text("a\n")
    gitcmd.run_in(git_env, "add", "a.txt")
    gitcmd.run_in(git_env, *_ID, "commit", "-qm", "x")
    subprocess.run(["git", "-C", str(git_env), "config", "diff.external",
                    str(ext)], check=True)
    (git_env / "a.txt").write_text("b\n")
    fs.set_repo_root(git_env)
    try:
        git_tools.TOOL_FNS["git_diff"]({})
    finally:
        fs._REPO_ROOT = None
    assert mark.read_text().strip() == _SCRUBBED


def test_gitcmd_parse_env_is_scrubbed(git_env):
    from luxe import gitcmd

    env = gitcmd.parse_env()
    leaked = "FAKE_TEST_TOKEN" in env
    assert not leaked
    assert env["FAKE_PLAIN_VAR"] == "visible"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"


def test_gitclone_env_is_scrubbed(git_env):
    from luxe.gitclone import clone_env

    env = clone_env()
    leaked = "FAKE_TEST_TOKEN" in env
    assert not leaked
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["SSH_AUTH_SOCK"] == "/tmp/fake-agent.sock"


def test_pr_flow_git_commit_hook_sees_the_scrubbed_env(git_env, tmp_path):
    from luxe import pr as pr_mod

    mark = _plant(git_env, tmp_path, "pre-commit")
    (git_env / "a.txt").write_text("a\n")
    pr_mod._run(["git", "add", "a.txt"], cwd=git_env)
    r = pr_mod._run(["git", *_ID, "commit", "-qm", "x"], cwd=git_env)
    assert r.ok, r.stderr
    assert mark.read_text().strip() == _SCRUBBED


def test_pr_flow_gh_is_not_scrubbed(git_env, monkeypatch):
    """gh runs no repo hooks and may authenticate from GH_TOKEN."""
    import subprocess

    from luxe import pr as pr_mod

    seen: list = []

    def _spy(cmd, **kw):
        seen.append((cmd[0], kw.get("env")))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(pr_mod.subprocess, "run", _spy)
    pr_mod._run(["gh", "api", "user"], cwd=git_env)
    pr_mod._run_net(["gh", "pr", "checks", "1"], cwd=git_env)
    pr_mod._run_net(["git", "push", "-u", "origin", "b"], cwd=git_env)
    pr_mod._run(["git", "status"], cwd=git_env)
    (gh1, e1), (gh2, e2), (g1, e3), (g2, e4) = seen
    # (booleans, so a failure never makes pytest print an environment)
    gh_inherits = e1 is None
    gh_net_unscrubbed = (e2 is not None and e2.get("FAKE_TEST_TOKEN") == FAKE
                         and e2.get("GIT_TERMINAL_PROMPT") == "0")
    git_scrubbed = all(e is not None and "FAKE_TEST_TOKEN" not in e
                       for e in (e3, e4))
    git_net_prompt_off = e3 is not None and e3.get("GIT_TERMINAL_PROMPT") == "0"
    assert (gh1, gh2, g1, g2) == ("gh", "gh", "git", "git")
    assert gh_inherits and gh_net_unscrubbed
    assert git_scrubbed and git_net_prompt_off


def test_every_literal_git_spawn_in_luxe_passes_an_env():
    """Structural backstop: any `subprocess.run/Popen(["git", …])` in
    src/luxe must pass `env=` (gitcmd's helpers and pr._run supply the scrub
    for every other git call; they are pinned behaviourally above)."""
    import ast

    import luxe

    root = Path(luxe.__file__).parent
    missing = []
    for py in sorted(root.rglob("*.py")):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("run", "Popen", "check_output")
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.args and isinstance(node.args[0], ast.List)
                    and node.args[0].elts
                    and isinstance(node.args[0].elts[0], ast.Constant)
                    and node.args[0].elts[0].value == "git"):
                continue
            # `**common` (spec_validator) carries env inside the splat.
            if not any(k.arg in ("env", None) for k in node.keywords):
                missing.append(f"{py.relative_to(root)}:{node.lineno}")
    assert missing == []
