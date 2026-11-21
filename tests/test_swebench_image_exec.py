"""SWE-bench image exec (LUXE_SWEBENCH_EXEC, 2026-11-20; tools.sdd).

The bench-only switch that runs the model's python/pytest/pip in the
instance's SWE-bench eval image, consumes a leading `cd <dir> &&`, and lets
the adapter allow scratch reproducers then strip them from the patch.
A fake `docker` on PATH records argv + stdin, so no colima is needed.
"""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

from luxe.tools import fs, shell
from tests._git import git

IMAGE = "swebench/sweb.eval.x86_64.demo_1776_demo-1:latest"

# docker gets only PATH/HOME/DOCKER_HOST from _run_in_image (by design), so
# the fake reads its directory, canned output and rc from files, not env.
FAKE_DOCKER = """#!/bin/sh
D=@DIR@
printf '%s\\n' "$@" > "$D/argv"
if [ "$1" = run ]; then cat > "$D/stdin.tar"; fi
if [ -f "$D/out" ]; then cat "$D/out"; else printf 'fake output'; fi
exit "$(cat "$D/rc" 2>/dev/null || echo 0)"
"""


@pytest.fixture
def repo(tmp_path: Path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    (r / "pkg").mkdir()
    (r / "pkg" / "mod.py").write_text("x = 1\n")
    (r / "pkg" / "gone.py").write_text("y = 2\n")
    (r / "README").write_text("hi\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    base = git(r, "rev-parse", "HEAD")
    fs.set_repo_root(r)
    yield r, base
    fs._REPO_ROOT = None
    shell.set_image_exec(None, None)


@pytest.fixture
def docker(tmp_path: Path, monkeypatch):
    d = tmp_path / "fakedocker"
    d.mkdir()
    exe = d / "docker"
    exe.write_text(FAKE_DOCKER.replace("@DIR@", str(d)))
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:{__import__('os').environ['PATH']}")
    return d


def _argv(d: Path) -> list[str]:
    return (d / "argv").read_text().splitlines()


def _tar_names(d: Path) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO((d / "stdin.tar").read_bytes())) as t:
        return set(t.getnames())


# --- off: byte-identical, docker never called -------------------------------

def test_off_by_default_runs_python_on_host(repo, docker):
    shell.set_image_exec(None, None)
    out, err = shell._bash({"command": "echo hello"})
    assert out == "hello\n" and err is None
    shell._bash({"command": "python -c pass"})
    assert not (docker / "argv").exists()


def test_off_leaves_cd_chain_refused(repo, docker):
    shell.set_image_exec(None, None)
    out, err = shell._bash({"command": "cd pkg && ls"})
    assert out == "" and "operators not allowed" in err


def test_needs_both_image_and_base():
    shell.set_image_exec(IMAGE, None)
    assert shell._image_exec is None
    shell.set_image_exec(None, "abc")
    assert shell._image_exec is None


# --- on: routing ------------------------------------------------------------

def test_python_runs_in_image_with_changes_synced(repo, docker):
    r, base = repo
    (r / "pkg" / "mod.py").write_text("x = 2\n")          # tracked edit
    (r / "pkg" / "gone.py").unlink()                       # deletion
    (r / "repro_issue.py").write_text("print(1)\n")        # untracked scratch
    (r / "repo.sdd").write_text("synthetic\n")             # adapter's .sdd
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": "python repro_issue.py"})
    assert err is None
    assert out.startswith("[ran in the instance image at /testbed]\n")
    assert "fake output" in out
    argv = _argv(docker)
    assert argv[:2] == ["run", "--rm"]
    for flag in ("--network", "none", "--platform", "linux/amd64", IMAGE):
        assert flag in argv
    inner = argv[-1]
    assert "rm -f -- pkg/gone.py" in inner
    assert "source /opt/miniconda3/bin/activate testbed" in inner
    assert inner.endswith("&& python repro_issue.py")
    assert _tar_names(docker) == {"pkg/mod.py", "repro_issue.py"}
    calls = shell.image_exec_calls()
    assert calls[-1]["binary"] == "python" and calls[-1]["files_synced"] == 2


def test_nonzero_exit_keeps_output_and_error_contract(repo, docker):
    _, base = repo
    (docker / "out").write_text("FAILED test_x - AssertionError")
    (docker / "rc").write_text("1")
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": "pytest tests/test_x.py"})
    assert err == "exit code 1"
    assert "FAILED test_x - AssertionError" in out


def test_non_exec_binaries_stay_on_host(repo, docker):
    _, base = repo
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": "ls pkg"})
    assert err is None and "mod.py" in out
    assert not (docker / "argv").exists()


def test_daemon_down_is_a_loud_error_not_a_host_fallback(repo, docker):
    _, base = repo
    (docker / "out").write_text("docker: Cannot connect to the Docker daemon at unix:///x.")
    (docker / "rc").write_text("125")
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": "python -c pass"})
    assert out == "" and err.startswith("image exec unavailable:")


def test_missing_docker_cli_is_a_loud_error(repo, monkeypatch, tmp_path):
    import shutil
    _, base = repo
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": "pytest"})
    assert out == "" and err == "image exec unavailable: docker CLI not found"


def test_still_validated_and_allowlisted_when_on(repo, docker):
    _, base = repo
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": "python a.py; rm -rf /"})
    assert out == "" and "operators not allowed" in err
    out, err = shell._bash({"command": "curl http://x"})
    assert "not in allowlist" in err
    assert not (docker / "argv").exists()


# --- on: leading cd -----------------------------------------------------------

def test_cd_to_hallucinated_path_runs_at_repo_root(repo, docker):
    _, base = repo
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": "cd /home/user/repo && python -m pytest -x"})
    assert err is None
    assert out.startswith("[luxe ran `python -m pytest -x` from .: leading cd handled]\n")
    assert "cd /testbed && source" in _argv(docker)[-1]


def test_cd_to_subdir_sets_host_and_image_cwd(repo, docker):
    r, base = repo
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": "cd pkg && ls"})
    assert err is None and "mod.py" in out and "README" not in out
    shell._bash({"command": f"cd {r}/pkg && python -c pass"})
    assert "cd /testbed/pkg && source" in _argv(docker)[-1]
    shell._bash({"command": "cd /testbed/pkg && pytest"})
    assert "cd /testbed/pkg && source" in _argv(docker)[-1]


def test_cd_outside_repo_is_not_followed(repo, docker, tmp_path):
    _, base = repo
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": f"cd {tmp_path} && ls"})
    assert err is None and "pkg" in out          # ran at repo root


def test_only_one_leading_cd_is_consumed(repo, docker):
    _, base = repo
    shell.set_image_exec(IMAGE, base)
    out, err = shell._bash({"command": "cd pkg && ls && touch X"})
    assert out == "" and "operators not allowed" in err


# --- maintain wiring -----------------------------------------------------------

def test_only_the_swebench_adapter_sets_the_switch():
    root = Path(__file__).resolve().parents[1]
    setters = [p for p in list((root / "src").rglob("*.py"))
               + list((root / "benchmarks").rglob("*.py"))
               if '"LUXE_SWEBENCH_EXEC":' in p.read_text(errors="replace")]
    rel = sorted(str(p.relative_to(root)) for p in setters)
    assert rel == ["benchmarks/swebench/adapter.py"]
    maint = (root / "src/luxe/maintain.py").read_text()
    assert 'os.environ.get("LUXE_SWEBENCH_EXEC")' in maint   # reads, never sets


# --- adapter ---------------------------------------------------------------------

def test_instance_image_key():
    from benchmarks.swebench.adapter import instance_image
    assert (instance_image("psf__requests-2931")
            == "swebench/sweb.eval.x86_64.psf_1776_requests-2931:latest")


def test_exec_sdd_drops_only_the_create_globs():
    from benchmarks.swebench.adapter import SWEBENCH_SDD_BODY, SWEBENCH_SDD_BODY_EXEC
    assert "## Forbids creating" in SWEBENCH_SDD_BODY
    assert "## Forbids creating" not in SWEBENCH_SDD_BODY_EXEC
    assert "- repo_root/**" in SWEBENCH_SDD_BODY_EXEC


@pytest.mark.parametrize("path,scratch", [
    ("repro_issue.py", True),
    ("reproduce.py", True),
    ("anything.py", True),            # new .py at the root
    ("pkg/debug_x.py", True),
    ("pkg/tmp_out.txt", True),
    ("pkg/new_module.py", False),     # a real new module stays
    ("pkg/test_new_feature.py", False),
    ("docs/notes.md", False),
])
def test_is_scratch(path, scratch):
    from benchmarks.swebench.adapter import is_scratch
    assert is_scratch(path) is scratch


def test_strip_scratch_removes_added_scratch_only(repo):
    from benchmarks.swebench.adapter import extract_diff, strip_scratch
    r, base = repo
    (r / "pkg" / "mod.py").write_text("x = 2\n")
    (r / "pkg" / "new_module.py").write_text("z = 3\n")
    (r / "repro_issue.py").write_text("print(1)\n")      # untracked
    (r / "pkg" / "debug_trace.py").write_text("t\n")
    git(r, "add", "pkg/debug_trace.py")
    git(r, "commit", "-q", "-m", "luxe seals edits")      # committed scratch
    removed = strip_scratch(r, base)
    assert removed == ["pkg/debug_trace.py", "repro_issue.py"]
    diff = extract_diff(r, base)
    assert "pkg/mod.py" in diff and "pkg/new_module.py" in diff
    assert "repro_issue" not in diff and "debug_trace" not in diff


def test_run_refuses_image_exec_without_exec_config(monkeypatch, tmp_path):
    from benchmarks.swebench import run
    monkeypatch.setattr(sys, "argv", ["run", "--output", str(tmp_path), "--image-exec"])
    assert run.main() == 2
    monkeypatch.setattr(sys, "argv", ["run", "--output", str(tmp_path), "--config",
                                      "configs/single_64gb_swebench_exec.yaml"])
    assert run.main() == 2


def test_exec_config_routes_bugfix_to_exec_prompt():
    import yaml
    from luxe.agents.prompts import PROMPT_REGISTRY, get_overlay
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "configs/single_64gb_swebench_exec.yaml").read_text())
    overlay = cfg["roles"]["monolith"]["task_overlay_id"]
    assert get_overlay(overlay).by_task == {"bugfix": "swebench_bugfix_exec"}
    prefix = PROMPT_REGISTRY["swebench_bugfix_exec"].task_prefix
    assert "scratch" in prefix and "Do NOT create any new files" not in prefix
    base = yaml.safe_load((root / "configs/single_64gb_swebench.yaml").read_text())
    for c in (base, cfg):                                   # all else identical
        c.pop("profile")
        c["roles"]["monolith"].pop("task_overlay_id")
    assert base == cfg


