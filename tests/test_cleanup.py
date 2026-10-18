"""Cleanup turn (LUXE_CLEANUP_TURN, opt-in, 2026-10-17; luxe/cleanup.py).

Qwen3.8-27B verified its /health route with a throwaway `health_check_tmp.py`
("delete after use") and left it, failing the `orphan_file` gate 3/3 on an
otherwise-correct change (acceptance/tuning_2026_10). The model decides which
new, unreferenced files are scratch; Python deletes only offered paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from luxe import cleanup
from luxe.backend import ChatResponse, GenerationTiming


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    base = _git(r, "rev-parse", "HEAD").strip()
    # The run: a real new module the app imports, a scratch script, a cache,
    # and a new doc.
    (r / "health.py").write_text("def health():\n    return {'status': 'ok'}\n")
    (r / "app.py").write_text((r / "app.py").read_text() + "import health\n")
    (r / "health_check_tmp.py").write_text('"""Throwaway (delete after use)."""\n')
    (r / "__pycache__").mkdir()
    (r / "__pycache__" / "app.cpython-314.pyc").write_bytes(b"\0")
    (r / "NOTES.md").write_text("notes\n")
    return r, base


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list = []

    def chat(self, messages, **kw):
        self.calls.append((messages, kw))
        return ChatResponse(text=self.text, finish_reason="stop",
                            timing=GenerationTiming(prompt_tokens=1, completion_tokens=1))


def test_candidates_are_unreferenced_code_and_caches_only(repo):
    r, base = repo
    paths = {c.path: c.kind for c in cleanup.find_candidates(r, base)}
    assert paths == {"health_check_tmp.py": "unreferenced",
                     "__pycache__/app.cpython-314.pyc": "generated"}


def test_delete_lines_apply_only_to_offered_paths(repo):
    r, base = repo
    backend = _Reply("DELETE health_check_tmp.py\nDELETE app.py\n"
                     "KEEP __pycache__/app.cpython-314.pyc -- cache\n")
    rec = cleanup.run_cleanup_turn(backend, goal="add /health", repo=str(r), base_sha=base)  # maintain passes a str
    assert rec["deleted"] == ["health_check_tmp.py"]
    assert not (r / "health_check_tmp.py").exists()
    assert (r / "app.py").exists(), "a path the turn did not offer is untouchable"
    assert (r / "health.py").exists()
    _, kw = backend.calls[0]
    assert kw["tools"] is None and kw["temperature"] == 0.0


def test_emptied_cache_dir_is_pruned_and_staged_files_unstaged(repo):
    r, base = repo
    _git(r, "add", "-A")
    rec = cleanup.run_cleanup_turn(
        _Reply("- DELETE `__pycache__/app.cpython-314.pyc`"), goal="g",
        repo=r, base_sha=base)
    assert rec["deleted"] == ["__pycache__/app.cpython-314.pyc"]
    assert not (r / "__pycache__").exists()
    assert "pyc" not in _git(r, "diff", "--cached", "--name-only")


def test_no_candidates_means_no_model_call(repo):
    r, base = repo
    (r / "health_check_tmp.py").unlink()
    (r / "__pycache__" / "app.cpython-314.pyc").unlink()
    backend = _Reply("DELETE x")
    rec = cleanup.run_cleanup_turn(backend, goal="g", repo=r, base_sha=base)
    assert rec["candidates"] == [] and backend.calls == []


def test_backend_failure_leaves_the_tree_alone(repo):
    r, base = repo

    class Boom:
        def chat(self, *a, **k):
            raise RuntimeError("server gone")

    rec = cleanup.run_cleanup_turn(Boom(), goal="g", repo=r, base_sha=base)
    assert rec["deleted"] == [] and "server gone" in rec["error"]
    assert (r / "health_check_tmp.py").exists()


def test_maintain_never_imports_cleanup_when_off():
    src = Path(cleanup.__file__).with_name("maintain.py").read_text()
    assert 'os.environ.get("LUXE_CLEANUP_TURN") == "1"' in src
