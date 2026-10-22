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
])
def test_ordinary_and_keep_listed_names_are_not_secret(name):
    assert not secrets.is_secret_name(name)


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