def test_exec_config_is_keyed_on_overlay_not_file_name(tmp_path):
    """A step-budget copy of the exec config pairs with --image-exec; a
    renamed strict config does not (2026-11-20, ix60 follow-up)."""
    import yaml
    from benchmarks.swebench.run import is_exec_config
    root = Path(__file__).resolve().parents[1]
    assert is_exec_config(root / "configs/single_64gb_swebench_exec.yaml")
    assert is_exec_config(root / "configs/single_64gb_swebench_exec_60.yaml")
    assert not is_exec_config(root / "configs/single_64gb_swebench.yaml")
    strict = yaml.safe_load((root / "configs/single_64gb_swebench.yaml").read_text())
    fake = tmp_path / "single_64gb_swebench_exec.yaml"     # exec name, strict overlay
    fake.write_text(yaml.safe_dump(strict))
    assert not is_exec_config(fake)
    assert not is_exec_config(tmp_path / "missing.yaml")


def test_exec_60_config_differs_only_in_step_budget():
    import yaml
    root = Path(__file__).resolve().parents[1]
    a = yaml.safe_load((root / "configs/single_64gb_swebench_exec.yaml").read_text())
    b = yaml.safe_load((root / "configs/single_64gb_swebench_exec_60.yaml").read_text())
    assert a["roles"]["monolith"]["max_steps"] == 30
    assert b["roles"]["monolith"]["max_steps"] == 60
    for c in (a, b):
        c.pop("profile")
        c["roles"]["monolith"].pop("max_steps")
    assert a == b


# ── 2026-11-21: scratch in new directories; scratch writes and the idle exit ──

def test_strip_scratch_removes_new_top_level_dirs_but_not_new_packages(repo):
    from benchmarks.swebench.adapter import extract_diff, strip_scratch
    r, base = repo
    (r / "pkg" / "mod.py").write_text("x = 2\n")
    for p in ("test_repro/.a/foo.py", "test_repro/bar.py", "tmp/pylint_test/.a/foo.py",
              ".a/foo.py", "newpkg/__init__.py", "newpkg/core.py", "pkg/sub/data.txt"):
        (r / p).parent.mkdir(parents=True, exist_ok=True)
        (r / p).write_text("import re\n")
    removed = strip_scratch(r, base)
    assert removed == [".a/foo.py", "test_repro/.a/foo.py", "test_repro/bar.py",
                       "tmp/pylint_test/.a/foo.py"]
    assert not (r / "test_repro").exists() and not (r / "tmp").exists() and not (r / ".a").exists()
    diff = extract_diff(r, base)
    assert "pkg/mod.py" in diff and "newpkg/core.py" in diff and "pkg/sub/data.txt" in diff
    assert "test_repro" not in diff and "pylint_test" not in diff


def _idle_run(repo_root, write_path, image_exec, monkeypatch):
    import luxe.agents.loop as loop_mod
    from luxe.agents.loop import run_agent
    from luxe.backend import ChatResponse, GenerationTiming, ToolCallResponse
    from luxe.config import RoleConfig
    from luxe.tools.base import ToolDef
    from tests._fakes import ScriptedBackend

    def resp(name, args):
        return ChatResponse(text="", tool_calls=[ToolCallResponse(id=name, name=name, arguments=args)],
                            finish_reason="tool_calls",
                            timing=GenerationTiming(prompt_tokens=100, completion_tokens=50))
    scripted = [resp("write_file", {"path": write_path, "content": "x"})]
    scripted += [resp("lint", {"n": i}) for i in range(5)]
    scripted.append(ChatResponse(text="done", finish_reason="stop",
                                 timing=GenerationTiming(prompt_tokens=100, completion_tokens=10)))
    events = []
    monkeypatch.setattr(loop_mod, "append_event", lambda rid, kind, **kw: events.append(kind))
    shell.set_image_exec("img:latest" if image_exec else None,
                         git(repo_root, "rev-parse", "HEAD") if image_exec else None)
    backend = ScriptedBackend(scripted)
    run_agent(
        backend=backend,
        role_cfg=RoleConfig(model_key="t", num_ctx=4096, max_steps=30,
                            max_tokens_per_turn=2048, temperature=0.0),
        system_prompt="s", task_prompt="t",
        tool_defs=[ToolDef(name="write_file", description="w", parameters={
                       "type": "object", "properties": {"path": {"type": "string"},
                                                        "content": {"type": "string"}},
                       "required": ["path", "content"]}),
                   ToolDef(name="lint", description="l",
                           parameters={"type": "object", "properties": {"n": {"type": "integer"}},
                                       "required": []})],
        tool_fns={"write_file": lambda a: ("ok", None), "lint": lambda a: ("", None)},
        run_id="t",
    )
    return len(backend.calls), events


@pytest.mark.parametrize("image_exec,path,exits", [
    (False, "repro_issue.py", True),      # off: any write arms the exit (unchanged)
    (True, "pkg/mod.py", True),           # on: a source write still arms it
    (True, "repro_issue.py", False),      # on: a scratch reproducer does not
    (True, "test_repro/bar.py", False),   # on: nor a file in a new scratch dir
])
def test_scratch_write_does_not_arm_post_write_idle_exit(repo, monkeypatch, image_exec, path, exits):
    r, _ = repo
    calls, events = _idle_run(r, path, image_exec, monkeypatch)
    assert ("post_write_idle_exit" in events) is exits
    assert calls == (4 if exits else 7)
    assert ("scratch_write" in events) is (image_exec and not exits)


@pytest.mark.parametrize("commit_it", [False, True])
def test_strip_scratch_removes_a_nested_git_repo(repo, commit_it):
    from benchmarks.swebench.adapter import extract_diff, strip_scratch
    r, base = repo
    (r / "pkg" / "mod.py").write_text("x = 2\n")
    nested = r / "sphinx-test-locale-override"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "conf.py").write_text("language = 'da'\n")
    git(nested, "add", "-A")
    git(nested, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "n")
    if commit_it:   # luxe's commit step stages it as a gitlink
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "luxe seals edits")
    removed = strip_scratch(r, base)
    assert removed == ["sphinx-test-locale-override/"]
    assert not nested.exists()
    diff = extract_diff(r, base)
    assert "pkg/mod.py" in diff
    assert "Subproject" not in diff and "sphinx-test-locale-override" not in diff
